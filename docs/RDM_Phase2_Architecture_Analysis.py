# Databricks notebook source
# DBTITLE 1,Executive Summary
# MAGIC %md
# MAGIC # RDM Phase 2 — Architecture Analysis & Recommendation
# MAGIC
# MAGIC ## 1. Executive Summary
# MAGIC
# MAGIC The Reference Data Reconciliation (RDM) application currently operates as an **interactive, session-based Flask app** that:
# MAGIC - Accepts manually uploaded Excel/CSV files from three sources (COA, FAQ/SAP, DataPool)
# MAGIC - Applies embedded field mappings (SKA/SKB) with fuzzy column resolution
# MAGIC - Runs pandas-based 3-way comparison with configurable transforms
# MAGIC - Stores results in an in-memory SQLite database for AI agent queries
# MAGIC - Provides LLM-powered analysis via Databricks Foundation Model API
# MAGIC - Supports Jira ticket creation for escalated conflicts
# MAGIC
# MAGIC **Key Finding:** The existing comparison engine is well-architected for its current purpose (\~30K rows, 12-15 fields, 3 sources). The primary evolution path is to **decouple data ingestion from the interactive session** while preserving the UI and AI capabilities.
# MAGIC
# MAGIC **Recommended Approach:** Option 2 — Move ingestion, standardization, and scheduled comparison into Databricks Jobs; the app becomes a results viewer + ad-hoc re-comparison tool.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Critical Metrics from Current Codebase
# MAGIC
# MAGIC | Component | Current State | Phase 2 Target |
# MAGIC |-----------|--------------|----------------|
# MAGIC | Data Input | Manual upload (Excel/CSV/XLSB) | Automated ingestion from SharePoint, SAP OData, DataPool |
# MAGIC | Comparison Engine | In-app pandas (in-memory SQLite) | Dual: scheduled Spark job + on-demand in-app |
# MAGIC | Result Storage | Session memory (lost on restart) | Delta tables with run_id, audit trail |
# MAGIC | Field Mappings | Hardcoded `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` | UC-managed config table |
# MAGIC | Transforms | Python functions in `diff_service.py` | Reusable module, version-controlled |
# MAGIC | AI Agent | Session-scoped SQLite queries | Delta table queries via Databricks SQL |
# MAGIC | Run History | None | Full lineage: input snapshot → transform → result |

# COMMAND ----------

# DBTITLE 1,Current-State Architecture
# MAGIC %md
# MAGIC ## 2. Current-State Interpretation
# MAGIC
# MAGIC ### Architecture Diagram (Current)
# MAGIC
# MAGIC ```
# MAGIC ┌─────────────────────────────────────────────────────────────────┐
# MAGIC │                    DATABRICKS APP (Flask)                         │
# MAGIC │                                                                   │
# MAGIC │  ┌──────────┐    ┌──────────────┐    ┌────────────────┐          │
# MAGIC │  │  Upload  │───▶│  FileService │───▶│   DiffService  │          │
# MAGIC │  │  (UI)    │    │  (parse)     │    │   (compare)    │          │
# MAGIC │  └──────────┘    └──────────────┘    └───────┬────────┘          │
# MAGIC │                                              │                    │
# MAGIC │                                              ▼                    │
# MAGIC │                                       ┌──────────┐               │
# MAGIC │                                       │  SQLite  │ (in-memory)   │
# MAGIC │                                       │ diff_results│              │
# MAGIC │                                       └─────┬────┘               │
# MAGIC │                                             │                     │
# MAGIC │                      ┌──────────────────────┼──────────────┐     │
# MAGIC │                      ▼                      ▼              ▼     │
# MAGIC │               ┌────────────┐        ┌────────────┐  ┌──────────┐│
# MAGIC │               │ LLM Agent  │        │   Export   │  │   Jira   ││
# MAGIC │               │ (tool-call)│        │  (xlsx/csv)│  │  Service ││
# MAGIC │               └────────────┘        └────────────┘  └──────────┘│
# MAGIC └─────────────────────────────────────────────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC ### Key Observations
# MAGIC
# MAGIC 1. **Stateless but session-bound**: All data lives in `_sessions` dict (Python process memory). App restart = data loss.
# MAGIC 2. **Comparison logic is pure Python/pandas**: No Spark dependency. Works for current scale (\~50K rows max).
# MAGIC 3. **Embedded mappings are sophisticated**: SKA (15 fields), SKB (12 fields) with predefined transforms, fuzzy column resolution, and custom user transforms.
# MAGIC 4. **SQLite is the query layer**: The LLM agent uses SQLite for text-to-SQL. This is elegant but ephemeral.
# MAGIC 5. **No run history or audit trail**: Each comparison is fire-and-forget.
# MAGIC 6. **Volume path defined but unused for persistence**: `/Volumes/data_mesh_hub/rdm/uploads` exists in config but only used for temporary file storage.
# MAGIC
# MAGIC ### Source Data Characteristics (from embedded mappings)
# MAGIC
# MAGIC | Source | Format | Key Fields | Peculiarities |
# MAGIC |--------|--------|------------|---------------|
# MAGIC | COA (SharePoint) | Excel (.xlsx/.xlsb) | Account Number (10-digit) | Yellow rows = excluded, strikethrough = deleted |
# MAGIC | FAQ (SAP) | Excel/CSV | G/L Account + Company Code | Values use descriptive names (need mapping to codes) |
# MAGIC | DataPool | Excel/CSV | gl_account / gl_account_number | Already uses snake_case canonical names |

# COMMAND ----------

# DBTITLE 1,Target-State Architecture
# MAGIC %md
# MAGIC ## 3. Target-State Architecture
# MAGIC
# MAGIC ### Proposed Architecture (Phase 2)
# MAGIC
# MAGIC ```
# MAGIC ┌─────────────────────────────────────────────────────────────────────────┐
# MAGIC │                         DATABRICKS PLATFORM                               │
# MAGIC │                                                                           │
# MAGIC │  ┌─────────────────────────── INGESTION LAYER ──────────────────────┐    │
# MAGIC │  │                                                                    │    │
# MAGIC │  │  ┌──────────┐   ┌──────────┐   ┌──────────────┐                  │    │
# MAGIC │  │  │SharePoint│   │ SAP OData│   │  DataPool    │                  │    │
# MAGIC │  │  │  (Graph  │   │  (REST)  │   │  (ADLS/API)  │                  │    │
# MAGIC │  │  │   API)   │   │          │   │              │                  │    │
# MAGIC │  │  └─────┬────┘   └────┬─────┘   └──────┬───────┘                  │    │
# MAGIC │  │        │              │                │                           │    │
# MAGIC │  │        ▼              ▼                ▼                           │    │
# MAGIC │  │  ┌──────────────────────────────────────────────┐                 │    │
# MAGIC │  │  │         Landing / Raw (UC Volume)             │                 │    │
# MAGIC │  │  │   /Volumes/data_mesh_hub/rdm/raw/            │                 │    │
# MAGIC │  │  │     coa/2026-06-27/coa_master.xlsx           │                 │    │
# MAGIC │  │  │     sap/2026-06-27/faq_extract.json          │                 │    │
# MAGIC │  │  │     datapool/2026-06-27/gl_accounts.parquet  │                 │    │
# MAGIC │  │  └──────────────────────┬───────────────────────┘                 │    │
# MAGIC │  └─────────────────────────┼─────────────────────────────────────────┘    │
# MAGIC │                            │                                              │
# MAGIC │  ┌─────────────────────────┼───── PROCESSING LAYER ──────────────────┐   │
# MAGIC │  │                         ▼                                          │   │
# MAGIC │  │  ┌───────────────────────────────────────────────┐                 │   │
# MAGIC │  │  │          Bronze (Delta Tables)                 │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.bronze_coa_master         │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.bronze_sap_faq            │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.bronze_datapool_gl        │                 │   │
# MAGIC │  │  └──────────────────────┬────────────────────────┘                 │   │
# MAGIC │  │                         │  Normalize / Standardize                 │   │
# MAGIC │  │                         ▼                                          │   │
# MAGIC │  │  ┌───────────────────────────────────────────────┐                 │   │
# MAGIC │  │  │          Silver (Delta Tables)                 │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_ska_coa            │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_ska_faq            │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_ska_datapool       │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_skb_coa            │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_skb_faq            │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.silver_skb_datapool       │                 │   │
# MAGIC │  │  └──────────────────────┬────────────────────────┘                 │   │
# MAGIC │  │                         │  Compare (3-way diff)                    │   │
# MAGIC │  │                         ▼                                          │   │
# MAGIC │  │  ┌───────────────────────────────────────────────┐                 │   │
# MAGIC │  │  │          Gold / Results (Delta Tables)         │                 │   │
# MAGIC │  │  │   data_mesh_hub.rdm.reconciliation_runs       │  (metadata)     │   │
# MAGIC │  │  │   data_mesh_hub.rdm.reconciliation_results    │  (row-level)    │   │
# MAGIC │  │  │   data_mesh_hub.rdm.reconciliation_summary    │  (field stats)  │   │
# MAGIC │  │  │   data_mesh_hub.rdm.field_mappings            │  (config)       │   │
# MAGIC │  │  │   data_mesh_hub.rdm.transform_registry        │  (config)       │   │
# MAGIC │  │  └──────────────────────┬────────────────────────┘                 │   │
# MAGIC │  └─────────────────────────┼─────────────────────────────────────────┘   │
# MAGIC │                            │                                              │
# MAGIC │  ┌─────────────────────────┼───── PRESENTATION LAYER ────────────────┐   │
# MAGIC │  │                         ▼                                          │   │
# MAGIC │  │  ┌────────────────────────────────────────────────────────────┐    │   │
# MAGIC │  │  │              DATABRICKS APP (Flask)                          │    │   │
# MAGIC │  │  │                                                              │    │   │
# MAGIC │  │  │  Mode A: Results Viewer (reads Gold Delta tables)            │    │   │
# MAGIC │  │  │  Mode B: Ad-hoc Comparison (existing upload flow retained)   │    │   │
# MAGIC │  │  │  Mode C: AI Agent (queries Delta instead of SQLite)          │    │   │
# MAGIC │  │  │                                                              │    │   │
# MAGIC │  │  └────────────────────────────────────────────────────────────┘    │   │
# MAGIC │  │                                                                    │   │
# MAGIC │  │  ┌────────────────┐    ┌───────────────┐    ┌──────────────────┐  │   │
# MAGIC │  │  │ Notifications  │    │  Jira         │    │  Genie / AI      │  │   │
# MAGIC │  │  │ (email/Teams)  │    │  Integration  │    │  (SQL endpoint)  │  │   │
# MAGIC │  │  └────────────────┘    └───────────────┘    └──────────────────┘  │   │
# MAGIC │  └────────────────────────────────────────────────────────────────────┘   │
# MAGIC └───────────────────────────────────────────────────────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC ### Unity Catalog Structure
# MAGIC
# MAGIC ```sql
# MAGIC -- New schema for RDM
# MAGIC CREATE SCHEMA IF NOT EXISTS data_mesh_hub.rdm;
# MAGIC
# MAGIC -- Volume for raw file landing
# MAGIC CREATE VOLUME IF NOT EXISTS data_mesh_hub.rdm.raw;
# MAGIC CREATE VOLUME IF NOT EXISTS data_mesh_hub.rdm.uploads;  -- existing, for ad-hoc
# MAGIC
# MAGIC -- Tables created by the processing pipeline (see Section 8)
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,File Format Recommendation
# MAGIC %md
# MAGIC ## 4. File Format Recommendation
# MAGIC
# MAGIC ### Comparative Analysis
# MAGIC
# MAGIC | Criterion | JSON | CSV/Excel | Parquet | **Delta Lake** |
# MAGIC |-----------|------|-----------|---------|----------------|
# MAGIC | Read Performance | ❌ Parse overhead | ❌ No predicate pushdown | ✅ Columnar, fast | ✅✅ Columnar + data skipping |
# MAGIC | Join Performance | ❌ Needs conversion | ❌ Needs conversion | ✅ Good | ✅✅ Z-ORDER, statistics |
# MAGIC | Schema Enforcement | ❌ None | ❌ None | ⚠️ At write time | ✅✅ Enforced + evolution |
# MAGIC | Schema Evolution | ✅ Flexible | ❌ Manual | ⚠️ Limited | ✅✅ ADD/RENAME/MERGE |
# MAGIC | Nested Structures | ✅ Native | ❌ Flat only | ✅ Supported | ✅ Supported |
# MAGIC | Auditability | ❌ No history | ❌ No history | ❌ No history | ✅✅ Time travel, CDF |
# MAGIC | Data Lineage | ❌ Manual | ❌ Manual | ❌ Manual | ✅✅ UC lineage |
# MAGIC | Incremental Processing | ❌ Full scan | ❌ Full scan | ⚠️ Partition-based | ✅✅ CDF, MERGE |
# MAGIC | Databricks SQL | ❌ Not queryable | ❌ Not queryable | ✅ Queryable | ✅✅ Full SQL + serverless |
# MAGIC | Databricks Apps | ❌ Needs SDK code | ❌ Needs SDK code | ⚠️ Via Spark only | ✅✅ Via SQL Connector |
# MAGIC | Long-term Maintenance | ❌ Brittle | ❌ Manual effort | ✅ Stable | ✅✅ VACUUM, OPTIMIZE |
# MAGIC
# MAGIC ### Recommendation
# MAGIC
# MAGIC **All processing and comparison MUST use Delta Lake tables.** Raw formats are only acceptable in the landing zone.
# MAGIC
# MAGIC | Layer | Format | Rationale |
# MAGIC |-------|--------|-----------|
# MAGIC | Landing/Raw | Original format (JSON, Excel, Parquet) | Preserve source fidelity, enable re-processing |
# MAGIC | Bronze | Delta | Schema enforcement, audit trail, time travel |
# MAGIC | Silver | Delta | Normalized schema, enables SQL access |
# MAGIC | Gold/Results | Delta | Query by Databricks SQL, Genie, Apps, notebooks |
# MAGIC
# MAGIC **SAP JSON specifically:** Must be flattened to tabular Delta at Bronze. Nested structures are acceptable in raw landing only. The SKA/SKB fields are already defined as flat canonical fields — JSON nesting adds no value for comparison.
# MAGIC
# MAGIC **DataPool files:** If already Parquet, the Bronze step is a simple `COPY INTO` or `read_files()` → Delta. Minimal transformation needed.

# COMMAND ----------

# DBTITLE 1,Required Changes to Existing Application
# MAGIC %md
# MAGIC ## 5. Required Changes to Existing Application
# MAGIC
# MAGIC ### Change Classification
# MAGIC
# MAGIC #### Mandatory Changes (Required for Phase 2)
# MAGIC
# MAGIC | # | Component | Current | Target | Effort |
# MAGIC |---|-----------|---------|--------|--------|
# MAGIC | 1 | **Data Access Layer** | Session `_sessions` dict | Read from Delta tables via `databricks-sql-connector` or Spark Connect | Medium |
# MAGIC | 2 | **Results persistence** | In-memory SQLite (lost on restart) | Write/read `reconciliation_results` Delta table | Medium |
# MAGIC | 3 | **Run management** | None | `reconciliation_runs` table with run_id, timestamp, status, source_versions | Low |
# MAGIC | 4 | **App startup mode** | Always starts empty | Load latest run results on startup ("Results Viewer" mode) | Low |
# MAGIC | 5 | **LLM Agent query backend** | SQLite `diff_results` | Databricks SQL endpoint (query Delta directly) | Medium |
# MAGIC
# MAGIC #### Recommended Enhancements
# MAGIC
# MAGIC | # | Component | Current | Target | Effort |
# MAGIC |---|-----------|---------|--------|--------|
# MAGIC | 6 | **Dual-mode operation** | Upload-only | Mode A: View scheduled results / Mode B: Ad-hoc upload comparison | Medium |
# MAGIC | 7 | **Field mappings externalized** | Hardcoded `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` | UC table `field_mappings` (version-controlled, editable) | Low |
# MAGIC | 8 | **Transform registry** | Python functions in `diff_service.py` | UC table `transform_registry` + Python module | Low |
# MAGIC | 9 | **Run comparison** | N/A | Compare current run vs previous run (trend detection) | Medium |
# MAGIC | 10 | **Notification trigger** | N/A | After scheduled comparison, notify on threshold breach | Low |
# MAGIC
# MAGIC #### Optional Future Improvements
# MAGIC
# MAGIC | # | Component | Description | Effort |
# MAGIC |---|-----------|-------------|--------|
# MAGIC | 11 | **Spark-based comparison** | Replace pandas with PySpark for datasets > 1M rows | High |
# MAGIC | 12 | **Streaming ingestion** | Auto-Loader for new files arriving in ADLS | Medium |
# MAGIC | 13 | **Multi-entity support** | Beyond SKA/SKB: cost centers, profit centers, etc. | Medium |
# MAGIC | 14 | **Approval workflow** | User marks conflict as "accepted" / "escalated" in app → status persisted | Low |
# MAGIC | 15 | **Data quality rules** | Great Expectations or DLT expectations on Bronze/Silver | Medium |
# MAGIC
# MAGIC ### What Remains Unchanged
# MAGIC
# MAGIC - **Upload flow** (`file_service.py`): Retained as-is for ad-hoc mode
# MAGIC - **Comparison engine** (`diff_service.py`): Core logic stays; add Delta output option
# MAGIC - **UI** (`index.html`, JS, CSS): Minimal changes (add "View Results" tab)
# MAGIC - **Jira integration** (`jira_service.py`): Works as-is; add run_id to ticket metadata
# MAGIC - **LLM agent tools**: Same tool definitions; swap SQLite backend for SQL endpoint

# COMMAND ----------

# DBTITLE 1,Databricks Jobs Workflow Design
# MAGIC %md
# MAGIC ## 6. Databricks Jobs / Workflow Design
# MAGIC
# MAGIC ### Recommended Orchestration: Multi-Task Job
# MAGIC
# MAGIC ```
# MAGIC ┌─────────────────────────────────────────────────────────────────┐
# MAGIC │              JOB: rdm_reconciliation_pipeline                     │
# MAGIC │              Schedule: Daily 06:00 UTC (weekdays)                 │
# MAGIC │              Cluster: Serverless or Job Cluster (4-8 cores)       │
# MAGIC │                                                                   │
# MAGIC │  ┌────────────┐  ┌────────────┐  ┌────────────────┐             │
# MAGIC │  │ Task 1     │  │ Task 2     │  │ Task 3         │             │
# MAGIC │  │ Ingest COA │  │ Ingest SAP │  │ Ingest DataPool│             │
# MAGIC │  │ (SharePoint│  │ (OData API)│  │ (ADLS/API)     │             │
# MAGIC │  │  Graph API)│  │            │  │                │             │
# MAGIC │  └─────┬──────┘  └─────┬──────┘  └───────┬────────┘             │
# MAGIC │        │                │                 │                       │
# MAGIC │        └────────────────┼─────────────────┘                       │
# MAGIC │                         │  (all succeed)                          │
# MAGIC │                         ▼                                         │
# MAGIC │  ┌──────────────────────────────────────────────┐                │
# MAGIC │  │ Task 4: Validate Ingestion Completeness       │                │
# MAGIC │  │   - Row count thresholds                      │                │
# MAGIC │  │   - Schema drift detection                    │                │
# MAGIC │  │   - Freshness check                           │                │
# MAGIC │  └──────────────────────┬───────────────────────┘                │
# MAGIC │                         │                                         │
# MAGIC │                         ▼                                         │
# MAGIC │  ┌──────────────────────────────────────────────┐                │
# MAGIC │  │ Task 5: Normalize & Standardize (Bronze→Silver)│               │
# MAGIC │  │   - Apply column renames (field_mappings table)│                │
# MAGIC │  │   - Apply transforms (transform_registry)      │               │
# MAGIC │  │   - Produce silver_ska_*, silver_skb_* tables  │               │
# MAGIC │  └──────────────────────┬───────────────────────┘                │
# MAGIC │                         │                                         │
# MAGIC │                         ▼                                         │
# MAGIC │  ┌──────────────────────────────────────────────┐                │
# MAGIC │  │ Task 6: Execute 3-Way Comparison              │                │
# MAGIC │  │   - Read silver tables                        │                │
# MAGIC │  │   - Apply comparison logic (adapted from      │                │
# MAGIC │  │     diff_service.run_diff)                    │                │
# MAGIC │  │   - Persist to reconciliation_results         │                │
# MAGIC │  │   - Generate reconciliation_summary           │                │
# MAGIC │  └──────────────────────┬───────────────────────┘                │
# MAGIC │                         │                                         │
# MAGIC │                         ▼                                         │
# MAGIC │  ┌──────────────────────────────────────────────┐                │
# MAGIC │  │ Task 7: Post-Processing                       │                │
# MAGIC │  │   - Update reconciliation_runs status         │                │
# MAGIC │  │   - Classify discrepancies (severity)         │                │
# MAGIC │  │   - Compare with previous run (new conflicts) │                │
# MAGIC │  └──────────────────────┬───────────────────────┘                │
# MAGIC │                         │                                         │
# MAGIC │                         ▼                                         │
# MAGIC │  ┌──────────────────────────────────────────────┐                │
# MAGIC │  │ Task 8: Notify & Update                       │                │
# MAGIC │  │   - Send email/Teams if threshold breached    │                │
# MAGIC │  │   - Update app-visible status flag            │                │
# MAGIC │  │   - (Optional) Auto-create Jira for critical  │                │
# MAGIC │  └──────────────────────────────────────────────┘                │
# MAGIC └─────────────────────────────────────────────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC ### Task Implementation Mapping
# MAGIC
# MAGIC | Task | Implementation | Source |
# MAGIC |------|---------------|--------|
# MAGIC | 1-3 (Ingest) | Python notebooks using `requests` (Graph API, OData) | New notebooks |
# MAGIC | 4 (Validate) | Python notebook with assertions / DQ checks | New notebook |
# MAGIC | 5 (Normalize) | Python/SQL notebook applying `field_mappings` table | New notebook (reuses transform logic) |
# MAGIC | 6 (Compare) | Python notebook importing `diff_service` module | **Reuse existing module** |
# MAGIC | 7 (Post-process) | SQL/Python notebook | New notebook |
# MAGIC | 8 (Notify) | Python notebook (email via SMTP/Graph, Teams webhook) | New notebook |
# MAGIC
# MAGIC ### Cluster Recommendation
# MAGIC
# MAGIC - **Tasks 1-4**: Serverless compute (lightweight HTTP calls + file parsing)
# MAGIC - **Tasks 5-7**: Job cluster with 4-8 workers (Spark for large datasets) OR Serverless SQL (if < 5M rows)
# MAGIC - **Task 8**: Serverless compute (API calls only)
# MAGIC
# MAGIC ### Scheduling
# MAGIC
# MAGIC | Scenario | Schedule |
# MAGIC |----------|----------|
# MAGIC | Normal operations | Weekdays 06:00 UTC |
# MAGIC | Month-end close | Daily including weekends |
# MAGIC | Ad-hoc | Manual trigger from app UI or Databricks Jobs UI |

# COMMAND ----------

# DBTITLE 1,Comparison Logic Refactoring
# MAGIC %md
# MAGIC ## 7. Comparison Logic Refactoring Approach
# MAGIC
# MAGIC ### Recommendation: Hybrid Model
# MAGIC
# MAGIC The comparison logic should exist in **two modes**:
# MAGIC
# MAGIC | Mode | Use Case | Engine | Data Source | Result Storage |
# MAGIC |------|----------|--------|-------------|----------------|
# MAGIC | **Scheduled** | Daily automated reconciliation | PySpark (Delta → Delta) | Silver Delta tables | Gold Delta tables |
# MAGIC | **Ad-hoc** | User uploads files, immediate comparison | Pandas (as today) | In-memory DataFrames | Delta table + session |
# MAGIC
# MAGIC ### Refactoring Strategy
# MAGIC
# MAGIC **Step 1: Extract reusable module** (Week 1-2)
# MAGIC
# MAGIC ```
# MAGIC src/
# MAGIC ├── rdm_app/              (Flask app — unchanged)
# MAGIC │   └── services/
# MAGIC │       └── diff_service.py  (imports from shared module)
# MAGIC └── rdm_core/             (NEW — shared comparison library)
# MAGIC     ├── __init__.py
# MAGIC     ├── mappings.py       (SKA_EMBEDDED_MAP, SKB_EMBEDDED_MAP)
# MAGIC     ├── transforms.py     (all transform functions)
# MAGIC     ├── comparator.py     (comparison logic — works with DataFrames OR Spark DFs)
# MAGIC     └── persistence.py    (Delta table read/write helpers)
# MAGIC ```
# MAGIC
# MAGIC **Step 2: Delta output adapter** (Week 2-3)
# MAGIC
# MAGIC After comparison, persist results to Delta:
# MAGIC
# MAGIC ```python
# MAGIC # reconciliation_results table schema
# MAGIC schema = StructType([
# MAGIC     StructField("run_id", StringType()),        # UUID per execution
# MAGIC     StructField("run_timestamp", TimestampType()),
# MAGIC     StructField("mode", StringType()),           # SKA or SKB
# MAGIC     StructField("key_value", StringType()),      # composite key
# MAGIC     StructField("dtype", StringType()),          # same/conflict/only_COA/only_FAQ/only_DP
# MAGIC     StructField("field_canonical", StringType()),# which field
# MAGIC     StructField("coa_value", StringType()),
# MAGIC     StructField("faq_value", StringType()),
# MAGIC     StructField("datapool_value", StringType()),
# MAGIC     StructField("is_conflict", BooleanType()),
# MAGIC     StructField("severity", StringType()),       # critical/high/medium/low
# MAGIC     StructField("resolution_status", StringType()),  # open/accepted/escalated/resolved
# MAGIC     StructField("resolved_by", StringType()),
# MAGIC     StructField("jira_key", StringType()),
# MAGIC ])
# MAGIC ```
# MAGIC
# MAGIC **Step 3: Spark implementation** (Week 3-4)
# MAGIC
# MAGIC Port the pandas comparison to PySpark for scheduled runs:
# MAGIC - Joins replace key-map lookups (broadcast join for < 100K rows)
# MAGIC - Transforms apply via UDFs or `CASE WHEN` expressions
# MAGIC - Conflict detection via column-level comparison
# MAGIC
# MAGIC ### Key Decisions
# MAGIC
# MAGIC | Decision | Recommendation | Rationale |
# MAGIC |----------|---------------|-----------|
# MAGIC | Keep pandas in app? | **Yes** | Fast for ad-hoc (< 60K rows), no Spark startup overhead |
# MAGIC | Move scheduled to Spark? | **Yes** | Enables > 1M rows, Delta native, job cluster |
# MAGIC | Store each run? | **Yes** | `run_id` partitioning enables trend analysis |
# MAGIC | Support incremental comparison? | **Phase 2.1** | First get full comparison working; then add CDF-based incremental |
# MAGIC | Manual rerun from UI? | **Yes** | Trigger Databricks Job via API from app button |

# COMMAND ----------

# DBTITLE 1,Storage and Table Design
# MAGIC %md
# MAGIC ## 8. Storage and Table Design Recommendation
# MAGIC
# MAGIC ### Schema: `data_mesh_hub.rdm`
# MAGIC
# MAGIC #### Configuration Tables
# MAGIC
# MAGIC ```sql
# MAGIC -- Field mappings (replaces hardcoded SKA_EMBEDDED_MAP / SKB_EMBEDDED_MAP)
# MAGIC CREATE TABLE data_mesh_hub.rdm.field_mappings (
# MAGIC   mapping_id STRING,
# MAGIC   version STRING,
# MAGIC   mode STRING,               -- 'SKA' or 'SKB'
# MAGIC   canonical_field STRING,
# MAGIC   label STRING,
# MAGIC   is_key BOOLEAN,
# MAGIC   source_name STRING,        -- 'COA', 'FAQ', 'DataPool'
# MAGIC   source_column STRING,      -- expected column name in source
# MAGIC   active BOOLEAN DEFAULT TRUE,
# MAGIC   updated_at TIMESTAMP,
# MAGIC   updated_by STRING
# MAGIC );
# MAGIC
# MAGIC -- Transform registry (replaces hardcoded SKA_TRANSFORMS / SKB_TRANSFORMS)
# MAGIC CREATE TABLE data_mesh_hub.rdm.transform_registry (
# MAGIC   transform_id STRING,
# MAGIC   mode STRING,
# MAGIC   canonical_field STRING,
# MAGIC   source_name STRING,
# MAGIC   transform_type STRING,     -- 'predefined' or 'custom'
# MAGIC   function_name STRING,      -- e.g. '_tx_x_true_empty_false'
# MAGIC   instruction STRING,        -- human-readable description
# MAGIC   function_code STRING,      -- for custom: Python lambda/function code
# MAGIC   active BOOLEAN DEFAULT TRUE,
# MAGIC   updated_at TIMESTAMP
# MAGIC );
# MAGIC ```
# MAGIC
# MAGIC #### Bronze Tables (Source-Aligned)
# MAGIC
# MAGIC ```sql
# MAGIC -- Bronze: raw ingested data with minimal transformation
# MAGIC CREATE TABLE data_mesh_hub.rdm.bronze_coa_master (
# MAGIC   _ingestion_id STRING,
# MAGIC   _ingestion_timestamp TIMESTAMP,
# MAGIC   _source_file STRING,
# MAGIC   -- All source columns preserved as STRING (schema-on-read)
# MAGIC   ...dynamic columns...
# MAGIC ) USING DELTA
# MAGIC PARTITIONED BY (_ingestion_id);
# MAGIC
# MAGIC CREATE TABLE data_mesh_hub.rdm.bronze_sap_faq (...);
# MAGIC CREATE TABLE data_mesh_hub.rdm.bronze_datapool_gl (...);
# MAGIC ```
# MAGIC
# MAGIC #### Silver Tables (Standardized)
# MAGIC
# MAGIC ```sql
# MAGIC -- Silver: canonical schema, transforms applied
# MAGIC CREATE TABLE data_mesh_hub.rdm.silver_ska_coa (
# MAGIC   ingestion_id STRING,
# MAGIC   g_l_account STRING,        -- 10-digit, canonical
# MAGIC   account_group STRING,
# MAGIC   indicator_blocked_for_posting STRING,  -- already transformed (X→TRUE, empty→FALSE)
# MAGIC   chart_of_account STRING,
# MAGIC   functional_area_code STRING,
# MAGIC   gl_acct_long_text STRING,
# MAGIC   gl_account_subtype STRING,
# MAGIC   gl_account_type STRING,
# MAGIC   gl_account_external_id STRING,
# MAGIC   group_account_number STRING,
# MAGIC   indicator_mark_for_deletion STRING,
# MAGIC   pl_statement_account_type STRING,
# MAGIC   reconciliation_account_for_account_group STRING,
# MAGIC   short_text STRING,
# MAGIC   trading_partner_number STRING
# MAGIC ) USING DELTA;
# MAGIC
# MAGIC -- Similar for silver_ska_faq, silver_ska_datapool,
# MAGIC -- silver_skb_coa, silver_skb_faq, silver_skb_datapool
# MAGIC ```
# MAGIC
# MAGIC #### Gold / Results Tables
# MAGIC
# MAGIC ```sql
# MAGIC -- Run metadata
# MAGIC CREATE TABLE data_mesh_hub.rdm.reconciliation_runs (
# MAGIC   run_id STRING,
# MAGIC   run_timestamp TIMESTAMP,
# MAGIC   mode STRING,                 -- 'SKA' or 'SKB'
# MAGIC   trigger_type STRING,         -- 'scheduled' / 'manual' / 'adhoc_upload'
# MAGIC   triggered_by STRING,         -- user email or 'system'
# MAGIC   status STRING,               -- 'running' / 'completed' / 'failed'
# MAGIC   total_keys INT,
# MAGIC   matching_keys INT,
# MAGIC   conflict_keys INT,
# MAGIC   coa_only_keys INT,
# MAGIC   faq_only_keys INT,
# MAGIC   dp_only_keys INT,
# MAGIC   match_percentage DOUBLE,
# MAGIC   source_coa_version STRING,   -- ingestion_id or filename
# MAGIC   source_faq_version STRING,
# MAGIC   source_dp_version STRING,
# MAGIC   duration_seconds INT,
# MAGIC   error_message STRING,
# MAGIC   completed_at TIMESTAMP
# MAGIC ) USING DELTA;
# MAGIC
# MAGIC -- Row-level results (partitioned by run_id for efficient querying)
# MAGIC CREATE TABLE data_mesh_hub.rdm.reconciliation_results (
# MAGIC   run_id STRING,
# MAGIC   mode STRING,
# MAGIC   key_value STRING,
# MAGIC   dtype STRING,                -- 'same'/'conflict'/'only_COA'/'only_FAQ'/'only_DP'
# MAGIC   field_canonical STRING,
# MAGIC   field_label STRING,
# MAGIC   coa_value STRING,
# MAGIC   faq_value STRING,
# MAGIC   datapool_value STRING,
# MAGIC   is_conflict BOOLEAN,
# MAGIC   severity STRING,             -- derived from business rules
# MAGIC   resolution_status STRING DEFAULT 'open',
# MAGIC   resolved_by STRING,
# MAGIC   resolved_at TIMESTAMP,
# MAGIC   jira_key STRING
# MAGIC ) USING DELTA
# MAGIC PARTITIONED BY (run_id);
# MAGIC
# MAGIC -- Aggregated field-level summary per run
# MAGIC CREATE TABLE data_mesh_hub.rdm.reconciliation_summary (
# MAGIC   run_id STRING,
# MAGIC   mode STRING,
# MAGIC   field_canonical STRING,
# MAGIC   field_label STRING,
# MAGIC   conflict_count INT,
# MAGIC   total_records INT,
# MAGIC   conflict_percentage DOUBLE,
# MAGIC   severity_critical INT,
# MAGIC   severity_high INT,
# MAGIC   severity_medium INT,
# MAGIC   severity_low INT
# MAGIC ) USING DELTA;
# MAGIC ```
# MAGIC
# MAGIC ### Data Lifecycle
# MAGIC
# MAGIC | Layer | Retention | VACUUM | Purpose |
# MAGIC |-------|-----------|--------|---------|
# MAGIC | Raw (Volume) | 90 days | N/A | Re-processing if needed |
# MAGIC | Bronze | 30 days history | 7 days | Audit, reprocessing |
# MAGIC | Silver | 30 days history | 7 days | Intermediate, debugging |
# MAGIC | Gold (Results) | Indefinite | 30 days | Business reporting, trend analysis |
# MAGIC | Runs | Indefinite | N/A | Full audit trail |

# COMMAND ----------

# DBTITLE 1,AI / Genie / Knowledge Base Enablement
# MAGIC %md
# MAGIC ## 9. AI / Genie / Knowledge Base Enablement
# MAGIC
# MAGIC ### Immediate Opportunities (Phase 2.0)
# MAGIC
# MAGIC | Capability | Data Asset | Implementation | Effort |
# MAGIC |-----------|-----------|----------------|--------|
# MAGIC | Natural language Q&A on results | `reconciliation_results` + `reconciliation_summary` | **Genie Space** on Delta tables | Low |
# MAGIC | "Which fields have most conflicts?" | `reconciliation_summary` | SQL query via Genie or in-app agent | Low |
# MAGIC | "Show me new conflicts since last run" | `reconciliation_results` (compare run_ids) | SQL window functions | Low |
# MAGIC | Explanation of WHY records mismatch | `reconciliation_results` + `transform_registry` | In-app LLM with context | Already exists |
# MAGIC | Summary generation for stakeholders | `reconciliation_runs` + `reconciliation_summary` | LLM summarization task in Job | Low |
# MAGIC
# MAGIC ### Near-Term Opportunities (Phase 2.1)
# MAGIC
# MAGIC | Capability | Data Asset | Implementation | Effort |
# MAGIC |-----------|-----------|----------------|--------|
# MAGIC | Root cause analysis | Historical results (multi-run) | LLM + pattern detection on conflict trends | Medium |
# MAGIC | Suggested conflict resolution | `resolution_status` history | Few-shot learning from past resolutions | Medium |
# MAGIC | Pattern detection | Time-series of field conflict rates | Statistical anomaly detection + LLM narrative | Medium |
# MAGIC | Recommendations for next best action | All gold tables + Jira tickets | RAG over resolution history | Medium |
# MAGIC
# MAGIC ### Future Opportunities (Phase 3)
# MAGIC
# MAGIC | Capability | Requirement | Implementation |
# MAGIC |-----------|-------------|----------------|
# MAGIC | Predictive conflict detection | 6+ months of run history | ML model on conflict patterns |
# MAGIC | Automated resolution proposals | User confirmation data on past resolutions | Fine-tuned model or prompt engineering |
# MAGIC | Cross-domain impact analysis | Multiple entity types (SKA, SKB, cost centers, etc.) | Knowledge graph on Delta |
# MAGIC
# MAGIC ### Genie Space Design
# MAGIC
# MAGIC ```
# MAGIC Genie Space: "RDM Reconciliation Results"
# MAGIC Tables:
# MAGIC   - data_mesh_hub.rdm.reconciliation_runs
# MAGIC   - data_mesh_hub.rdm.reconciliation_results
# MAGIC   - data_mesh_hub.rdm.reconciliation_summary
# MAGIC   - data_mesh_hub.rdm.field_mappings
# MAGIC
# MAGIC Sample Questions:
# MAGIC   - "How many conflicts were found in the latest SKA run?"
# MAGIC   - "Which fields have the highest conflict rate this month?"
# MAGIC   - "Show me the trend of match percentage over the last 10 runs"
# MAGIC   - "List all open critical conflicts not yet assigned to Jira"
# MAGIC   - "Compare today's run with last week's run"
# MAGIC ```
# MAGIC
# MAGIC ### In-App Agent Evolution
# MAGIC
# MAGIC The current LLM agent queries SQLite. In Phase 2, swap the backend:
# MAGIC
# MAGIC | Current | Phase 2 |
# MAGIC |---------|----------|
# MAGIC | `diff_service.execute_sql(sql)` on SQLite | `sql_connector.execute(sql)` on Databricks SQL endpoint |
# MAGIC | Tool: `query_diff_results` | Tool: `query_reconciliation_results` |
# MAGIC | Context: session-scoped data | Context: all historical runs |
# MAGIC | Scope: current comparison only | Scope: cross-run analysis, trends, history |

# COMMAND ----------

# DBTITLE 1,Notification and Jira Workflow
# MAGIC %md
# MAGIC ## 10. Notification and Jira Workflow
# MAGIC
# MAGIC ### Notification Design
# MAGIC
# MAGIC #### When to Notify
# MAGIC
# MAGIC | Trigger | Notification | Channel |
# MAGIC |---------|-------------|----------|
# MAGIC | Run completes with match% < threshold (e.g. 95%) | Alert: "Reconciliation below threshold" | Email + Teams |
# MAGIC | New critical conflicts detected (not in previous run) | Alert: "X new critical conflicts" | Email + Teams |
# MAGIC | Run fails | Error: "Reconciliation pipeline failed" | Email to pipeline owner |
# MAGIC | Run completes, all good | No notification | — (avoid noise) |
# MAGIC | First run of the week (Monday summary) | Digest: weekly trend | Email |
# MAGIC
# MAGIC #### Notification Content
# MAGIC
# MAGIC ```
# MAGIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAGIC ⚠️ RDM Reconciliation Alert
# MAGIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAGIC Run:       2026-06-27 06:00 UTC (SKB mode)
# MAGIC Match:     92.3% (threshold: 95%)
# MAGIC Conflicts: 847 records across 12 fields
# MAGIC New since last run: +23 conflicts
# MAGIC
# MAGIC Top conflict fields:
# MAGIC   • Field Status Group: 234 conflicts (27.6%)
# MAGIC   • Sort Key: 189 conflicts (22.3%)
# MAGIC   • Open Item Management: 156 conflicts (18.4%)
# MAGIC
# MAGIC 🔗 View in RDM App:
# MAGIC https://rdm-app-test.azuredatabricks.net/?run_id=abc123
# MAGIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAGIC ```
# MAGIC
# MAGIC #### Implementation
# MAGIC
# MAGIC | Method | Pros | Cons | Recommendation |
# MAGIC |--------|------|------|----------------|
# MAGIC | Databricks SQL Alert | Native, low-code | Limited formatting | Good for simple thresholds |
# MAGIC | Python (SMTP) in Job task | Full control, rich HTML | Needs SMTP relay | ✅ **Recommended** |
# MAGIC | Microsoft Teams Webhook | Easy, visible | No email fallback | Use in addition to email |
# MAGIC | Power Automate | Enterprise-grade | Separate platform | Consider for Phase 3 |
# MAGIC
# MAGIC ### Jira Workflow
# MAGIC
# MAGIC #### Current State (Already Working)
# MAGIC
# MAGIC The existing `JiraService` creates stories with:
# MAGIC - Conflict field name and rate
# MAGIC - Sample conflict rows (table in Jira description)
# MAGIC - CSV attachment with all conflict rows
# MAGIC - App link for full investigation
# MAGIC - Labels: `rdm-conflict`, `data-quality`, `automated`
# MAGIC
# MAGIC #### Phase 2 Enhancements
# MAGIC
# MAGIC | Enhancement | Description | Trigger |
# MAGIC |-------------|-------------|----------|
# MAGIC | Auto-create for critical | If severity=critical AND conflict_rate > 30%, auto-create Jira | Job task (system-triggered) |
# MAGIC | User-triggered from app | User reviews conflict → clicks "Create Jira" (existing flow) | App UI (user-triggered) |
# MAGIC | Link to run_id | Jira ticket includes `run_id` for traceability | Both |
# MAGIC | Resolution tracking | When Jira status changes → update `resolution_status` in Delta | Jira webhook → Databricks API |
# MAGIC | De-duplication | Don't create duplicate tickets for same field+key if open ticket exists | Jira search before create |
# MAGIC
# MAGIC #### Information in Jira Tickets
# MAGIC
# MAGIC ```
# MAGIC [Auto-created fields]
# MAGIC - Summary: [RDM] Data conflict: {field} — {count} of {total} records differ
# MAGIC - Priority: Critical/High/Medium/Low (based on conflict rate + business rules)
# MAGIC - Labels: rdm-conflict, data-quality, automated, {mode}, {run_date}
# MAGIC - Description: conflict report + sample data + app link
# MAGIC - Attachment: Full conflict CSV
# MAGIC - Custom fields (if configured):
# MAGIC   - RDM Run ID
# MAGIC   - Conflict Rate %
# MAGIC   - Sources Compared
# MAGIC   - First Detected Date
# MAGIC ```
# MAGIC
# MAGIC #### Avoid Notification Noise
# MAGIC
# MAGIC 1. **Threshold-based**: Only notify if match% drops below configurable threshold
# MAGIC 2. **Delta-based**: Only notify on NEW conflicts (not previously seen)
# MAGIC 3. **Cooldown**: Don't re-notify for same field within 24 hours
# MAGIC 4. **Digest mode**: Option for daily digest instead of per-run alerts
# MAGIC 5. **Severity filter**: Only notify for critical/high by default

# COMMAND ----------

# DBTITLE 1,Governance and Security
# MAGIC %md
# MAGIC ## 11. Governance and Security Considerations
# MAGIC
# MAGIC ### UAT vs Production Requirements
# MAGIC
# MAGIC | Control | UAT (Current) | Production (Target) |
# MAGIC |---------|--------------|---------------------|
# MAGIC | **Unity Catalog access** | User identity (PAT) | Service Principal with minimal grants |
# MAGIC | **App access control** | All workspace users | Group-based (`rdm-tool-users`) via app permissions |
# MAGIC | **Source data sensitivity** | Internal financial reference data | Same + audit logging |
# MAGIC | **Business Partner exclusion** | Not applicable (GL accounts only) | Monitor if scope expands |
# MAGIC | **Audit logging** | App server logs only | `reconciliation_runs` + UC audit logs |
# MAGIC | **Secrets management** | Databricks Secret Scope | Same (already using `github-secrets`) |
# MAGIC | **Service Principal** | `sp-data-comparer-deploy` for CI/CD | Add `sp-rdm-pipeline` for scheduled jobs |
# MAGIC | **Network isolation** | Standard workspace | Consider Private Endpoints for SAP/SharePoint |
# MAGIC | **Data encryption** | At-rest (ADLS default) | At-rest + in-transit (TLS) |
# MAGIC
# MAGIC ### Unity Catalog Permissions Design
# MAGIC
# MAGIC ```sql
# MAGIC -- Schema-level grants
# MAGIC GRANT USE SCHEMA ON data_mesh_hub.rdm TO `rdm-tool-users`;
# MAGIC GRANT SELECT ON SCHEMA data_mesh_hub.rdm TO `rdm-tool-users`;
# MAGIC
# MAGIC -- Pipeline SP needs write access
# MAGIC GRANT USE SCHEMA ON data_mesh_hub.rdm TO `sp-rdm-pipeline`;
# MAGIC GRANT CREATE TABLE ON SCHEMA data_mesh_hub.rdm TO `sp-rdm-pipeline`;
# MAGIC GRANT MODIFY ON SCHEMA data_mesh_hub.rdm TO `sp-rdm-pipeline`;
# MAGIC
# MAGIC -- Volume access
# MAGIC GRANT READ VOLUME ON VOLUME data_mesh_hub.rdm.raw TO `sp-rdm-pipeline`;
# MAGIC GRANT WRITE VOLUME ON VOLUME data_mesh_hub.rdm.raw TO `sp-rdm-pipeline`;
# MAGIC GRANT READ VOLUME ON VOLUME data_mesh_hub.rdm.uploads TO `rdm-tool-users`;
# MAGIC GRANT WRITE VOLUME ON VOLUME data_mesh_hub.rdm.uploads TO `rdm-tool-users`;
# MAGIC ```
# MAGIC
# MAGIC ### App Security Model
# MAGIC
# MAGIC | Layer | Mechanism | Notes |
# MAGIC |-------|-----------|-------|
# MAGIC | Authentication | Databricks SSO (via X-Forwarded-Email header) | Already implemented |
# MAGIC | Authorization | App permissions (CAN_USE grant to group) | Configured in `rdm_app.app.yml` |
# MAGIC | Data access from app | App runs as SP → UC permissions on SP | SP identity determines table access |
# MAGIC | User actions (Jira) | Logged with user email from proxy headers | Already implemented |
# MAGIC | Secret access | Databricks Secret Scope (`github-secrets`) | App SP needs scope access |
# MAGIC
# MAGIC ### Lineage and Audit
# MAGIC
# MAGIC - **UC Lineage**: Automatic for Delta tables created by Spark jobs
# MAGIC - **Custom lineage**: `reconciliation_runs` tracks source_version → result mapping
# MAGIC - **User actions**: App logs all compare/export/jira actions with user identity
# MAGIC - **Time travel**: Delta 30-day history enables point-in-time investigation

# COMMAND ----------

# DBTITLE 1,Architecture Options Comparison
# MAGIC %md
# MAGIC ## 12. Architecture Options Comparison
# MAGIC
# MAGIC ### Option 1: Minimal Change (File-Based Replacement)
# MAGIC
# MAGIC **Description:** Replace manual uploads with pre-staged files in UC Volume. The app reads from Volume instead of user upload. Comparison still runs inside the app.
# MAGIC
# MAGIC | Aspect | Assessment |
# MAGIC |--------|------------|
# MAGIC | **Changes required** | Ingestion notebooks deposit files to Volume; `FileService` reads from Volume path instead of upload | 
# MAGIC | **Benefits** | Minimal code changes; fast to implement; app logic unchanged |
# MAGIC | **Drawbacks** | No run history; results still ephemeral; no audit trail; app restart = data loss; can't support > 1 user comparing simultaneously; no notifications |
# MAGIC | **Complexity** | Low |
# MAGIC | **Maintainability** | Poor — still session-bound, no persistence, no scheduled execution |
# MAGIC | **Recommended for** | Quick PoC only; NOT suitable for production reconciliation |
# MAGIC
# MAGIC ### Option 2: Full Separation (Jobs + App as Viewer) ✅ RECOMMENDED
# MAGIC
# MAGIC **Description:** Ingestion, standardization, and scheduled comparison run as Databricks Jobs. Results persist in Delta tables. The app reads results from Delta and retains ad-hoc upload capability.
# MAGIC
# MAGIC | Aspect | Assessment |
# MAGIC |--------|------------|
# MAGIC | **Changes required** | New rdm schema + tables; ingestion notebooks; comparison job (reuses diff logic); app adds Delta read mode |
# MAGIC | **Benefits** | Full audit trail; run history; trend analysis; Genie-ready; supports notifications; survives app restart; enables multiple users; production-grade |
# MAGIC | **Drawbacks** | More development effort; requires schema design; job monitoring |
# MAGIC | **Complexity** | Medium |
# MAGIC | **Maintainability** | Excellent — clear separation of concerns, UC-governed, version-controlled |
# MAGIC | **Recommended for** | ✅ Production reconciliation, enterprise UAT, team collaboration |
# MAGIC
# MAGIC ### Option 3: Full Platform (SDP / Streaming)
# MAGIC
# MAGIC **Description:** Use Lakeflow Spark Declarative Pipelines (SDP) with streaming tables, materialized views, and expectations for the entire flow.
# MAGIC
# MAGIC | Aspect | Assessment |
# MAGIC |--------|------------|
# MAGIC | **Changes required** | Rewrite all logic as SDP pipeline; streaming tables for Bronze; MVs for Silver/Gold |
# MAGIC | **Benefits** | Built-in DQ expectations; automatic retry; lineage; optimized incremental processing |
# MAGIC | **Drawbacks** | Over-engineered for batch reference data (changes weekly/monthly); steep learning curve; harder to debug comparison logic; less flexible for ad-hoc |
# MAGIC | **Complexity** | High |
# MAGIC | **Maintainability** | Good for streaming workloads; overkill for weekly reference data reconciliation |
# MAGIC | **Recommended for** | Only if data changes frequently (hourly/real-time) — NOT the case here |
# MAGIC
# MAGIC ### Final Comparison Matrix
# MAGIC
# MAGIC | Criterion | Option 1 | **Option 2** ✅ | Option 3 |
# MAGIC |-----------|----------|------------|----------|
# MAGIC | Implementation effort | 1 week | 4-6 weeks | 8-10 weeks |
# MAGIC | Run history | ❌ | ✅ | ✅ |
# MAGIC | Audit trail | ❌ | ✅ | ✅ |
# MAGIC | Notification support | ❌ | ✅ | ✅ |
# MAGIC | Genie/AI enablement | ❌ | ✅ | ✅ |
# MAGIC | Ad-hoc comparison | ✅ | ✅ | ⚠️ Limited |
# MAGIC | Multi-user support | ❌ | ✅ | ✅ |
# MAGIC | Production readiness | ❌ | ✅ | ✅ |
# MAGIC | Data freshness | File-based | Scheduled (daily) | Near real-time |
# MAGIC | Suits reference data workload | ⚠️ | ✅ | ❌ Over-engineered |

# COMMAND ----------

# DBTITLE 1,Implementation Roadmap
# MAGIC %md
# MAGIC ## 13. Recommended Implementation Roadmap
# MAGIC
# MAGIC ### Phase 2.0 — Foundation (Weeks 1-4)
# MAGIC
# MAGIC | Week | Deliverable | Tasks |
# MAGIC |------|-------------|-------|
# MAGIC | **1** | UC Schema + Tables | Create `data_mesh_hub.rdm` schema; create all Gold tables (runs, results, summary); create config tables (field_mappings, transform_registry) |
# MAGIC | **1** | Seed config tables | Migrate `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` → `field_mappings`; migrate transforms → `transform_registry` |
# MAGIC | **2** | Extract `rdm_core` module | Refactor `diff_service.py` into importable module; create Delta persistence adapter |
# MAGIC | **2** | Comparison Job (notebook) | Create notebook that reads Silver tables → runs comparison → writes to Gold |
# MAGIC | **3** | Ingestion notebooks (COA) | SharePoint Graph API → Raw Volume → Bronze Delta |
# MAGIC | **3** | Ingestion notebooks (SAP) | SAP OData → Raw Volume (JSON) → Bronze Delta (flattened) |
# MAGIC | **4** | Normalization notebook | Bronze → Silver (apply field_mappings + transform_registry) |
# MAGIC | **4** | Job orchestration | Create multi-task job with dependency chain |
# MAGIC
# MAGIC ### Phase 2.1 — App Integration (Weeks 5-6)
# MAGIC
# MAGIC | Week | Deliverable | Tasks |
# MAGIC |------|-------------|-------|
# MAGIC | **5** | App reads Delta results | Add "Results Viewer" mode; app queries `reconciliation_results` via SQL connector |
# MAGIC | **5** | Run selector UI | Dropdown to select run_id; show run metadata (match%, timestamp) |
# MAGIC | **6** | LLM agent on Delta | Swap SQLite backend for Databricks SQL endpoint queries |
# MAGIC | **6** | Manual rerun trigger | Button in app → triggers Databricks Job via API |
# MAGIC
# MAGIC ### Phase 2.2 — Notifications & Actions (Weeks 7-8)
# MAGIC
# MAGIC | Week | Deliverable | Tasks |
# MAGIC |------|-------------|-------|
# MAGIC | **7** | Email notifications | Task 8 in job: send email on threshold breach |
# MAGIC | **7** | Teams webhook | Post conflict summary to Teams channel |
# MAGIC | **8** | Jira enhancements | Auto-create for critical; de-duplication; run_id in tickets |
# MAGIC | **8** | Resolution tracking | User marks conflicts in app → updates Delta table |
# MAGIC
# MAGIC ### Phase 2.3 — AI & Analytics (Weeks 9-10)
# MAGIC
# MAGIC | Week | Deliverable | Tasks |
# MAGIC |------|-------------|-------|
# MAGIC | **9** | Genie Space | Create Genie space over Gold tables |
# MAGIC | **9** | Trend dashboard | AI/BI dashboard showing match% trend, top fields, run history |
# MAGIC | **10** | Cross-run analysis | Agent tool: compare two runs, highlight new/resolved conflicts |
# MAGIC | **10** | Weekly digest | Automated summary email with trend + recommendations |
# MAGIC
# MAGIC ### Immediate Next Steps (This Sprint)
# MAGIC
# MAGIC 1. ✅ **Complete current deployment** (push wheels, validate CI/CD on test workspace)
# MAGIC 2. Create `data_mesh_hub.rdm` schema and volume in test workspace
# MAGIC 3. Create Gold tables DDL (can run from UC_Bootstrap notebook)
# MAGIC 4. Seed `field_mappings` table from existing `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP`
# MAGIC 5. Create first ingestion notebook (start with DataPool — simplest source)

# COMMAND ----------

# DBTITLE 1,Open Questions and Decisions Needed
# MAGIC %md
# MAGIC ## 14. Open Questions / Decisions Needed
# MAGIC
# MAGIC | # | Question | Impact | Default Recommendation |
# MAGIC |---|----------|--------|------------------------|
# MAGIC | 1 | **SharePoint access method**: Graph API vs direct ADLS mount vs Power Automate export? | Ingestion architecture | Graph API (direct, no intermediary) |
# MAGIC | 2 | **SAP extraction**: OData API vs RFC/BAPI vs scheduled SAP extract to ADLS? | Ingestion complexity | OData API if available; else scheduled extract |
# MAGIC | 3 | **DataPool format**: Is it already available as Parquet in ADLS, or needs extraction? | Bronze complexity | Confirm with DataPool team |
# MAGIC | 4 | **Comparison frequency**: Daily? Weekly? On-demand only? | Job scheduling | Daily for production; weekly sufficient for UAT |
# MAGIC | 5 | **Match threshold for alerts**: What % triggers notification? | Notification design | 95% (configurable) |
# MAGIC | 6 | **Severity classification rules**: How to categorize critical vs high vs medium? | Result enrichment | Critical: key fields (account group, type); High: posting-relevant; Medium: descriptive |
# MAGIC | 7 | **UC catalog ownership**: Can `sp-rdm-pipeline` get CREATE TABLE on `data_mesh_hub`? | Permissions | Needs catalog owner (`data-mesh-cicd` SP) to grant |
# MAGIC | 8 | **Multi-entity scope**: Phase 2 includes cost centers, profit centers beyond SKA/SKB? | Schema design | Start with SKA/SKB only; design for extensibility |
# MAGIC | 9 | **Jira project**: Dedicated RDM project or shared data quality project? | Jira configuration | Dedicated: cleaner queries, better reporting |
# MAGIC | 10 | **User resolution workflow**: Accept/Escalate/Suppress — what statuses are needed? | Result table schema | open → acknowledged → escalated → resolved → suppressed |
# MAGIC | 11 | **Historical retention**: How long to keep reconciliation results? | Storage cost | Indefinite for Gold; 90 days for Bronze/Silver |
# MAGIC | 12 | **Test workspace readiness**: Is `data_mesh_hub` catalog available in `dbx-dps-raise-dev`? | Deployment | Run UC_Bootstrap notebook in test workspace first |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## Summary
# MAGIC
# MAGIC **Go with Option 2**: Jobs handle ingestion + scheduled comparison; App becomes a dual-mode viewer (scheduled results + ad-hoc uploads). Delta Lake throughout. Genie space for self-service analytics. Notifications on threshold breach only.
# MAGIC
# MAGIC The existing `diff_service.py` comparison logic is solid — extract it as a shared module, don't rewrite it. The app UI needs minimal changes (add a results viewer tab). The biggest new work is ingestion notebooks and the job orchestration.
# MAGIC
# MAGIC **Total estimated effort**: 8-10 weeks for a single developer, with Phase 2.0 (foundation) deliverable in 4 weeks.

# COMMAND ----------

# DBTITLE 1,ADDENDUM: Corrected Assumptions
# MAGIC %md
# MAGIC ---
# MAGIC
# MAGIC ## ADDENDUM: Corrected Assumptions & Refined Design Focus
# MAGIC
# MAGIC > **Updated 2026-06-27** — Based on clarification of actual current state.
# MAGIC
# MAGIC ### Key Corrections to Initial Analysis
# MAGIC
# MAGIC | Assumption in Original | Actual State |
# MAGIC |----------------------|---------------|
# MAGIC | SharePoint/COA data must be ingested from scratch | **Already implemented** — Graph API ingestion exists, data persisted as Excel |
# MAGIC | SAP data requires new extraction | **Already ingested** — persisted as JSON |
# MAGIC | DataPool ingestion undefined | **Partially defined** — expected JSON/structured → Parquet |
# MAGIC | Existing comparison logic can be extracted and reused | **Cannot be reused without significant refactoring** — currently operates on in-memory pandas DataFrames built from file uploads |
# MAGIC | Ingestion is the biggest new work | **Ingestion exists** — biggest work is comparison engine refactoring + UI decoupling |
# MAGIC
# MAGIC ### Revised Design Priorities
# MAGIC
# MAGIC 1. **Parquet/Delta Standardization** — Convert existing JSON/Excel landing data to structured Delta
# MAGIC 2. **Spark-Based Comparison Engine** — Replace pandas in-memory comparison with distributed Spark/SQL logic
# MAGIC 3. **UI-Backend Decoupling** — App reads results from Delta, never executes comparison
# MAGIC 4. **Interface Contract** — API-driven results delivery via `databricks-sql-connector`, not session-based
# MAGIC
# MAGIC ### Why Existing Comparison Logic Cannot Be Reused
# MAGIC
# MAGIC | Current Design | Problem for Production |
# MAGIC |---------------|------------------------|
# MAGIC | Sequential Python loop O(N×F) | Works at 30K rows; OOM at 500K+ |
# MAGIC | Session-bound `_sessions` dict | Cannot run as scheduled job; restart = data loss |
# MAGIC | Pandas-only execution | No parallelism, no predicate pushdown |
# MAGIC | In-memory SQLite for queries | Ephemeral, not queryable cross-session |
# MAGIC | Hardcoded `SKA_EMBEDDED_MAP` / `SKB_EMBEDDED_MAP` | Not version-controlled, can't differ across runs |
# MAGIC
# MAGIC ### Target Comparison: Spark SQL (JOIN + Column Expressions)
# MAGIC
# MAGIC | Current (Pandas) | Target (Spark SQL) |
# MAGIC |-----------------|--------------------|
# MAGIC | Build dict of key→row_index per source | `FULL OUTER JOIN` on canonical_key |
# MAGIC | Loop over all keys sequentially | Spark evaluates all keys in parallel |
# MAGIC | For each key, loop over fields | Column expressions evaluate all fields at once |
# MAGIC | `if val.upper() == "X": return "TRUE"` | `WHEN(col = 'X', 'TRUE')` in Silver view |
# MAGIC | Store results in Python list | Write directly to Delta |
# MAGIC | O(N × F) sequential | O(1) Spark plan, fully parallelized |

# COMMAND ----------

# DBTITLE 1,Backend-to-UI Interface Contract
# MAGIC %md
# MAGIC ## Backend-to-UI Interface Contract
# MAGIC
# MAGIC ### Core Principle
# MAGIC
# MAGIC The Databricks App **never executes comparison logic** in Phase 2. It:
# MAGIC 1. **Reads results** from Delta tables (Gold layer) via `databricks-sql-connector`
# MAGIC 2. **Triggers jobs** via Databricks Jobs API (for on-demand re-runs)
# MAGIC 3. **Updates resolution status** via SQL connector (user actions)
# MAGIC 4. **Queries historical data** for AI agent analysis
# MAGIC
# MAGIC ### App Service Layer (Phase 2)
# MAGIC
# MAGIC | Service | Purpose | Backend |
# MAGIC |---------|---------|----------|
# MAGIC | `ResultsService` | Read latest/historical reconciliation results | SQL Connector → Delta |
# MAGIC | `JobTriggerService` | Trigger ad-hoc runs, poll status | Databricks SDK → Jobs API |
# MAGIC | `ResolutionService` | User marks conflicts resolved/escalated | SQL Connector → Delta UPDATE |
# MAGIC | `LLMService` | AI agent (unchanged tools, new backend) | SQL Endpoint queries |
# MAGIC | `JiraService` | Create tickets (unchanged) | Jira REST API |
# MAGIC | `FileService` | **Legacy** ad-hoc upload only | Unchanged (pandas) |
# MAGIC
# MAGIC ### UI Mode Architecture
# MAGIC
# MAGIC ```
# MAGIC ┌─────────────────────────────────────────────────────────┐
# MAGIC │  DATABRICKS APP (Phase 2)                               │
# MAGIC │                                                         │
# MAGIC │  [📊 View Results]  [🔄 Trigger Run]  [📤 Ad-hoc*]     │
# MAGIC │                                                         │
# MAGIC │  Mode A: Results Viewer (DEFAULT)                       │
# MAGIC │  ├── Run selector (from reconciliation_runs)           │
# MAGIC │  ├── Paginated results grid                            │
# MAGIC │  ├── Field summary chart                              │
# MAGIC │  ├── Resolution actions → Delta UPDATE                 │
# MAGIC │  └── AI Agent → SQL endpoint                          │
# MAGIC │                                                         │
# MAGIC │  Mode B: Job Trigger                                    │
# MAGIC │  ├── Mode selector (SKA/SKB)                           │
# MAGIC │  ├── Trigger → Jobs API → poll status                  │
# MAGIC │  └── Auto-switch to Results on completion              │
# MAGIC │                                                         │
# MAGIC │  Mode C: Ad-hoc Upload (LEGACY, clearly labeled)        │
# MAGIC │  ├── Retained for testing/UAT/one-off comparisons      │
# MAGIC │  └── ⚠️ Results NOT persisted to Delta                  │
# MAGIC └─────────────────────────────────────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC ### Revised Roadmap (Adjusted for Existing Ingestion)
# MAGIC
# MAGIC | Week | Deliverable | Notes |
# MAGIC |------|-------------|-------|
# MAGIC | 1-2 | Delta schema + standardization notebooks | Convert existing Excel/JSON to Bronze/Silver Delta |
# MAGIC | 3-4 | Spark SQL comparison engine | FULL OUTER JOIN + CASE WHEN per field |
# MAGIC | 5 | Job orchestration | Multi-task: standardize → compare → notify |
# MAGIC | 6-7 | App Results Viewer + Job Trigger | `databricks-sql-connector` integration |
# MAGIC | 8 | Notifications + resolution tracking | Email/Teams + Delta updates |
# MAGIC
# MAGIC ### What Does NOT Need Building
# MAGIC
# MAGIC - ~~Ingestion from SharePoint~~ (already exists)
# MAGIC - ~~Ingestion from SAP~~ (already exists)
# MAGIC - ~~App UI redesign~~ (add tab, not rebuild)
# MAGIC - ~~Jira integration~~ (already works)
# MAGIC - ~~LLM agent tools~~ (same tools, different backend)

# COMMAND ----------


