"""LLM Service — Databricks Foundation Model API with Tool-Calling Agent.

Implements a tool-calling loop: the LLM can request tools (get_field_stats,
query_diff_results, etc.), the backend executes them, feeds results back,
and loops until the LLM produces a final answer.
"""

import re
import json
import logging
from typing import Optional, Callable
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

# ============================================================================
# Agent Tool Definitions (sent to the LLM as function schemas)
# ============================================================================

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_loaded_sources",
            "description": "Get information about currently loaded source files (filenames, row counts, headers). Call this when user asks what data is loaded.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_field_stats",
            "description": "Get per-field conflict statistics: how many conflicts each field has, sorted by most conflicts first. Call this when user asks which fields differ most.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_diff_results",
            "description": "Execute a SQL query against the diff_results SQLite table. Use for specific questions needing data lookups. Always LIMIT 50.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQLite SELECT query on diff_results table. Always include LIMIT.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sample_conflicts",
            "description": "Get sample rows where a specific field has conflicts. Shows COA, FAQ, DataPool values side by side.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "Canonical field name (e.g. 'gl_account_long_text').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of samples (default 10, max 30).",
                    },
                },
                "required": ["field"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_transforms",
            "description": "Get list of active transforms (normalization rules) applied to fields.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_comparison_summary",
            "description": "Get overall comparison summary: total rows, matches, conflicts, per-source counts, match percentage.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_jira_story",
            "description": "Create a JIRA story for a data conflict. Use when user asks to create a ticket, log a JIRA issue, or raise a story for a conflict. Provide the field name and optionally specific keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "The field name with conflicts (e.g. 'GL Account Long Text')"},
                    "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"], "description": "Story priority based on conflict severity"},
                    "keys": {"type": "string", "description": "Comma-separated specific keys to include, or 'all' for all conflicts in this field"},
                    "additional_context": {"type": "string", "description": "Any extra context to include in the story description"},
                },
                "required": ["field"],
            },
        },
    },
]


class LLMService:
    """Handles all LLM interactions for the RDM Agent with tool-calling."""

    MAX_TOOL_ROUNDS = 5  # Max tool-call iterations before forcing final answer

    def __init__(self, endpoint_name: str = "databricks-claude-sonnet-4-5"):
        self.endpoint_name = endpoint_name
        self.client = WorkspaceClient()
        self._host = self.client.config.host.rstrip("/")

    def _call_endpoint(self, messages: list, max_tokens: int = 2000, temperature: float = 0.2, tools: list = None) -> dict:
        """Call serving endpoint using SDK's API client (handles auth automatically)."""
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = self.client.api_client.do(
            "POST",
            f"/serving-endpoints/{self.endpoint_name}/invocations",
            body=payload,
        )
        return response

    def _build_system_prompt(self, context: dict) -> str:
        """Build system prompt for the tool-calling agent."""
        lines = [
            "You are RDM Agent, an expert Reference & Master Data analyst embedded in a 3-way reconciliation tool "
            "comparing COA Master Sheet, FAQ (SAP), and DataPool.",
            "You have full context of the loaded data and comparison results. Be specific, concise, and actionable.",
            "Format responses with clear sections using **bold** headers where helpful. Use bullet points for lists.",
            "",
            "## Current Session Context",
        ]

        # Source files
        sources = context.get("sources", {})
        for src in ["COA", "FAQ", "DataPool"]:
            info = sources.get(src, {})
            filename = info.get("filename", "not loaded")
            lines.append(f"**{src} file:** {filename}")

        # Mapping info
        mapping = context.get("mapping")
        if mapping:
            fields = mapping.get("comparable_fields", [])
            if fields:
                labels = [f.get("label", f.get("canonical", "")) for f in fields]
                lines.append(f"")
                lines.append(f"**Fields compared ({len(fields)}):** {', '.join(labels)}")

        # Diff results
        diff = context.get("diff_results")
        if diff and diff.get("total", 0) > 0:
            total = diff["total"]
            same = diff.get("same", 0)
            match_pct = f"{(same / total * 100):.1f}" if total > 0 else "0.0"
            lines.extend([
                "",
                "**Comparison results:**",
                f"  - Total rows: {total:,}",
                f"  - Matching (all sources agree): {same:,} ({match_pct}%)",
                f"  - Conflicts (same key, values differ): {diff.get('conflict', 0):,}",
                f"  - COA-only rows: {diff.get('onlyCOA', 0):,}",
                f"  - FAQ-only rows: {diff.get('onlyFAQ', 0):,}",
                f"  - DataPool-only rows: {diff.get('onlyDP', 0):,}",
            ])
        else:
            lines.extend(["", "**Comparison results:** No comparison has been run yet."])

        # SQL instructions with ACTUAL column names
        lines.extend([
            "",
            "## SQL Instructions",
            "A diff_results table is available. When a question needs specific rows or values, "
            "include a ```sql fenced block — it will be executed automatically.",
            "Always LIMIT 50. Column names use FULL canonical names (not abbreviations).",
            "dtype values: 'conflict', 'same', 'only_COA', 'only_FAQ', 'only_DataPool'.",
        ])

        # Include actual column names so the LLM doesn't guess
        if mapping:
            fields = mapping.get("comparable_fields", [])
            if fields:
                import re
                def safe_col(s):
                    name = re.sub(r"[^a-z0-9_]", "_", s.lower().strip())
                    name = re.sub(r"_+", "_", name).strip("_")
                    if name and name[0].isdigit():
                        name = "f_" + name
                    return name or "field"

                col_list = ["dtype TEXT", "key TEXT"]
                for f in fields:
                    cn = safe_col(f.get("canonical", ""))
                    col_list.append(f'coa_{cn} TEXT, faq_{cn} TEXT, dp_{cn} TEXT, conflict_{cn} INTEGER')
                lines.append("")
                lines.append("**Exact table schema (use these column names exactly):**")
                lines.append("```")
                lines.append(", ".join(col_list))
                lines.append("```")
                lines.append("")
                lines.append("IMPORTANT: Use the FULL canonical field names above. For example:")
                sample_cn = safe_col(fields[0].get("canonical", "")) if fields else "field_name"
                lines.append(f"  - To count conflicts on first field: SELECT SUM(conflict_{sample_cn}) FROM diff_results")
                lines.append(f"  - To find conflicting rows: SELECT * FROM diff_results WHERE conflict_{sample_cn} = 1 LIMIT 10")

        lines.extend([
            "",
            "## JIRA Integration",
            "You have a `create_jira_story` tool. When the user asks to create a JIRA ticket/story for a conflict:",
            "1. First gather the conflict details using get_field_stats or get_sample_conflicts",
            "2. Present a summary preview: field, conflict count, priority suggestion, and sample values",
            "3. Ask the user to confirm before calling create_jira_story",
            "4. If JIRA returns success, display the story key and URL as a link",
            "5. If JIRA is not configured, tell the user which env vars to set",
            "Auto-suggest priority: >30% conflicts = High, >10% = Medium, else Low",
            "",
            "## Follow-up Suggestions",
            "After your answer, include a JSON block with 3 suggested follow-up questions.",
            "Format: ```suggestions\n[\"question 1\", \"question 2\", \"question 3\"]\n```",
            "Suggestions must be short (under 40 chars), actionable, and relevant to what was just discussed.",
        ])

        return "\n".join(lines)

    def chat(
        self,
        user_message: str,
        history: list,
        context: dict,
        tool_executor: Optional[Callable] = None,
    ) -> dict:
        """Send a chat message using a tool-calling agent loop.

        Args:
            user_message: The user's latest message.
            history: Conversation history [{role, content}, ...].
            context: Session context (sources, mapping, diff_results).
            tool_executor: Callable(tool_name, arguments) -> str JSON result.
                           If provided, the agent can call tools iteratively.

        Returns:
            dict with 'reply', optional 'sql', and 'tool_calls_made' count.
        """
        system_prompt = self._build_system_prompt(context)

        # Build messages: system + last 8 history turns + new user message
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": user_message})

        reply = ""
        tool_calls_made = 0
        tools = AGENT_TOOLS if tool_executor else None

        for _round in range(self.MAX_TOOL_ROUNDS):
            data = self._call_endpoint(messages, max_tokens=2000, temperature=0.2, tools=tools)

            # Extract the response message
            if isinstance(data, dict):
                choices = data.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
            elif hasattr(data, "choices") and data.choices:
                msg = data.choices[0].message
                message = {"role": getattr(msg, "role", "assistant"),
                           "content": getattr(msg, "content", None),
                           "tool_calls": getattr(msg, "tool_calls", None)}
            else:
                message = {}

            tool_calls = message.get("tool_calls")

            # No tool calls → this is the final answer
            if not tool_calls or not tool_executor:
                reply = message.get("content") or ""
                if not isinstance(reply, str):
                    reply = ""
                break

            # Append assistant message (with tool_calls) to conversation
            messages.append({
                "role": "assistant",
                "content": message.get("content"),  # may be None
                "tool_calls": tool_calls,
            })

            # Execute each tool call and feed results back
            for tc in tool_calls:
                tool_calls_made += 1
                if isinstance(tc, dict):
                    tc_id = tc.get("id", "")
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "")
                    try:
                        arguments = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                else:
                    tc_id = getattr(tc, "id", "")
                    fn = getattr(tc, "function", None)
                    tool_name = getattr(fn, "name", "") if fn else ""
                    try:
                        arguments = json.loads(getattr(fn, "arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}

                try:
                    result_str = tool_executor(tool_name, arguments)
                except Exception as e:
                    result_str = json.dumps({"error": str(e)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_str,
                })
        else:
            # Exhausted MAX_TOOL_ROUNDS — use last message content
            reply = message.get("content") or ""
            if not isinstance(reply, str):
                reply = ""

        # Extract SQL block if present
        sql_match = re.search(r"```sql\s*([\s\S]*?)```", reply, re.IGNORECASE)
        sql = sql_match.group(1).strip() if sql_match else None

        return {
            "reply": reply,
            "sql": sql,
            "tool_calls_made": tool_calls_made,
        }

    def generate_transform(
        self,
        instruction: str,
        field: str,
        samples: list,
    ) -> dict:
        """Generate a Python transform function for field normalization.

        Replaces the JS function generation in the original app.
        """
        prompt = (
            f"Generate a Python function called `transform(value: str) -> str` that implements: "
            f"{instruction}\n\n"
            f"Field: {field}\n"
            f"Sample values: {json.dumps(samples[:10])}\n\n"
            f"Return ONLY the function definition, no explanation. "
            f"The function must handle None/empty values gracefully."
        )

        messages = [
            {"role": "system", "content": "You are a Python code generator. Return only valid Python code, no explanation."},
            {"role": "user", "content": prompt},
        ]

        # Call via SDK's API client (handles auth automatically)
        data = self._call_endpoint(messages, max_tokens=500, temperature=0.0)

        reply = ""
        if isinstance(data, dict):
            choices = data.get("choices", [])
            if choices:
                reply = choices[0].get("message", {}).get("content", "")
        elif hasattr(data, "choices") and data.choices:
            reply = data.choices[0].message.content

        # Extract function from code block
        code_match = re.search(r"```python\s*([\s\S]*?)```", reply)
        fn_str = code_match.group(1).strip() if code_match else reply.strip()

        return {
            "function": fn_str,
            "function_code": fn_str,
            "instruction": instruction,
            "field": field,
        }
