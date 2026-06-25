"""Diff Service — 3-way comparison engine.

Ports the JavaScript diff logic (runDiffSKB, runDiffFlat) to Python.
Uses pandas DataFrames for in-memory comparison and optional
Delta table persistence for large datasets.
"""

import pandas as pd
import sqlite3
from typing import Optional
from rapidfuzz import fuzz


# =============================================================================
# Embedded Mappings (ported from rdm-3way-diff-v6.html SKA_MAP / SKB_MAP)
# These define the canonical field mappings for the 3-way comparison.
# =============================================================================
SKA_EMBEDDED_MAP = {"version": "4.0", "table": "SKA", "description": "Chart-of-accounts level GL account master (SKA). Three-way comparison: COA Master vs FAQ (SAP) vs DataPool.", "comparison_grain": "gl_account", "fields": [{"canonical": "g_l_account", "label": "G/L Account", "is_key": True, "sources": {"COA": "cCoA  Account Number  CHK_KEY", "FAQ": "G/L Account", "DataPool": "gl_account"}}, {"canonical": "account_group", "label": "Account Group", "is_key": False, "sources": {"COA": "Account Group", "FAQ": "Account Group", "DataPool": "account_group"}}, {"canonical": "indicator_blocked_for_posting", "label": "Blocked for Posting", "is_key": False, "sources": {"COA": "Marked for deletion (ALL SYSTEMS)", "FAQ": "Blocked for Posting", "DataPool": "indicator_blocked_for_posting"}}, {"canonical": "chart_of_account", "label": "Chart of Accounts", "is_key": False, "sources": {"COA": "", "FAQ": "Chart of Accounts", "DataPool": "chart_of_account"}}, {"canonical": "functional_area_code", "label": "Functional Area", "is_key": False, "sources": {"COA": "Functional Area Code", "FAQ": "Functional Area", "DataPool": "functional_area_code"}}, {"canonical": "gl_acct_long_text", "label": "G/L Account Long Text", "is_key": False, "sources": {"COA": "G/L Acct Long Text", "FAQ": "G/L Account Long Text", "DataPool": "gl_acct_long_text"}}, {"canonical": "gl_account_subtype", "label": "G/L Account Subtype", "is_key": False, "sources": {"COA": "G/L Account Subtype", "FAQ": "G/L Account Subtype", "DataPool": "gl_account_subtype"}}, {"canonical": "gl_account_type", "label": "G/L Account Type", "is_key": False, "sources": {"COA": "G/L Account Type", "FAQ": "G/L Account Type", "DataPool": "gl_account_type"}}, {"canonical": "gl_account_external_id", "label": "G/L Acct External ID", "is_key": False, "sources": {"COA": "cCoA  Account Number  CHK_KEY", "FAQ": "G/L Acct External ID", "DataPool": "gl_account_external_id"}}, {"canonical": "group_account_number", "label": "Group Account Number", "is_key": False, "sources": {"COA": "Group Account Number", "FAQ": "Group Account Number", "DataPool": "group_account_number"}}, {"canonical": "indicator_mark_for_deletion", "label": "Marked for Deletion", "is_key": False, "sources": {"COA": "Marked for deletion (ALL SYSTEMS)", "FAQ": "Marked for Deletion", "DataPool": "indicator_mark_for_deletion"}}, {"canonical": "pl_statement_account_type", "label": "P&L State. Acct", "is_key": False, "sources": {"COA": "", "FAQ": "P&L State. Acct", "DataPool": "pl_statement_account_type"}}, {"canonical": "reconciliation_account_for_account_group", "label": "Reconciliation Acct", "is_key": False, "sources": {"COA": "Reconciliation Account for Account Group", "FAQ": "Reconciliation Acct", "DataPool": "reconciliation_account_for_account_group"}}, {"canonical": "short_text", "label": "Short Text", "is_key": False, "sources": {"COA": "Short Text", "FAQ": "Short Text", "DataPool": "short_text"}}, {"canonical": "trading_partner_number", "label": "Trading Partner No.", "is_key": False, "sources": {"COA": "Trading Partner Number", "FAQ": "Trading Partner No.", "DataPool": "trading_partner_number"}}]}

SKB_EMBEDDED_MAP = {"version": "4.0", "table": "SKB", "description": "Company-code level GL account master (SKB). Three-way comparison: COA-side (COA Master + CC-Matrix) vs FAQ (SAP) vs DataPool.", "comparison_grain": "account_company_code", "fields": [{"canonical": "gl_account_number", "label": "G/L Account", "is_key": True, "sources": {"COA": "CHK_KEY", "FAQ": "G/L Account", "DataPool": "gl_account_number"}}, {"canonical": "company_code", "label": "Company Code", "is_key": True, "sources": {"COA": "", "FAQ": "Company Code", "DataPool": "company_code"}}, {"canonical": "authorization_group", "label": "Authorization Group", "is_key": False, "sources": {"COA": "", "FAQ": "Authorization Group", "DataPool": "authorization_group"}}, {"canonical": "indicator_is_account_blocked_for_posting", "label": "Blocked for Posting", "is_key": False, "sources": {"COA": "Marked for deletion (ALL SYSTEMS)", "FAQ": "Blocked for Posting", "DataPool": "indicator_is_account_blocked_for_posting"}}, {"canonical": "field_status_group_harmonized", "label": "Field Status Group", "is_key": False, "sources": {"COA": "Field Status Group (Harmonized)", "FAQ": "Field Status Group", "DataPool": "field_status_group_harmonized"}}, {"canonical": "indicator_account_marked_for_deletion", "label": "Marked for Deletion", "is_key": False, "sources": {"COA": "Marked for deletion (ALL SYSTEMS)", "FAQ": "Marked for Deletion", "DataPool": "indicator_account_marked_for_deletion"}}, {"canonical": "open_item_management", "label": "Open Item Management", "is_key": False, "sources": {"COA": "Open Item Management", "FAQ": "Open Item Management", "DataPool": "open_item_management"}}, {"canonical": "open_item_management_by_ledger_group", "label": "Open Item Mgmt by Ledger Group", "is_key": False, "sources": {"COA": "Open Item Management by Ledger Group", "FAQ": "Open Item Mgmt by Ledger Group", "DataPool": "open_item_management_by_ledger_group"}}, {"canonical": "posting_without_tax_allowed", "label": "Posting Without Tax Allowed", "is_key": False, "sources": {"COA": "Posting Without tax allowed", "FAQ": "Posting Without Tax Allowed", "DataPool": "posting_without_tax_allowed"}}, {"canonical": "account_is_reconciliation_account", "label": "Recon. Account for Account Type", "is_key": False, "sources": {"COA": "Reconciliation Account for Account Group", "FAQ": "Recon. Account for Account Type", "DataPool": "account_is_reconciliation_account"}}, {"canonical": "sort_key", "label": "Sort Key", "is_key": False, "sources": {"COA": "Sort key (Harmonized)", "FAQ": "Sort Key", "DataPool": "sort_key"}}, {"canonical": "tax_category_in_account_master_record", "label": "Tax Category", "is_key": False, "sources": {"COA": "Tax Category", "FAQ": "Tax Category", "DataPool": "tax_category_in_account_master_record"}}]}


# =============================================================================
# Predefined Transforms (ported from rdm-3way-diff-v6.html)
# These normalize values BEFORE comparison to eliminate false conflicts.
# Format: {(canonical_field, source): transform_func}
# =============================================================================

def _tx_x_true_empty_false(v):
    """Treat X as TRUE and empty/blank as FALSE."""
    v = str(v).strip() if v else ""
    if v.upper() == "X":
        return "TRUE"
    if v in ("", "[blank]", "nan"):
        return "FALSE"
    return v

def _tx_blank_as_empty(v):
    """Treat [blank] literal string as empty."""
    v = str(v).strip() if v else ""
    if v in ("[blank]", "nan"):
        return ""
    return v

def _tx_blank_as_false(v):
    """Treat [blank] and empty as FALSE."""
    v = str(v).strip() if v else ""
    if v in ("", "[blank]", "nan"):
        return "FALSE"
    return v

def _tx_blank_false_x_true(v):
    """Treat [blank] as FALSE and X as TRUE."""
    v = str(v).strip() if v else ""
    if v.upper() == "X":
        return "TRUE"
    if v in ("", "[blank]", "nan"):
        return "FALSE"
    return v

def _tx_extract_before_space(v):
    """Extract digits and characters before first space."""
    v = str(v).strip() if v else ""
    if v and " " in v:
        return v.split(" ")[0]
    return v

def _tx_gl_account_subtype_faq(v):
    """Map FAQ G/L Account Subtype names to COA codes."""
    v = str(v).strip() if v else ""
    mapping = {
        "Bank Reconciliation Account": "B",
        "Petty Cash": "P",
        "Bank Subaccount": "P",
        "General": "",
        "Cash Account": "C",
    }
    return mapping.get(v, v)

def _tx_gl_account_type_faq(v):
    """Map FAQ G/L Account Type names to COA codes."""
    v = str(v).strip() if v else ""
    mapping = {
        "Balance Sheet Account": "X",
        "Cash Account": "C",
        "Nonoperating Expense or Income": "N",
        "Primary Costs or Revenue": "P",
        "Secondary Costs": "S",
    }
    return mapping.get(v, v)


# SKB predefined transforms: (canonical_field, source) -> (func, description)
SKB_TRANSFORMS = {
    ("indicator_is_account_blocked_for_posting", "COA"): (_tx_x_true_empty_false, "treat X as TRUE and empty value as FALSE"),
    ("indicator_account_marked_for_deletion", "COA"): (_tx_x_true_empty_false, "treat X as TRUE and empty as FALSE"),
    ("open_item_management", "COA"): (_tx_blank_as_false, "treat [blank] as FALSE"),
    ("open_item_management_by_ledger_group", "COA"): (_tx_blank_false_x_true, "treat [blank] as FALSE and X as TRUE"),
    ("posting_without_tax_allowed", "COA"): (_tx_blank_false_x_true, "treat [blank] as FALSE and X as TRUE"),
    ("account_is_reconciliation_account", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("tax_category_in_account_master_record", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("sort_key", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("field_status_group_harmonized", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("authorization_group", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("gl_account_number", "FAQ"): (_tx_extract_before_space, "extract digits and characters before first space"),
}

# SKA predefined transforms
SKA_TRANSFORMS = {
    ("indicator_blocked_for_posting", "COA"): (_tx_x_true_empty_false, "treat X as TRUE and empty value as FALSE"),
    ("indicator_mark_for_deletion", "COA"): (_tx_x_true_empty_false, "treat X as TRUE and empty as FALSE"),
    ("gl_account_subtype", "FAQ"): (_tx_gl_account_subtype_faq, "treat Bank Reconciliation Account as 'B', Petty Cash as 'P'"),
    ("gl_account_subtype", "COA"): (_tx_blank_as_empty, "treat [blank] as empty"),
    ("gl_account_type", "FAQ"): (_tx_gl_account_type_faq, "treat Balance Sheet Account as 'X', Cash Account as 'C'"),
    ("reconciliation_account_for_account_group", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("trading_partner_number", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("functional_area_code", "COA"): (_tx_blank_as_empty, "treat [blank] as empty value"),
    ("g_l_account", "FAQ"): (_tx_extract_before_space, "extract only digits and characters before first space"),
}


class DiffService:
    """Handles 3-way data comparison across COA, FAQ, and DataPool."""

    def __init__(self):
        self._diff_df: Optional[pd.DataFrame] = None
        self._db: Optional[sqlite3.Connection] = None
        self._mapping: Optional[dict] = None
        self._active_transforms: list = []  # Track which transforms were applied

    def compute_mapping(self, sources: dict, mode: str = "SKB") -> dict:
        """Compute column mapping using embedded map + fuzzy matching against uploaded headers.

        Uses the pre-defined SKA_MAP/SKB_MAP (ported from the original HTML app) as the
        canonical field definitions, then resolves actual column names from uploaded file
        headers using fuzzy matching.
        """
        all_headers = {}
        for src, info in sources.items():
            all_headers[src] = info.get("headers", [])

        # Select embedded map based on mode
        embedded = SKB_EMBEDDED_MAP if mode == "SKB" else SKA_EMBEDDED_MAP
        comparable_fields = []

        for field_def in embedded["fields"]:
            field = {
                "canonical": field_def["canonical"],
                "label": field_def["label"],
                "is_key": field_def.get("is_key", False),
                "sources": {},
            }

            # For each source, resolve the column name from the embedded map
            # against the actual uploaded headers using fuzzy matching
            for src in ["COA", "FAQ", "DataPool"]:
                expected_col = field_def["sources"].get(src, "")
                if not expected_col:
                    continue  # No mapping defined for this source

                if src not in all_headers or not all_headers[src]:
                    continue  # Source not uploaded

                # Try exact match first, then fuzzy
                actual_headers = all_headers[src]
                matched = self._resolve_column(expected_col, actual_headers)
                if matched:
                    field["sources"][src] = matched

            # Only include if at least 2 sources mapped
            if len(field["sources"]) >= 2:
                comparable_fields.append(field)

        self._mapping = {
            "comparable_fields": comparable_fields,
            "embedded_map": {
                "version": embedded["version"],
                "table": embedded["table"],
                "description": embedded["description"],
                "comparison_grain": embedded["comparison_grain"],
                "total_fields": len(embedded["fields"]),
            },
        }
        return self._mapping

    def _resolve_column(self, expected: str, headers: list) -> str:
        """Resolve an expected column name against actual headers.

        Uses a 3-tier approach:
        1. Exact normalized match
        2. Starts-with match (handles suffixes in COA files)
        3. Fuzzy similarity >= 55%
        """
        if not expected:
            return ""

        norm_expected = self._normalize(expected)

        # Tier 1: exact normalized match
        for h in headers:
            if self._normalize(h) == norm_expected:
                return h

        # Tier 2: header starts with expected (handles COA suffixes)
        for h in headers:
            norm_h = self._normalize(h)
            if norm_h.startswith(norm_expected + " ") or norm_h.startswith(norm_expected):
                return h

        # Tier 3: fuzzy match
        best_match, best_score = None, 0
        for h in headers:
            score = fuzz.ratio(norm_expected, self._normalize(h))
            if score > best_score:
                best_score = score
                best_match = h

        return best_match if best_score >= 55 else ""

    def run_diff(
        self,
        sources: dict,
        mapping: dict,
        mode: str = "SKB",
        options: dict = None,
        key_columns: dict = None,
        custom_transforms: dict = None,
        disabled_transforms: list = None,
    ) -> dict:
        """Run the 3-way comparison.

        Args:
            sources: Dict of source data {source_name: {headers, rows, ...}}.
            mapping: Column mapping result from compute_mapping().
            mode: 'SKA' (flat account) or 'SKB' (company-code level).
            options: Comparison options (case_sensitive, trim_whitespace, etc.).
            key_columns: Optional override {source: column_name} for key columns.
            custom_transforms: User-defined transforms {"field:source": {function_code, ...}}
            disabled_transforms: List of {"field", "source"} predefined transforms to skip.

        Returns:
            Dict with rows (diff results) and summary counts.
        """
        import logging
        import re
        logger = logging.getLogger(__name__)
        options = options or {}
        key_columns = key_columns or {}
        case_sensitive = options.get("case_sensitive", False)
        trim_ws = options.get("trim_whitespace", True)
        skip_yellow = options.get("skip_yellow", True)
        skip_ten_digit = options.get("skip_ten_digit", True)
        skip_strike = options.get("skip_strike", True)

        comparable_fields = mapping.get("comparable_fields", [])
        source_names = ["COA", "FAQ", "DataPool"]
        active_sources = [s for s in source_names if s in sources]

        # =====================================================================
        # KEY COLUMN RESOLUTION — Critical for row matching
        # Priority: 1) UI-selected key_columns  2) Embedded mapping (is_key=True)
        # =====================================================================
        embedded = SKB_EMBEDDED_MAP if mode == "SKB" else SKA_EMBEDDED_MAP
        key_field_defs = [f for f in embedded["fields"] if f.get("is_key")]

        # Resolve key column names per source
        resolved_key_cols = {}  # {src: [col1, col2, ...]}
        for src in active_sources:
            headers = sources[src].get("headers", [])
            if src in key_columns and key_columns[src]:
                # UI override — user explicitly selected key column
                resolved_key_cols[src] = [key_columns[src]]
            else:
                # Use embedded mapping key fields
                cols = []
                for kf in key_field_defs:
                    expected = kf["sources"].get(src, "")
                    if expected:
                        resolved = self._resolve_column(expected, headers)
                        if resolved:
                            cols.append(resolved)
                resolved_key_cols[src] = cols if cols else [headers[0]] if headers else []

        logger.info(f"Key columns resolved: {resolved_key_cols}")

        # =====================================================================
        # BUILD KEYED LOOKUPS — composite key = concat of key column values
        # Apply row filters: skip_yellow, skip_strike (metadata from upload),
        # and skip_ten_digit (account number must be exactly 10 digits)
        # =====================================================================
        source_dfs = {}
        source_key_maps = {}  # {src: {composite_key: row_index}}

        for src in active_sources:
            info = sources[src]
            df = pd.DataFrame(info.get("rows", []), columns=info.get("headers", []))
            key_cols = resolved_key_cols.get(src, [])

            # Build composite key for each row
            if key_cols:
                # Concatenate key column values with | separator
                key_series = df[key_cols[0]].astype(str).str.strip()
                for kc in key_cols[1:]:
                    if kc in df.columns:
                        key_series = key_series + "|" + df[kc].astype(str).str.strip()
                df["_composite_key"] = key_series
            else:
                df["_composite_key"] = df.index.astype(str)

            # -----------------------------------------------------------------
            # ROW FILTERING — Applied at comparison time (not upload time)
            # -----------------------------------------------------------------
            initial_count = len(df)

            # Skip non-10-digit accounts: The FIRST key column (account number)
            # must be exactly 10 digits. This filters out header/group rows.
            if skip_ten_digit and key_cols:
                acct_col = key_cols[0]  # First key column = account number
                if acct_col in df.columns:
                    # Keep only rows where the account column is exactly 10 digits
                    ten_digit_mask = df[acct_col].astype(str).str.strip().str.fullmatch(r'\d{10}', na=False)
                    df = df[ten_digit_mask.fillna(False)].copy()

            # Skip yellow rows (only applicable to COA source)
            # Note: Yellow-row filtering during upload already removes these rows
            # in file_service.py. This is a secondary check.
            # skip_strike: same — already filtered at upload time.

            filtered_count = len(df)
            if filtered_count < initial_count:
                logger.info(f"{src}: Filtered {initial_count - filtered_count} rows (10-digit filter). {filtered_count} rows remain.")

            source_dfs[src] = df
            # Build key→row-indices lookup (handle duplicate keys by taking first)
            source_key_maps[src] = {}
            for idx, key_val in enumerate(df["_composite_key"]):
                if key_val not in source_key_maps[src]:
                    source_key_maps[src][key_val] = idx

        # Collect all unique keys across sources
        all_keys = set()
        for km in source_key_maps.values():
            all_keys.update(km.keys())

        logger.info(f"Total unique keys: {len(all_keys)}, per source: {[(s, len(m)) for s, m in source_key_maps.items()]}")


        # =====================================================================
        # PREDEFINED + CUSTOM TRANSFORMS — applied before value comparison
        # =====================================================================
        all_predefined = SKB_TRANSFORMS if mode == "SKB" else SKA_TRANSFORMS

        # Build set of disabled predefined transforms (user removed them via UI)
        disabled_transforms = disabled_transforms or []
        disabled_set = set()
        for d in disabled_transforms:
            disabled_set.add((d.get("field", ""), d.get("source", "")))

        # Filter out disabled predefined transforms
        transforms = {
            k: v for k, v in all_predefined.items()
            if k not in disabled_set
        }

        # Compile custom user-defined transforms into executable functions
        custom_transforms = custom_transforms or {}
        custom_tx_fns = {}  # {(field, source): compiled_fn}
        for tx_key, tx_info in custom_transforms.items():
            func_code = tx_info.get("function_code", "")
            if func_code:
                try:
                    ns = {}
                    exec(f"fn = {func_code}", ns)
                    parts = tx_key.split(":")
                    custom_tx_fns[(parts[0], parts[1])] = ns["fn"]
                except Exception as ex:
                    logger.warning(f"Failed to compile custom transform {tx_key}: {ex}")

        # Build active transforms list for API response
        self._active_transforms = [
            {"field": k[0], "source": k[1], "instruction": v[1]}
            for k, v in transforms.items()
        ]
        for tx_key, tx_info in custom_transforms.items():
            parts = tx_key.split(":")
            self._active_transforms.append({
                "field": parts[0], "source": parts[1],
                "instruction": tx_info.get("instruction", "custom"),
                "is_custom": True,
            })
        logger.info(f"Transforms: {len(transforms)} predefined ({len(disabled_set)} disabled) + {len(custom_tx_fns)} custom")


        # =====================================================================
        # 3-WAY COMPARISON
        # =====================================================================
        diff_rows = []
        counts = {"same": 0, "conflict": 0, "onlyCOA": 0, "onlyFAQ": 0, "onlyDP": 0, "total": 0}

        for key in sorted(all_keys):
            row_data = {"key": key, "vals": {}, "field_conflicts": []}
            present_in = [s for s in active_sources if key in source_key_maps[s]]

            if len(present_in) == 1:
                src = present_in[0]
                if src == "COA":
                    counts["onlyCOA"] += 1
                elif src == "FAQ":
                    counts["onlyFAQ"] += 1
                else:
                    counts["onlyDP"] += 1
                row_data["dtype"] = f"only_{src}"

                # Still populate vals for the source that has data
                row_idx = source_key_maps[src][key]
                for field in comparable_fields:
                    col = field["sources"].get(src)
                    if col and col in source_dfs[src].columns:
                        val = str(source_dfs[src].iloc[row_idx][col])
                        if val == "nan":
                            val = ""
                        row_data["vals"][field["canonical"]] = {src: val}
                    else:
                        row_data["vals"][field["canonical"]] = {}
                row_data["field_conflicts"] = [False] * len(comparable_fields)
            else:
                # Compare field values across sources
                has_conflict = False
                for fi, field in enumerate(comparable_fields):
                    values = {}
                    for src in present_in:
                        col = field["sources"].get(src)
                        if col and col in source_dfs[src].columns:
                            row_idx = source_key_maps[src][key]
                            val = str(source_dfs[src].iloc[row_idx][col])
                            if val == "nan":
                                val = ""
                            # Apply predefined transform BEFORE comparison
                            tx_key = (field["canonical"], src)
                            if tx_key in transforms:
                                val = transforms[tx_key][0](val)
                            # Apply custom user-defined transform (overrides predefined)
                            if tx_key in custom_tx_fns:
                                try:
                                    val = custom_tx_fns[tx_key](val)
                                except Exception:
                                    pass
                            if val and trim_ws:
                                val = val.strip()
                            if val and not case_sensitive:
                                val = val.lower()
                            values[src] = val

                    row_data["vals"][field["canonical"]] = values

                    # Check if values differ (ignore empty/None)
                    non_empty = [v for v in values.values() if v]
                    unique_vals = set(non_empty)
                    is_conflict = len(unique_vals) > 1
                    row_data["field_conflicts"].append(is_conflict)
                    if is_conflict:
                        has_conflict = True

                row_data["dtype"] = "conflict" if has_conflict else "same"
                counts["conflict" if has_conflict else "same"] += 1

            counts["total"] += 1
            diff_rows.append(row_data)

        # Store results and build SQLite for chat queries
        self._diff_df = pd.DataFrame(diff_rows)
        self._build_sqlite(diff_rows, comparable_fields, active_sources)

        return {
            "rows": diff_rows[:250],  # First page
            "summary": counts,
            "total_rows": len(diff_rows),
            "page_size": 250,
            "comparable_fields": [{"canonical": f["canonical"], "label": f["label"]} for f in comparable_fields],
            "active_transforms": self._active_transforms,
        }

    def _build_sqlite(self, rows: list, fields: list, sources: list):
        """Build in-memory SQLite table for Text-to-SQL queries.

        Replaces the sql.js in-browser database from the original app.
        """
        self._db = sqlite3.connect(":memory:")
        cursor = self._db.cursor()

        # Build column definitions with safe names
        col_defs = ["dtype TEXT", "key TEXT"]
        for f in fields:
            safe_name = self._safe_col_name(f['canonical'])
            for src in sources:
                col_defs.append(f"\"{src.lower()}_{safe_name}\" TEXT")
            col_defs.append(f"\"conflict_{safe_name}\" INTEGER")

        create_sql = f"CREATE TABLE diff_results ({', '.join(col_defs)})"
        cursor.execute(create_sql)
        cursor.execute("CREATE INDEX idx_dtype ON diff_results(dtype)")
        cursor.execute("CREATE INDEX idx_key ON diff_results(key)")

        # Insert rows
        for row in rows:
            vals = [row.get("dtype", ""), row.get("key", "")]
            for fi, f in enumerate(fields):
                field_vals = row.get("vals", {}).get(f["canonical"], {})
                for src in sources:
                    vals.append(field_vals.get(src))
                conflicts = row.get("field_conflicts", [])
                vals.append(1 if fi < len(conflicts) and conflicts[fi] else 0)

            placeholders = ",".join(["?"] * len(vals))
            cursor.execute(f"INSERT INTO diff_results VALUES ({placeholders})", vals)

        self._db.commit()

    def execute_sql(self, sql: str) -> dict:
        """Execute SQL query against the diff_results table."""
        if not self._db:
            return {"error": "No comparison results available. Run a comparison first."}

        try:
            cursor = self._db.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return {
                "columns": columns,
                "rows": [list(r) for r in rows],
                "row_count": len(rows),
                "sql": sql,
            }
        except Exception as e:
            return {"error": str(e), "sql": sql}

    def export(self, format_type: str = "csv", conflicts_only: bool = False) -> dict:
        """Export comparison results."""
        if self._diff_df is None or self._diff_df.empty:
            return {"error": "No results to export"}

        df = self._diff_df
        if conflicts_only:
            df = df[df["dtype"] == "conflict"]

        # For now, return as JSON (actual file download handled by frontend)
        return {
            "row_count": len(df),
            "format": format_type,
            "data": df.to_dict(orient="records")[:1000],
        }

    @staticmethod
    def _normalize(s: str) -> str:
        """Normalize string for fuzzy matching."""
        import re
        return re.sub(r"[^a-z0-9 ]", "", s.lower().strip())

    @staticmethod
    def _safe_col_name(s: str) -> str:
        """Create a safe SQLite column name from a canonical string.

        - Replace spaces with underscores
        - Prefix with 'f_' if starts with a digit (SQLite can't handle unquoted digit-leading identifiers)
        - Remove any remaining non-alphanumeric/underscore chars
        """
        import re
        name = re.sub(r"[^a-z0-9_]", "_", s.lower().strip())
        name = re.sub(r"_+", "_", name).strip("_")  # Collapse multiple underscores
        if name and name[0].isdigit():
            name = "f_" + name
        return name or "field"