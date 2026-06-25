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
            "description": "Get per-field conflict statistics: how many conflicts each field has, sorted by most conflicts. Call this when user asks which fields differ most or about data quality.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_diff_results",
            "description": "Execute a SQL query against the diff_results SQLite table. Use for specific questions needing data lookups. Always include LIMIT clause (max 50).",
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
            "description": "Get sample rows where a specific field has conflicts. Shows COA, FAQ, DataPool values side by side for comparison.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "Canonical field name (e.g. 'gl_account_long_text', 'indicator_blocked_for_posting').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of samples to return (default 10, max 30).",
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
            "description": "Get list of active data transforms (normalization rules) applied to fields before comparison.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_comparison_summary",
            "description": "Get overall comparison summary: total rows, matches, conflicts, per-source only counts, match percentage. Call this for overview questions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
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
            "",
            "## Your Capabilities",
            "You have TOOLS to query the data directly. USE THEM instead of guessing:",
            "- `get_loaded_sources` — see what files are loaded",
            "- `get_comparison_summary` — overall match/conflict stats",
            "- `get_field_stats` — per-field conflict counts (which fields differ most)",
            "- `query_diff_results` — run SQL on the diff table for specific lookups",
            "- `get_sample_conflicts` — see actual differing values for a field",
            "- `get_active_transforms` — see what normalization rules are applied",
            "",
            "## Rules",
            "- ALWAYS call tools to get real data before answering data questions.",
            "- Do NOT guess or make up numbers. If you need data, call a tool.",
            "- Be specific, concise, and actionable. Use **bold** for headers, bullets for lists.",
            "- Format tables using markdown | col | syntax when showing tabular results.",
            "",
            "## Follow-up Suggestions",
            "After your FINAL answer, include exactly 3 follow-up suggestions:",
            "```suggestions",
            '["short question 1", "short question 2", "short question 3"]',
            "```",
        ]

        # Include column schema so query_diff_results tool calls use correct names
        mapping = context.get("mapping")
        if mapping:
            fields = mapping.get("comparable_fields", [])
            if fields:
                def safe_col(s):
                    name = re.sub(r"[^a-z0-9_]", "_", s.lower().strip())
                    name = re.sub(r"_+", "_", name).strip("_")
                    if name and name[0].isdigit():
                        name = "f_" + name
                    return name or "field"

                lines.append("")
                lines.append("## diff_results SQL Schema (for query_diff_results tool)")
                lines.append("Columns: dtype TEXT, key TEXT, " + ", ".join(
                    f"coa_{safe_col(f['canonical'])} TEXT, faq_{safe_col(f['canonical'])} TEXT, dp_{safe_col(f['canonical'])} TEXT, conflict_{safe_col(f['canonical'])} INT"
                    for f in fields
                ))
                lines.append("")
                lines.append("Field label mapping: " + ", ".join(
                    f"{safe_col(f['canonical'])}=\'{f.get('label', f['canonical'])}\'"
                    for f in fields
                ))

        return "\n".join(lines)

    def chat(
        self,
        user_message: str,
        history: list,
        context: dict,
        tool_executor: Callable = None,
    ) -> dict:
        """Run the agent loop: call LLM with tools, execute tool calls, repeat until final answer.

        Args:
            user_message: The user's latest message.
            history: Conversation history [{role, content}, ...].
            context: Session context (sources, mapping, diff_results).
            tool_executor: Callable(tool_name, arguments) -> str that executes tools.

        Returns:
            dict with 'reply' (text) and optionally 'sql' (extracted SQL), 'tool_calls_made' (list).
        """
        system_prompt = self._build_system_prompt(context)

        # Build messages array: system + history (last 8 turns) + new message
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": user_message})

        tool_calls_made = []
        tools_to_use = AGENT_TOOLS if tool_executor else None

        # Agent loop: call LLM → if tool_calls → execute → feed back → repeat
        for round_num in range(self.MAX_TOOL_ROUNDS):
            data = self._call_endpoint(messages, max_tokens=2000, temperature=0.2, tools=tools_to_use)

            # Extract the response message
            choice = {}
            if isinstance(data, dict):
                choices = data.get("choices", [])
                if choices:
                    choice = choices[0]
            elif hasattr(data, "choices") and data.choices:
                choice = {"message": {"role": "assistant", "content": data.choices[0].message.content}}

            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")

            # Check if LLM wants to call tools
            tool_calls = message.get("tool_calls", [])

            if tool_calls and tool_executor:
                # Append the assistant message with tool_calls to conversation
                messages.append(message)

                # Execute each tool call and add results
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        arguments = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {}

                    logger.info(f"Agent tool call [{round_num+1}]: {tool_name}({arguments})")

                    # Execute the tool
                    try:
                        result = tool_executor(tool_name, arguments)
                    except Exception as e:
                        result = json.dumps({"error": str(e)})

                    tool_calls_made.append({"tool": tool_name, "args": arguments})

                    # Add tool result to conversation
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result if isinstance(result, str) else json.dumps(result),
                    })

                # Continue loop — LLM will see tool results and respond
                continue

            else:
                # No tool calls — this is the final answer
                reply = message.get("content") or ""
                break
        else:
            # Exhausted max rounds, get whatever we have
            reply = (message.get("content") or "") if message else "I was unable to complete the analysis within the allowed steps."

        # Ensure reply is always a string (content can be null in tool-call responses)
        if not isinstance(reply, str):
            reply = ""

        # Extract SQL block if present (for backward compat)
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
        """Generate a Python transform function for field normalization."""
        prompt = (
            f"Generate a Python lambda or one-liner function that implements: "
            f"{instruction}\n\n"
            f"Field: {field}\n"
            f"Sample values: {json.dumps(samples[:10])}\n\n"
            f"Return ONLY a single lambda expression like: lambda v: v.upper()\n"
            f"Handle None/empty values gracefully. No explanation, just the lambda."
        )

        messages = [
            {"role": "system", "content": "You are a Python code generator. Return only a lambda expression, no explanation."},
            {"role": "user", "content": prompt},
        ]

        data = self._call_endpoint(messages, max_tokens=500, temperature=0.0)

        reply = ""
        if isinstance(data, dict):
            choices = data.get("choices", [])
            if choices:
                reply = choices[0].get("message", {}).get("content") or ""
        elif hasattr(data, "choices") and data.choices:
            reply = data.choices[0].message.content or ""

        # Extract function from code block
        code_match = re.search(r"```python\s*([\s\S]*?)```", reply)
        fn_str = code_match.group(1).strip() if code_match else reply.strip()

        return {
            "function": fn_str,
            "function_code": fn_str,
            "instruction": instruction,
            "field": field,
        }
