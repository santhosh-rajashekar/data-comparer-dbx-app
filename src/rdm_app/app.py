"""RDM 3-Way Diff — Databricks App

Flask application for Reference & Master Data Quality Assurance.
Performs 3-way reconciliation across COA, FAQ (SAP), and DataPool sources.
Uses Databricks Foundation Model API for AI chat (RDM Agent).
"""

import os
import sys
import json
import uuid
import io
import logging
from flask import Flask, render_template, request, jsonify, session, send_file

# Configure logging for visibility in app logs
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

try:
    from services.llm_service import LLMService
    from services.diff_service import DiffService
    from services.file_service import FileService
    from services.jira_service import JiraService
    logger.info("All services imported successfully")
except Exception as e:
    logger.error(f"Failed to import services: {e}", exc_info=True)
    raise

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", str(uuid.uuid4()))

# Allow large file uploads (up to 200 MB)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# =============================================================================
# Server-side session store (replaces Flask cookie session which has 4KB limit).
# Flask cookies can't hold headers for 3 source files. Store all session data
# server-side in memory, keyed by a session_id cookie.
# =============================================================================
_sessions = {}  # {session_id: {sources: {...}, mapping: {...}, diff_results: {...}}}


def get_session_id():
    """Get or create a session ID from the cookie."""
    sid = session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    if sid not in _sessions:
        _sessions[sid] = {"sources": {}, "mapping": None, "diff_results": None}
    return sid


def get_store():
    """Get the server-side store for the current session."""
    sid = get_session_id()
    return _sessions[sid]


def get_current_user() -> dict:
    """Read user identity from Databricks Apps proxy headers.

    Databricks Apps injects X-Forwarded-Email and X-Forwarded-User
    after validating the user's workspace SSO session.
    These headers are trustworthy — they cannot be spoofed by the client.
    """
    email = request.headers.get("X-Forwarded-Email", "")
    user = request.headers.get("X-Forwarded-User", email)
    return {"email": email, "user": user or email}


# Initialize services
llm_service = LLMService(
    endpoint_name=os.environ.get("SERVING_ENDPOINT", "databricks-claude-sonnet-4-5")
)
diff_service = DiffService()
file_service = FileService(
    volume_path=os.environ.get("UPLOAD_VOLUME_PATH", "/Volumes/data_mesh_hub/rdm/uploads")
)
jira_service = JiraService()


@app.route("/")
def index():
    """Render the main RDM 3-Way Diff page."""
    return render_template("index.html")


@app.route("/api/whoami", methods=["GET"])
def whoami():
    """Return the authenticated user's identity (from Databricks Apps proxy headers)."""
    return jsonify(get_current_user())


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Upload a source file (COA, FAQ, or DataPool)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    source = request.form.get("source", "unknown")

    try:
        store = get_store()
        result = file_service.process_upload(file, source, get_session_id())
        # Store full result server-side (includes rows for later diff)
        store["sources"][source] = result
        logger.info(f"Uploaded {source}: {result['filename']} ({result['row_count']} rows, {len(result['headers'])} cols)")
        logger.info(f"Session now has {len(store['sources'])} source(s): {list(store['sources'].keys())}")
        return jsonify({
            "filename": result["filename"],
            "source": result["source"],
            "headers": result["headers"],
            "row_count": result["row_count"],
            "sheets": result.get("sheets", []),
            "active_sheet": result.get("active_sheet"),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/mapping", methods=["POST"])
def compute_mapping():
    """Compute column mapping across uploaded sources."""
    store = get_store()
    sources = store.get("sources", {})
    data = request.get_json() or {}
    mode = data.get("mode", "SKB")
    logger.info(f"Mapping requested. Sources available: {list(sources.keys())}, mode={mode}")
    if len(sources) < 2:
        return jsonify({"error": f"Upload at least 2 source files. Currently have: {list(sources.keys())}"}), 400

    try:
        mapping = diff_service.compute_mapping(sources, mode=mode)
        store["mapping"] = mapping
        return jsonify(mapping)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/compare", methods=["POST"])
def run_comparison():
    """Run the 3-way diff comparison."""
    store = get_store()
    sources = store.get("sources", {})
    mapping = store.get("mapping")
    config = request.get_json() or {}

    if not mapping:
        return jsonify({"error": "Run mapping first"}), 400

    try:
        results = diff_service.run_diff(
            sources=sources,
            mapping=mapping,
            mode=config.get("mode", "SKB"),
            options=config.get("options", {}),
            key_columns=config.get("key_columns", {}),
            custom_transforms=store.get("custom_transforms", {}),
            disabled_transforms=store.get("disabled_transforms", []),
        )
        store["diff_results"] = results["summary"]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Agent Tool Executor — implements the tools the LLM can call
# =============================================================================

def _execute_agent_tool(tool_name: str, arguments: dict, store: dict) -> str:
    """Execute an agent tool and return the result as a JSON string."""
    import re as _re

    def safe_col(s):
        name = _re.sub(r"[^a-z0-9_]", "_", s.lower().strip())
        name = _re.sub(r"_+", "_", name).strip("_")
        if name and name[0].isdigit():
            name = "f_" + name
        return name or "field"

    if tool_name == "get_loaded_sources":
        sources = store.get("sources", {})
        result = {}
        for k, v in sources.items():
            result[k] = {
                "filename": v.get("filename", "not loaded"),
                "row_count": v.get("row_count", 0),
                "headers": v.get("headers", [])[:20],  # First 20 headers
            }
        if not result:
            return json.dumps({"message": "No sources loaded yet. User needs to upload files first."})
        return json.dumps(result)

    elif tool_name == "get_comparison_summary":
        diff = store.get("diff_results")
        if not diff or not diff.get("total"):
            return json.dumps({"message": "No comparison has been run yet."})
        total = diff["total"]
        same = diff.get("same", 0)
        match_pct = round(same / total * 100, 1) if total > 0 else 0
        return json.dumps({
            "total_rows": total,
            "matching_rows": same,
            "match_percentage": match_pct,
            "conflict_rows": diff.get("conflict", 0),
            "coa_only": diff.get("onlyCOA", 0),
            "faq_only": diff.get("onlyFAQ", 0),
            "datapool_only": diff.get("onlyDP", 0),
        })

    elif tool_name == "get_field_stats":
        # Query SQLite for per-field conflict counts
        mapping = store.get("mapping")
        if not mapping:
            return json.dumps({"message": "No mapping available. Run comparison first."})
        fields = mapping.get("comparable_fields", [])
        if not fields:
            return json.dumps({"message": "No comparable fields found."})

        total = store.get("diff_results", {}).get("total", 0)
        stats = []
        for f in fields:
            cn = safe_col(f.get("canonical", ""))
            col = f"conflict_{cn}"
            try:
                result = diff_service.execute_sql(f"SELECT SUM({col}) as cnt FROM diff_results WHERE dtype='conflict'")
                rows = result.get("rows", [])
                count = rows[0][0] if rows and rows[0][0] else 0
            except Exception:
                count = 0
            stats.append({
                "field": f.get("label", f.get("canonical", "")),
                "canonical": cn,
                "conflicts": int(count),
                "pct_of_total": round(count / total * 100, 1) if total > 0 else 0,
            })
        stats.sort(key=lambda x: x["conflicts"], reverse=True)
        return json.dumps({"fields": stats, "total_rows": total})

    elif tool_name == "query_diff_results":
        sql = arguments.get("sql", "")
        if not sql:
            return json.dumps({"error": "No SQL provided."})
        # Safety: only allow SELECT
        if not sql.strip().upper().startswith("SELECT"):
            return json.dumps({"error": "Only SELECT queries are allowed."})
        try:
            result = diff_service.execute_sql(sql)
            if result.get("error"):
                return json.dumps({"error": result["error"]})
            return json.dumps({"columns": result.get("columns", []), "rows": result.get("rows", [])[:50], "row_count": len(result.get("rows", []))})
        except Exception as e:
            return json.dumps({"error": f"SQL error: {str(e)}"})

    elif tool_name == "get_sample_conflicts":
        field = arguments.get("field", "")
        limit = min(arguments.get("limit", 10), 30)
        cn = safe_col(field)
        try:
            sql = f"SELECT key, coa_{cn}, faq_{cn}, dp_{cn} FROM diff_results WHERE conflict_{cn} = 1 LIMIT {limit}"
            result = diff_service.execute_sql(sql)
            if result.get("error"):
                return json.dumps({"error": result["error"]})
            samples = []
            for row in result.get("rows", []):
                samples.append({"key": row[0], "coa": row[1], "faq": row[2], "datapool": row[3]})
            return json.dumps({"field": field, "samples": samples, "count": len(samples)})
        except Exception as e:
            return json.dumps({"error": f"Could not get samples for '{field}': {str(e)}"})

    elif tool_name == "get_active_transforms":
        # Return both predefined and custom transforms
        active = getattr(diff_service, '_active_transforms', [])
        custom = store.get("custom_transforms", {})
        disabled = list(store.get("disabled_transforms", set()))
        return json.dumps({
            "active_transforms": active,
            "custom_transforms": list(custom.keys()),
            "disabled_predefined": disabled,
        })

    elif tool_name == "create_jira_story":
        field = arguments.get("field", "")
        priority = arguments.get("priority", "Medium")
        keys_arg = arguments.get("keys", "all")
        additional_context = arguments.get("additional_context", "")

        if not field:
            return json.dumps({"error": "Field name is required"})

        # Check JIRA configuration
        if not jira_service.configured:
            return json.dumps({"error": "JIRA not configured. Set JIRA_BASE_URL, JIRA_PROJECT_KEY, JIRA_USER_EMAIL, JIRA_API_TOKEN in app environment.", "jira_status": jira_service.get_status()})

        # Gather conflict data for this field
        cn = safe_col(field)
        try:
            # Get conflict count
            count_sql = f"SELECT COUNT(*) FROM diff_results WHERE conflict_{cn} = 1"
            count_result = diff_service.execute_sql(count_sql)
            conflict_count = count_result.get("rows", [[0]])[0][0] if not count_result.get("error") else 0

            total_sql = "SELECT COUNT(*) FROM diff_results"
            total_result = diff_service.execute_sql(total_sql)
            total_records = total_result.get("rows", [[0]])[0][0] if not total_result.get("error") else 0

            # Get sample conflicts for JIRA description
            sample_sql = f"SELECT key, coa_{cn}, faq_{cn}, dp_{cn} FROM diff_results WHERE conflict_{cn} = 1 LIMIT 10"
            sample_result = diff_service.execute_sql(sample_sql)
            sample_rows = []
            for row in sample_result.get("rows", []):
                sample_rows.append({"key": row[0], "COA": row[1], "FAQ": row[2], "DataPool": row[3]})

            # Get all conflict rows for CSV attachment (or filtered by keys)
            if keys_arg and keys_arg != "all":
                key_list = [k.strip() for k in keys_arg.split(",")]
                placeholders = ",".join([f"'{k}'" for k in key_list])
                all_sql = f"SELECT key, coa_{cn}, faq_{cn}, dp_{cn} FROM diff_results WHERE conflict_{cn} = 1 AND key IN ({placeholders})"
            else:
                all_sql = f"SELECT key, coa_{cn}, faq_{cn}, dp_{cn} FROM diff_results WHERE conflict_{cn} = 1"
            all_result = diff_service.execute_sql(all_sql)
            all_rows = [{"key": r[0], "COA": r[1], "FAQ": r[2], "DataPool": r[3]} for r in all_result.get("rows", [])]

            # Determine sources present
            sources = []
            for src in ["COA", "FAQ", "DataPool"]:
                if store.get("sources", {}).get(src):
                    sources.append(src)

            # Create JIRA story (include requestor identity)
            current_user = get_current_user()
            ctx = additional_context
            if current_user["email"]:
                ctx = f"{ctx}\nRequested by: {current_user['email']}".strip()
            result = jira_service.create_conflict_story(
                field=field,
                conflict_count=conflict_count,
                total_records=total_records,
                sample_rows=sample_rows,
                all_conflict_rows=all_rows,
                sources=sources,
                priority=priority,
                additional_context=ctx,
            )
            if result.get("success"):
                logger.info(f"JIRA story created: {result['key']} by {current_user['email']} for field '{field}'")

            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": f"Failed to create JIRA story: {str(e)}"})

    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})


@app.route("/api/chat", methods=["POST"])
def chat():
    """RDM Agent chat endpoint — tool-calling agent loop."""
    data = request.get_json()
    user_message = data.get("message", "")
    history = data.get("history", [])

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    # Build context from server-side store
    store = get_store()
    context = {
        "sources": {k: {"filename": v.get("filename"), "headers": v.get("headers"), "row_count": v.get("row_count")} for k, v in store.get("sources", {}).items()},
        "mapping": store.get("mapping"),
        "diff_results": store.get("diff_results"),
    }

    # Tool executor — runs agent tools against session data
    def execute_tool(tool_name: str, arguments: dict) -> str:
        return _execute_agent_tool(tool_name, arguments, store)

    try:
        response = llm_service.chat(
            user_message=user_message,
            history=history,
            context=context,
            tool_executor=execute_tool,
        )
        # Extract follow-up suggestions from the reply if present
        import re as _re
        suggestions_match = _re.search(r'```suggestions\s*([\s\S]*?)```', response.get("reply", ""))
        if suggestions_match:
            try:
                suggestions = json.loads(suggestions_match.group(1).strip())
                response["suggestions"] = suggestions
                # Remove the suggestions block from the reply
                response["reply"] = response["reply"][:suggestions_match.start()].strip()
            except (json.JSONDecodeError, ValueError):
                pass
        return jsonify(response)
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat/sql", methods=["POST"])
def execute_chat_sql():
    """Execute SQL generated by the RDM Agent against diff results."""
    data = request.get_json()
    sql = data.get("sql", "")

    if not sql:
        return jsonify({"error": "No SQL provided"}), 400

    try:
        result = diff_service.execute_sql(sql)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export", methods=["POST"])
def export_results():
    """Export comparison results as Excel (.xlsx) or CSV file."""
    data = request.get_json() or {}
    format_type = data.get("format", "excel")
    conflicts_only = data.get("conflicts_only", False)

    try:
        store = get_store()
        mapping = store.get("mapping", {})
        fields = mapping.get("comparable_fields", [])

        # Get diff rows from the service's internal state
        result = diff_service.export(format_type=format_type, conflicts_only=conflicts_only)
        if "error" in result:
            return jsonify(result), 400
        rows = result.get("data", [])  # Key is "data" from diff_service.export()

        if format_type == "csv":
            # Generate CSV
            import csv
            output = io.StringIO()
            writer = csv.writer(output)
            # Header: Status | Key | then per field: COA | FAQ | DataPool
            hdr = ["Status", "Key"]
            for f in fields:
                label = f.get("label", f.get("canonical", ""))
                hdr.extend([f"{label} (COA)", f"{label} (FAQ)", f"{label} (DataPool)"])
            writer.writerow(hdr)
            for r in rows:
                vals = r.get("vals", {})
                row = [r.get("dtype", ""), r.get("key", "")]
                for f in fields:
                    fv = vals.get(f.get("canonical", ""), {})
                    row.extend([fv.get("COA", ""), fv.get("FAQ", ""), fv.get("DataPool", "")])
                writer.writerow(row)
            csv_bytes = io.BytesIO(output.getvalue().encode("utf-8-sig"))
            filename = "rdm_conflicts.csv" if conflicts_only else "rdm_diff_results.csv"
            return send_file(csv_bytes, mimetype="text/csv", as_attachment=True, download_name=filename)

        # Excel export with rich styling (matching original HTML version)
        import xlsxwriter
        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {"in_memory": True})
        ws = wb.add_worksheet("Diff Results")

        # --- Formats (matching original HTML Excel styling) ---
        hdr_fmt = wb.add_format({
            "bold": True, "bg_color": "#1e3a5f", "font_color": "#ffffff",
            "font_size": 10, "border": 1, "border_color": "#0d1f33",
            "text_wrap": True, "valign": "vcenter",
        })
        hdr_coa_fmt = wb.add_format({
            "bold": True, "bg_color": "#276749", "font_color": "#ffffff",
            "font_size": 10, "border": 1, "text_wrap": True, "valign": "vcenter",
        })
        hdr_faq_fmt = wb.add_format({
            "bold": True, "bg_color": "#8C1A30", "font_color": "#ffffff",
            "font_size": 10, "border": 1, "text_wrap": True, "valign": "vcenter",
        })
        hdr_dp_fmt = wb.add_format({
            "bold": True, "bg_color": "#1A4D9A", "font_color": "#ffffff",
            "font_size": 10, "border": 1, "text_wrap": True, "valign": "vcenter",
        })
        conflict_fmt = wb.add_format({"bg_color": "#FDE8E8", "font_size": 10, "border": 1, "border_color": "#e5e7eb"})
        same_fmt = wb.add_format({"bg_color": "#D1FAE5", "font_size": 10, "border": 1, "border_color": "#e5e7eb"})
        only_fmt = wb.add_format({"bg_color": "#FEF3C7", "font_size": 10, "border": 1, "border_color": "#e5e7eb"})
        default_fmt = wb.add_format({"font_size": 10, "border": 1, "border_color": "#e5e7eb"})
        conflict_cell_fmt = wb.add_format({"bg_color": "#FDE8E8", "font_size": 10, "font_color": "#991B1B", "bold": True, "border": 1, "border_color": "#e5e7eb"})

        # --- Write header row ---
        col = 0
        ws.write(0, col, "Status", hdr_fmt); col += 1
        ws.write(0, col, "Key", hdr_fmt); col += 1
        for f in fields:
            label = f.get("label", f.get("canonical", ""))
            ws.write(0, col, f"{label} (COA)", hdr_coa_fmt); col += 1
            ws.write(0, col, f"{label} (FAQ)", hdr_faq_fmt); col += 1
            ws.write(0, col, f"{label} (DataPool)", hdr_dp_fmt); col += 1

        # --- Write data rows with conditional formatting ---
        row_num = 1
        for r in rows:
            vals = r.get("vals", {})
            dtype = r.get("dtype", "")
            field_conflicts = r.get("field_conflicts", [])

            # Row-level format based on status
            if dtype == "conflict":
                row_fmt = conflict_fmt
                status_label = "\u26a0 Conflict"
            elif dtype == "same":
                row_fmt = same_fmt
                status_label = "\u2713 Match"
            elif dtype.startswith("only_"):
                row_fmt = only_fmt
                status_label = dtype.replace("only_", "Only in ")
            else:
                row_fmt = default_fmt
                status_label = dtype

            ws.write(row_num, 0, status_label, row_fmt)
            ws.write(row_num, 1, r.get("key", ""), row_fmt)

            col = 2
            for fi, f in enumerate(fields):
                fv = vals.get(f.get("canonical", ""), {})
                is_field_conflict = fi < len(field_conflicts) and field_conflicts[fi]
                cell_fmt = conflict_cell_fmt if is_field_conflict else row_fmt
                ws.write(row_num, col, fv.get("COA", ""), cell_fmt); col += 1
                ws.write(row_num, col, fv.get("FAQ", ""), cell_fmt); col += 1
                ws.write(row_num, col, fv.get("DataPool", ""), cell_fmt); col += 1

            row_num += 1

        # --- Formatting ---
        ws.set_column(0, 0, 14)  # Status
        ws.set_column(1, 1, 16)  # Key
        ws.set_column(2, col, 18)  # Data columns
        ws.freeze_panes(1, 2)  # Freeze header + key columns
        ws.autofilter(0, 0, row_num - 1, col - 1)

        wb.close()
        output.seek(0)
        filename = "rdm_conflicts.xlsx" if conflicts_only else "rdm_diff_results.xlsx"
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=filename)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/transform/preview", methods=["POST"])
def preview_transform():
    """Preview a transform: generates function, shows sample before/after."""
    data = request.get_json()
    field = data.get("field", "")
    source = data.get("source", "FAQ")
    instruction = data.get("instruction", "")

    if not field or not instruction:
        return jsonify({"error": "Field and instruction required"}), 400

    store = get_store()
    sources_data = store.get("sources", {})

    # Get sample values for this field from the selected source(s)
    target_sources = ["COA", "FAQ", "DataPool"] if source == "All" else [source]
    samples_raw = []
    mapping = store.get("mapping", {})
    fields = mapping.get("comparable_fields", [])
    field_def = next((f for f in fields if f.get("canonical") == field), None)

    if field_def:
        for src in target_sources:
            col_name = field_def.get("sources", {}).get(src)
            src_data = sources_data.get(src, {})
            rows = src_data.get("rows", [])
            headers = src_data.get("headers", [])
            if col_name and col_name in headers:
                col_idx = headers.index(col_name)
                seen = set()
                for row in rows:
                    if col_idx < len(row):
                        val = str(row[col_idx]) if row[col_idx] is not None else ""
                        if val and val != "nan" and val not in seen:
                            seen.add(val)
                            samples_raw.append(val)
                    if len(samples_raw) >= 10:
                        break

    # Generate transform function using LLM (or use preset logic)
    try:
        func_code = _get_transform_function(instruction, field, samples_raw)
        # Apply the transform to samples for preview
        preview_samples = []
        for val in samples_raw[:10]:
            transformed = _apply_transform_code(func_code, val)
            preview_samples.append({"original": val, "transformed": transformed})

        return jsonify({
            "samples": preview_samples,
            "function_code": func_code,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transform/apply", methods=["POST"])
def apply_transform():
    """Apply a user-defined transform to a field for subsequent comparisons."""
    data = request.get_json()
    field = data.get("field", "")
    source = data.get("source", "FAQ")
    instruction = data.get("instruction", "")
    function_code = data.get("function_code", "")

    if not field or not function_code:
        return jsonify({"error": "Field and function_code required"}), 400

    store = get_store()
    if "custom_transforms" not in store:
        store["custom_transforms"] = {}

    # Store the custom transform (keyed by field:source)
    sources = ["COA", "FAQ", "DataPool"] if source == "All" else [source]
    for src in sources:
        key = f"{field}:{src}"
        store["custom_transforms"][key] = {
            "field": field,
            "source": src,
            "instruction": instruction,
            "function_code": function_code,
        }

    return jsonify({"ok": True, "count": len(store["custom_transforms"])})


@app.route("/api/transform/clear", methods=["POST"])
def clear_transform():
    """Remove a custom transform for a field/source (from transform panel)."""
    data = request.get_json()
    field = data.get("field", "")
    source = data.get("source", "FAQ")

    store = get_store()
    transforms = store.get("custom_transforms", {})
    sources = ["COA", "FAQ", "DataPool"] if source == "All" else [source]
    for src in sources:
        key = f"{field}:{src}"
        transforms.pop(key, None)

    return jsonify({"ok": True, "count": len(transforms)})


@app.route("/api/transform/remove", methods=["POST"])
def remove_transform():
    """Remove a transform from the active list.

    For predefined transforms: adds to a 'disabled' list so they're skipped during comparison.
    For custom transforms: deletes them from the session.
    """
    data = request.get_json()
    field = data.get("field", "")
    source = data.get("source", "")
    is_custom = data.get("is_custom", False)

    if not field or not source:
        return jsonify({"error": "Field and source required"}), 400

    store = get_store()

    if is_custom:
        # Remove from custom transforms
        custom = store.get("custom_transforms", {})
        key = f"{field}:{source}"
        custom.pop(key, None)
    else:
        # Disable a predefined transform by adding to disabled list
        if "disabled_transforms" not in store:
            store["disabled_transforms"] = []
        entry = {"field": field, "source": source}
        if entry not in store["disabled_transforms"]:
            store["disabled_transforms"].append(entry)

    return jsonify({"ok": True})


def _get_transform_function(instruction: str, field: str, samples: list) -> str:
    """Generate a Python transform function from instruction.
    Uses predefined logic for common patterns, falls back to LLM."""
    instr_lower = instruction.lower().strip()

    # Preset transforms (no LLM needed)
    presets = {
        "remove leading zeros": "lambda v: v.lstrip('0') or '0' if v else v",
        "uppercase": "lambda v: v.upper() if v else v",
        "lowercase": "lambda v: v.lower() if v else v",
        "trim whitespace": "lambda v: v.strip() if v else v",
        "extract digits only": "lambda v: ''.join(c for c in v if c.isdigit()) if v else v",
        "remove special chars": "lambda v: ''.join(c for c in v if c.isalnum() or c == ' ') if v else v",
        "extract before first space": "lambda v: v.split(' ')[0] if v else v",
        "extract before space": "lambda v: v.split(' ')[0] if v else v",
    }

    if instr_lower in presets:
        return presets[instr_lower]

    # Check for partial matches
    for key, code in presets.items():
        if key in instr_lower:
            return code

    # Fall back to LLM
    try:
        result = llm_service.generate_transform(
            instruction=instruction,
            field=field,
            samples=samples[:10]
        )
        return result.get("function_code", "lambda v: v")
    except Exception:
        return "lambda v: v"


def _apply_transform_code(func_code: str, value: str) -> str:
    """Safely apply a transform function (as string) to a value."""
    try:
        ns = {}
        exec(f"fn = {func_code}", ns)
        fn = ns.get("fn", lambda v: v)
        result = fn(value)
        return str(result) if result is not None else ""
    except Exception:
        return value


# =============================================================================
# JIRA Integration
# =============================================================================

@app.route("/api/jira/status", methods=["GET"])
def jira_status():
    """Check JIRA configuration status."""
    return jsonify(jira_service.get_status())


@app.route("/api/jira/test", methods=["POST"])
def jira_test():
    """Test JIRA connectivity."""
    return jsonify(jira_service.test_connection())


# =============================================================================
# Dev Quick-Restore: Accept pre-parsed source data (from browser cache)
# =============================================================================

@app.route("/api/restore", methods=["POST"])
def restore_sources():
    """Restore previously cached source data without re-uploading files.

    Accepts JSON: {sources: {"COA": {...}, "FAQ": {...}, "DataPool": {...}}}
    Each source has: filename, headers, rows, row_count
    """
    data = request.get_json()
    sources = data.get("sources", {})
    if not sources:
        return jsonify({"error": "No source data provided."}), 400

    store = get_store()
    store["sources"] = sources
    store["mapping"] = None  # will be recomputed
    store["diff_results"] = None

    src_info = {}
    for k, v in sources.items():
        src_info[k] = {"filename": v.get("filename"), "row_count": v.get("row_count", len(v.get("rows", [])))}

    return jsonify({"status": "restored", "sources": src_info})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting RDM 3-Way Diff app on port {port}")
    logger.info(f"MAX_CONTENT_LENGTH: {app.config['MAX_CONTENT_LENGTH']} bytes")
    app.run(host="0.0.0.0", port=port, debug=False)
