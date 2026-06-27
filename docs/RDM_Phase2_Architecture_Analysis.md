# RDM Phase 2 — Architecture Analysis & Recommendation

## 1. Executive Summary

The Reference Data Reconciliation (RDM) application currently operates as an **interactive, session-based Flask app** that:
- Accepts manually uploaded Excel/CSV files from three sources (COA, FAQ/SAP, DataPool)
- Applies embedded field mappings (SKA/SKB) with fuzzy column resolution
- Runs pandas-based 3-way comparison with configurable transforms
- Stores results in an in-memory SQLite database for AI agent queries
- Provides LLM-powered analysis via Databricks Foundation Model API
- Supports Jira ticket creation for escalated conflicts

**Key Finding:** The existing comparison engine is well-architected for its current purpose (\~30K rows, 12-15 fields, 3 sources). The primary evolution path is to **decouple data ingestion from the interactive session** while preserving the UI and AI capabilities.

**Recommended Approach:** Option 2 — Move ingestion, standardization, and scheduled comparison into Databricks Jobs; the app becomes a results viewer + ad-hoc re-comparison tool.

---

### Critical Metrics from Current Codebase

| Component | Current State | Phase 2 Target |
|-----------|--------------|----------------|
| Data Input | Manual upload (Excel/CSV/XLSB) | Automated ingestion from SharePoint, SAP OData, DataPool |
| Comparison Engine | In-app pandas (in-memory SQLite) | Dual: scheduled Spark job + on-demand in-app |
| Result Storage | Session memory (lost on restart) | Delta tables with run_id, audit trail |
| Field Mappings | Hardcoded `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` | UC-managed config table |
| Transforms | Python functions in `diff_service.py` | Reusable module, version-controlled |
| AI Agent | Session-scoped SQLite queries | Delta table queries via Databricks SQL |
| Run History | None | Full lineage: input snapshot → transform → result |

## 2. Current-State Interpretation

### Architecture Diagram (Current)

```
┌─────────────────────────────────────────────────────────────────┐
│                    DATABRICKS APP (Flask)                         │
│                                                                   │
│  ┌──────────┐    ┌──────────────┐    ┌────────────────┐          │
│  │  Upload  │───▶│  FileService │───▶│   DiffService  │          │
│  │  (UI)    │    │  (parse)     │    │   (compare)    │          │
│  └──────────┘    └──────────────┘    └───────┬────────┘          │
│                                              │                    │
│                                              ▼                    │
│                                       ┌──────────┐               │
│                                       │  SQLite  │ (in-memory)   │
│                                       │ diff_results│              │
│                                       └─────┬────┘               │
│                                             │                     │
│                      ┌──────────────────────┼──────────────┐     │
│                      ▼                      ▼              ▼     │
│               ┌────────────┐        ┌────────────┐  ┌──────────┐│
│               │ LLM Agent  │        │   Export   │  │   Jira   ││
│               │ (tool-call)│        │  (xlsx/csv)│  │  Service ││
│               └────────────┘        └────────────┘  └──────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### Key Observations

1. **Stateless but session-bound**: All data lives in `_sessions` dict (Python process memory). App restart = data loss.
2. **Comparison logic is pure Python/pandas**: No Spark dependency. Works for current scale (\~50K rows max).
3. **Embedded mappings are sophisticated**: SKA (15 fields), SKB (12 fields) with predefined transforms, fuzzy column resolution, and custom user transforms.
4. **SQLite is the query layer**: The LLM agent uses SQLite for text-to-SQL. This is elegant but ephemeral.
5. **No run history or audit trail**: Each comparison is fire-and-forget.
6. **Volume path defined but unused for persistence**: `/Volumes/data_mesh_hub/rdm/uploads` exists in config but only used for temporary file storage.

### Source Data Characteristics (from embedded mappings)

| Source | Format | Key Fields | Peculiarities |
|--------|--------|------------|---------------|
| COA (SharePoint) | Excel (.xlsx/.xlsb) | Account Number (10-digit) | Yellow rows = excluded, strikethrough = deleted |
| FAQ (SAP) | Excel/CSV | G/L Account + Company Code | Values use descriptive names (need mapping to codes) |
| DataPool | Excel/CSV | gl_account / gl_account_number | Already uses snake_case canonical names |

## 3. Target-State Architecture

### Proposed Architecture (Phase 2)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DATABRICKS PLATFORM                               │
│                                                                           │
│  ┌─────────────────────────── INGESTION LAYER ──────────────────────┐    │
│  │                                                                    │    │
│  │  ┌──────────┐   ┌──────────┐   ┌──────────────┐                  │    │
│  │  │SharePoint│   │ SAP OData│   │  DataPool    │                  │    │
│  │  │  (Graph  │   │  (REST)  │   │  (ADLS/API)  │                  │    │
│  │  │   API)   │   │          │   │              │                  │    │
│  │  └─────┬────┘   └────┬─────┘   └──────┬───────┘                  │    │
│  │        │              │                │                           │    │
│  │        ▼              ▼                ▼                           │    │
│  │  ┌──────────────────────────────────────────────┐                 │    │
│  │  │         Landing / Raw (UC Volume)             │                 │    │
│  │  │   /Volumes/data_mesh_hub/rdm/raw/            │                 │    │
│  │  │     coa/2026-06-27/coa_master.xlsx           │                 │    │
│  │  │     sap/2026-06-27/faq_extract.json          │                 │    │
│  │  │     datapool/2026-06-27/gl_accounts.parquet  │                 │    │
│  │  └──────────────────────┬───────────────────────┘                 │    │
│  └─────────────────────────┼─────────────────────────────────────────┘    │
│                            │                                              │
│  ┌─────────────────────────┼───── PROCESSING LAYER ──────────────────┐   │
│  │                         ▼                                          │   │
│  │  ┌───────────────────────────────────────────────┐                 │   │
│  │  │          Bronze (Delta Tables)                 │                 │   │
│  │  │   data_mesh_hub.rdm.bronze_coa_master         │                 │   │
│  │  │   data_mesh_hub.rdm.bronze_sap_faq            │                 │   │
│  │  │   data_mesh_hub.rdm.bronze_datapool_gl        │                 │   │
│  │  └──────────────────────┬────────────────────────┘                 │   │
│  │                         │  Normalize / Standardize                 │   │
│  │                         ▼                                          │   │
│  │  ┌───────────────────────────────────────────────┐                 │   │
│  │  │          Silver (Delta Tables)                 │                 │   │
│  │  │   data_mesh_hub.rdm.silver_ska_coa            │                 │   │
│  │  │   data_mesh_hub.rdm.silver_ska_faq            │                 │   │
│  │  │   data_mesh_hub.rdm.silver_ska_datapool       │                 │   │
│  │  │   data_mesh_hub.rdm.silver_skb_coa            │                 │   │
│  │  │   data_mesh_hub.rdm.silver_skb_faq            │                 │   │
│  │  │   data_mesh_hub.rdm.silver_skb_datapool       │                 │   │
│  │  └──────────────────────┬────────────────────────┘                 │   │
│  │                         │  Compare (3-way diff)                    │   │
│  │                         ▼                                          │   │
│  │  ┌───────────────────────────────────────────────┐                 │   │
│  │  │          Gold / Results (Delta Tables)         │                 │   │
│  │  │   data_mesh_hub.rdm.reconciliation_runs       │  (metadata)     │   │
│  │  │   data_mesh_hub.rdm.reconciliation_results    │  (row-level)    │   │
│  │  │   data_mesh_hub.rdm.reconciliation_summary    │  (field stats)  │   │
│  │  │   data_mesh_hub.rdm.field_mappings            │  (config)       │   │
│  │  │   data_mesh_hub.rdm.transform_registry        │  (config)       │   │
│  │  └──────────────────────┬────────────────────────┘                 │   │
│  └─────────────────────────┼─────────────────────────────────────────┘   │
│                            │                                              │
│  ┌─────────────────────────┼───── PRESENTATION LAYER ────────────────┐   │
│  │                         ▼                                          │   │
│  │  ┌────────────────────────────────────────────────────────────┐    │   │
│  │  │              DATABRICKS APP (Flask)                          │    │   │
│  │  │                                                              │    │   │
│  │  │  Mode A: Results Viewer (reads Gold Delta tables)            │    │   │
│  │  │  Mode B: Ad-hoc Comparison (existing upload flow retained)   │    │   │
│  │  │  Mode C: AI Agent (queries Delta instead of SQLite)          │    │   │
│  │  │                                                              │    │   │
│  │  └────────────────────────────────────────────────────────────┘    │   │
│  │                                                                    │   │
│  │  ┌────────────────┐    ┌───────────────┐    ┌──────────────────┐  │   │
│  │  │ Notifications  │    │  Jira         │    │  Genie / AI      │  │   │
│  │  │ (email/Teams)  │    │  Integration  │    │  (SQL endpoint)  │  │   │
│  │  └────────────────┘    └───────────────┘    └──────────────────┘  │   │
│  └────────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────┘
```

### Unity Catalog Structure

```sql
-- New schema for RDM
CREATE SCHEMA IF NOT EXISTS data_mesh_hub.rdm;

-- Volume for raw file landing
CREATE VOLUME IF NOT EXISTS data_mesh_hub.rdm.raw;
CREATE VOLUME IF NOT EXISTS data_mesh_hub.rdm.uploads;  -- existing, for ad-hoc

-- Tables created by the processing pipeline (see Section 8)
```

## 4. File Format Recommendation

### Comparative Analysis

| Criterion | JSON | CSV/Excel | Parquet | **Delta Lake** |
|-----------|------|-----------|---------|----------------|
| Read Performance | ❌ Parse overhead | ❌ No predicate pushdown | ✅ Columnar, fast | ✅✅ Columnar + data skipping |
| Join Performance | ❌ Needs conversion | ❌ Needs conversion | ✅ Good | ✅✅ Z-ORDER, statistics |
| Schema Enforcement | ❌ None | ❌ None | ⚠️ At write time | ✅✅ Enforced + evolution |
| Schema Evolution | ✅ Flexible | ❌ Manual | ⚠️ Limited | ✅✅ ADD/RENAME/MERGE |
| Nested Structures | ✅ Native | ❌ Flat only | ✅ Supported | ✅ Supported |
| Auditability | ❌ No history | ❌ No history | ❌ No history | ✅✅ Time travel, CDF |
| Data Lineage | ❌ Manual | ❌ Manual | ❌ Manual | ✅✅ UC lineage |
| Incremental Processing | ❌ Full scan | ❌ Full scan | ⚠️ Partition-based | ✅✅ CDF, MERGE |
| Databricks SQL | ❌ Not queryable | ❌ Not queryable | ✅ Queryable | ✅✅ Full SQL + serverless |
| Databricks Apps | ❌ Needs SDK code | ❌ Needs SDK code | ⚠️ Via Spark only | ✅✅ Via SQL Connector |
| Long-term Maintenance | ❌ Brittle | ❌ Manual effort | ✅ Stable | ✅✅ VACUUM, OPTIMIZE |

### Recommendation

**All processing and comparison MUST use Delta Lake tables.** Raw formats are only acceptable in the landing zone.

| Layer | Format | Rationale |
|-------|--------|-----------|
| Landing/Raw | Original format (JSON, Excel, Parquet) | Preserve source fidelity, enable re-processing |
| Bronze | Delta | Schema enforcement, audit trail, time travel |
| Silver | Delta | Normalized schema, enables SQL access |
| Gold/Results | Delta | Query by Databricks SQL, Genie, Apps, notebooks |

**SAP JSON specifically:** Must be flattened to tabular Delta at Bronze. Nested structures are acceptable in raw landing only. The SKA/SKB fields are already defined as flat canonical fields — JSON nesting adds no value for comparison.

**DataPool files:** If already Parquet, the Bronze step is a simple `COPY INTO` or `read_files()` → Delta. Minimal transformation needed.

## 5. Required Changes to Existing Application

### Change Classification

#### Mandatory Changes (Required for Phase 2)

| # | Component | Current | Target | Effort |
|---|-----------|---------|--------|--------|
| 1 | **Data Access Layer** | Session `_sessions` dict | Read from Delta tables via `databricks-sql-connector` or Spark Connect | Medium |
| 2 | **Results persistence** | In-memory SQLite (lost on restart) | Write/read `reconciliation_results` Delta table | Medium |
| 3 | **Run management** | None | `reconciliation_runs` table with run_id, timestamp, status, source_versions | Low |
| 4 | **App startup mode** | Always starts empty | Load latest run results on startup ("Results Viewer" mode) | Low |
| 5 | **LLM Agent query backend** | SQLite `diff_results` | Databricks SQL endpoint (query Delta directly) | Medium |

#### Recommended Enhancements

| # | Component | Current | Target | Effort |
|---|-----------|---------|--------|--------|
| 6 | **Dual-mode operation** | Upload-only | Mode A: View scheduled results / Mode B: Ad-hoc upload comparison | Medium |
| 7 | **Field mappings externalized** | Hardcoded `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` | UC table `field_mappings` (version-controlled, editable) | Low |
| 8 | **Transform registry** | Python functions in `diff_service.py` | UC table `transform_registry` + Python module | Low |
| 9 | **Run comparison** | N/A | Compare current run vs previous run (trend detection) | Medium |
| 10 | **Notification trigger** | N/A | After scheduled comparison, notify on threshold breach | Low |

#### Optional Future Improvements

| # | Component | Description | Effort |
|---|-----------|-------------|--------|
| 11 | **Spark-based comparison** | Replace pandas with PySpark for datasets > 1M rows | High |
| 12 | **Streaming ingestion** | Auto-Loader for new files arriving in ADLS | Medium |
| 13 | **Multi-entity support** | Beyond SKA/SKB: cost centers, profit centers, etc. | Medium |
| 14 | **Approval workflow** | User marks conflict as "accepted" / "escalated" in app → status persisted | Low |
| 15 | **Data quality rules** | Great Expectations or DLT expectations on Bronze/Silver | Medium |

### What Remains Unchanged

- **Upload flow** (`file_service.py`): Retained as-is for ad-hoc mode
- **Comparison engine** (`diff_service.py`): Core logic stays; add Delta output option
- **UI** (`index.html`, JS, CSS): Minimal changes (add "View Results" tab)
- **Jira integration** (`jira_service.py`): Works as-is; add run_id to ticket metadata
- **LLM agent tools**: Same tool definitions; swap SQLite backend for SQL endpoint

## 6. Databricks Jobs / Workflow Design

### Recommended Orchestration: Multi-Task Job

```
┌─────────────────────────────────────────────────────────────────┐
│              JOB: rdm_reconciliation_pipeline                     │
│              Schedule: Daily 06:00 UTC (weekdays)                 │
│              Cluster: Serverless or Job Cluster (4-8 cores)       │
│                                                                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐             │
│  │ Task 1     │  │ Task 2     │  │ Task 3         │             │
│  │ Ingest COA │  │ Ingest SAP │  │ Ingest DataPool│             │
│  │ (SharePoint│  │ (OData API)│  │ (ADLS/API)     │             │
│  │  Graph API)│  │            │  │                │             │
│  └─────┬──────┘  └─────┬──────┘  └───────┬────────┘             │
│        │                │                 │                       │
│        └────────────────┼─────────────────┘                       │
│                         │  (all succeed)                          │
│                         ▼                                         │
│  ┌──────────────────────────────────────────────┐                │
│  │ Task 4: Validate Ingestion Completeness       │                │
│  │   - Row count thresholds                      │                │
│  │   - Schema drift detection                    │                │
│  │   - Freshness check                           │                │
│  └──────────────────────┬───────────────────────┘                │
│                         │                                         │
│                         ▼                                         │
│  ┌──────────────────────────────────────────────┐                │
│  │ Task 5: Normalize & Standardize (Bronze→Silver)│               │
│  │   - Apply column renames (field_mappings table)│                │
│  │   - Apply transforms (transform_registry)      │               │
│  │   - Produce silver_ska_*, silver_skb_* tables  │               │
│  └──────────────────────┬───────────────────────┘                │
│                         │                                         │
│                         ▼                                         │
│  ┌──────────────────────────────────────────────┐                │
│  │ Task 6: Execute 3-Way Comparison              │                │
│  │   - Read silver tables                        │                │
│  │   - Apply comparison logic (adapted from      │                │
│  │     diff_service.run_diff)                    │                │
│  │   - Persist to reconciliation_results         │                │
│  │   - Generate reconciliation_summary           │                │
│  └──────────────────────┬───────────────────────┘                │
│                         │                                         │
│                         ▼                                         │
│  ┌──────────────────────────────────────────────┐                │
│  │ Task 7: Post-Processing                       │                │
│  │   - Update reconciliation_runs status         │                │
│  │   - Classify discrepancies (severity)         │                │
│  │   - Compare with previous run (new conflicts) │                │
│  └──────────────────────┬───────────────────────┘                │
│                         │                                         │
│                         ▼                                         │
│  ┌──────────────────────────────────────────────┐                │
│  │ Task 8: Notify & Update                       │                │
│  │   - Send email/Teams if threshold breached    │                │
│  │   - Update app-visible status flag            │                │
│  │   - (Optional) Auto-create Jira for critical  │                │
│  └──────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
```

### Task Implementation Mapping

| Task | Implementation | Source |
|------|---------------|--------|
| 1-3 (Ingest) | Python notebooks using `requests` (Graph API, OData) | New notebooks |
| 4 (Validate) | Python notebook with assertions / DQ checks | New notebook |
| 5 (Normalize) | Python/SQL notebook applying `field_mappings` table | New notebook (reuses transform logic) |
| 6 (Compare) | Python notebook importing `diff_service` module | **Reuse existing module** |
| 7 (Post-process) | SQL/Python notebook | New notebook |
| 8 (Notify) | Python notebook (email via SMTP/Graph, Teams webhook) | New notebook |

### Cluster Recommendation

- **Tasks 1-4**: Serverless compute (lightweight HTTP calls + file parsing)
- **Tasks 5-7**: Job cluster with 4-8 workers (Spark for large datasets) OR Serverless SQL (if < 5M rows)
- **Task 8**: Serverless compute (API calls only)

### Scheduling

| Scenario | Schedule |
|----------|----------|
| Normal operations | Weekdays 06:00 UTC |
| Month-end close | Daily including weekends |
| Ad-hoc | Manual trigger from app UI or Databricks Jobs UI |

## 7. Comparison Logic Refactoring Approach

### Recommendation: Hybrid Model

The comparison logic should exist in **two modes**:

| Mode | Use Case | Engine | Data Source | Result Storage |
|------|----------|--------|-------------|----------------|
| **Scheduled** | Daily automated reconciliation | PySpark (Delta → Delta) | Silver Delta tables | Gold Delta tables |
| **Ad-hoc** | User uploads files, immediate comparison | Pandas (as today) | In-memory DataFrames | Delta table + session |

### Refactoring Strategy

**Step 1: Extract reusable module** (Week 1-2)

```
src/
├── rdm_app/              (Flask app — unchanged)
│   └── services/
│       └── diff_service.py  (imports from shared module)
└── rdm_core/             (NEW — shared comparison library)
    ├── __init__.py
    ├── mappings.py       (SKA_EMBEDDED_MAP, SKB_EMBEDDED_MAP)
    ├── transforms.py     (all transform functions)
    ├── comparator.py     (comparison logic — works with DataFrames OR Spark DFs)
    └── persistence.py    (Delta table read/write helpers)
```

**Step 2: Delta output adapter** (Week 2-3)

After comparison, persist results to Delta:

```python
# reconciliation_results table schema
schema = StructType([
    StructField("run_id", StringType()),        # UUID per execution
    StructField("run_timestamp", TimestampType()),
    StructField("mode", StringType()),           # SKA or SKB
    StructField("key_value", StringType()),      # composite key
    StructField("dtype", StringType()),          # same/conflict/only_COA/only_FAQ/only_DP
    StructField("field_canonical", StringType()),# which field
    StructField("coa_value", StringType()),
    StructField("faq_value", StringType()),
    StructField("datapool_value", StringType()),
    StructField("is_conflict", BooleanType()),
    StructField("severity", StringType()),       # critical/high/medium/low
    StructField("resolution_status", StringType()),  # open/accepted/escalated/resolved
    StructField("resolved_by", StringType()),
    StructField("jira_key", StringType()),
])
```

**Step 3: Spark implementation** (Week 3-4)

Port the pandas comparison to PySpark for scheduled runs:
- Joins replace key-map lookups (broadcast join for < 100K rows)
- Transforms apply via UDFs or `CASE WHEN` expressions
- Conflict detection via column-level comparison

### Key Decisions

| Decision | Recommendation | Rationale |
|----------|---------------|-----------|
| Keep pandas in app? | **Yes** | Fast for ad-hoc (< 60K rows), no Spark startup overhead |
| Move scheduled to Spark? | **Yes** | Enables > 1M rows, Delta native, job cluster |
| Store each run? | **Yes** | `run_id` partitioning enables trend analysis |
| Support incremental comparison? | **Phase 2.1** | First get full comparison working; then add CDF-based incremental |
| Manual rerun from UI? | **Yes** | Trigger Databricks Job via API from app button |

## 8. Storage and Table Design Recommendation

### Schema: `data_mesh_hub.rdm`

#### Configuration Tables

```sql
-- Field mappings (replaces hardcoded SKA_EMBEDDED_MAP / SKB_EMBEDDED_MAP)
CREATE TABLE data_mesh_hub.rdm.field_mappings (
  mapping_id STRING,
  version STRING,
  mode STRING,               -- 'SKA' or 'SKB'
  canonical_field STRING,
  label STRING,
  is_key BOOLEAN,
  source_name STRING,        -- 'COA', 'FAQ', 'DataPool'
  source_column STRING,      -- expected column name in source
  active BOOLEAN DEFAULT TRUE,
  updated_at TIMESTAMP,
  updated_by STRING
);

-- Transform registry (replaces hardcoded SKA_TRANSFORMS / SKB_TRANSFORMS)
CREATE TABLE data_mesh_hub.rdm.transform_registry (
  transform_id STRING,
  mode STRING,
  canonical_field STRING,
  source_name STRING,
  transform_type STRING,     -- 'predefined' or 'custom'
  function_name STRING,      -- e.g. '_tx_x_true_empty_false'
  instruction STRING,        -- human-readable description
  function_code STRING,      -- for custom: Python lambda/function code
  active BOOLEAN DEFAULT TRUE,
  updated_at TIMESTAMP
);
```

#### Bronze Tables (Source-Aligned)

```sql
-- Bronze: raw ingested data with minimal transformation
CREATE TABLE data_mesh_hub.rdm.bronze_coa_master (
  _ingestion_id STRING,
  _ingestion_timestamp TIMESTAMP,
  _source_file STRING,
  -- All source columns preserved as STRING (schema-on-read)
  ...dynamic columns...
) USING DELTA
PARTITIONED BY (_ingestion_id);

CREATE TABLE data_mesh_hub.rdm.bronze_sap_faq (...);
CREATE TABLE data_mesh_hub.rdm.bronze_datapool_gl (...);
```

#### Silver Tables (Standardized)

```sql
-- Silver: canonical schema, transforms applied
CREATE TABLE data_mesh_hub.rdm.silver_ska_coa (
  ingestion_id STRING,
  g_l_account STRING,        -- 10-digit, canonical
  account_group STRING,
  indicator_blocked_for_posting STRING,  -- already transformed (X→TRUE, empty→FALSE)
  chart_of_account STRING,
  functional_area_code STRING,
  gl_acct_long_text STRING,
  gl_account_subtype STRING,
  gl_account_type STRING,
  gl_account_external_id STRING,
  group_account_number STRING,
  indicator_mark_for_deletion STRING,
  pl_statement_account_type STRING,
  reconciliation_account_for_account_group STRING,
  short_text STRING,
  trading_partner_number STRING
) USING DELTA;

-- Similar for silver_ska_faq, silver_ska_datapool,
-- silver_skb_coa, silver_skb_faq, silver_skb_datapool
```

#### Gold / Results Tables

```sql
-- Run metadata
CREATE TABLE data_mesh_hub.rdm.reconciliation_runs (
  run_id STRING,
  run_timestamp TIMESTAMP,
  mode STRING,                 -- 'SKA' or 'SKB'
  trigger_type STRING,         -- 'scheduled' / 'manual' / 'adhoc_upload'
  triggered_by STRING,         -- user email or 'system'
  status STRING,               -- 'running' / 'completed' / 'failed'
  total_keys INT,
  matching_keys INT,
  conflict_keys INT,
  coa_only_keys INT,
  faq_only_keys INT,
  dp_only_keys INT,
  match_percentage DOUBLE,
  source_coa_version STRING,   -- ingestion_id or filename
  source_faq_version STRING,
  source_dp_version STRING,
  duration_seconds INT,
  error_message STRING,
  completed_at TIMESTAMP
) USING DELTA;

-- Row-level results (partitioned by run_id for efficient querying)
CREATE TABLE data_mesh_hub.rdm.reconciliation_results (
  run_id STRING,
  mode STRING,
  key_value STRING,
  dtype STRING,                -- 'same'/'conflict'/'only_COA'/'only_FAQ'/'only_DP'
  field_canonical STRING,
  field_label STRING,
  coa_value STRING,
  faq_value STRING,
  datapool_value STRING,
  is_conflict BOOLEAN,
  severity STRING,             -- derived from business rules
  resolution_status STRING DEFAULT 'open',
  resolved_by STRING,
  resolved_at TIMESTAMP,
  jira_key STRING
) USING DELTA
PARTITIONED BY (run_id);

-- Aggregated field-level summary per run
CREATE TABLE data_mesh_hub.rdm.reconciliation_summary (
  run_id STRING,
  mode STRING,
  field_canonical STRING,
  field_label STRING,
  conflict_count INT,
  total_records INT,
  conflict_percentage DOUBLE,
  severity_critical INT,
  severity_high INT,
  severity_medium INT,
  severity_low INT
) USING DELTA;
```

### Data Lifecycle

| Layer | Retention | VACUUM | Purpose |
|-------|-----------|--------|---------|
| Raw (Volume) | 90 days | N/A | Re-processing if needed |
| Bronze | 30 days history | 7 days | Audit, reprocessing |
| Silver | 30 days history | 7 days | Intermediate, debugging |
| Gold (Results) | Indefinite | 30 days | Business reporting, trend analysis |
| Runs | Indefinite | N/A | Full audit trail |

## 9. AI / Genie / Knowledge Base Enablement

### Immediate Opportunities (Phase 2.0)

| Capability | Data Asset | Implementation | Effort |
|-----------|-----------|----------------|--------|
| Natural language Q&A on results | `reconciliation_results` + `reconciliation_summary` | **Genie Space** on Delta tables | Low |
| "Which fields have most conflicts?" | `reconciliation_summary` | SQL query via Genie or in-app agent | Low |
| "Show me new conflicts since last run" | `reconciliation_results` (compare run_ids) | SQL window functions | Low |
| Explanation of WHY records mismatch | `reconciliation_results` + `transform_registry` | In-app LLM with context | Already exists |
| Summary generation for stakeholders | `reconciliation_runs` + `reconciliation_summary` | LLM summarization task in Job | Low |

### Near-Term Opportunities (Phase 2.1)

| Capability | Data Asset | Implementation | Effort |
|-----------|-----------|----------------|--------|
| Root cause analysis | Historical results (multi-run) | LLM + pattern detection on conflict trends | Medium |
| Suggested conflict resolution | `resolution_status` history | Few-shot learning from past resolutions | Medium |
| Pattern detection | Time-series of field conflict rates | Statistical anomaly detection + LLM narrative | Medium |
| Recommendations for next best action | All gold tables + Jira tickets | RAG over resolution history | Medium |

### Future Opportunities (Phase 3)

| Capability | Requirement | Implementation |
|-----------|-------------|----------------|
| Predictive conflict detection | 6+ months of run history | ML model on conflict patterns |
| Automated resolution proposals | User confirmation data on past resolutions | Fine-tuned model or prompt engineering |
| Cross-domain impact analysis | Multiple entity types (SKA, SKB, cost centers, etc.) | Knowledge graph on Delta |

### Genie Space Design

```
Genie Space: "RDM Reconciliation Results"
Tables:
  - data_mesh_hub.rdm.reconciliation_runs
  - data_mesh_hub.rdm.reconciliation_results
  - data_mesh_hub.rdm.reconciliation_summary
  - data_mesh_hub.rdm.field_mappings

Sample Questions:
  - "How many conflicts were found in the latest SKA run?"
  - "Which fields have the highest conflict rate this month?"
  - "Show me the trend of match percentage over the last 10 runs"
  - "List all open critical conflicts not yet assigned to Jira"
  - "Compare today's run with last week's run"
```

### In-App Agent Evolution

The current LLM agent queries SQLite. In Phase 2, swap the backend:

| Current | Phase 2 |
|---------|----------|
| `diff_service.execute_sql(sql)` on SQLite | `sql_connector.execute(sql)` on Databricks SQL endpoint |
| Tool: `query_diff_results` | Tool: `query_reconciliation_results` |
| Context: session-scoped data | Context: all historical runs |
| Scope: current comparison only | Scope: cross-run analysis, trends, history |

## 10. Notification and Jira Workflow

### Notification Design

#### When to Notify

| Trigger | Notification | Channel |
|---------|-------------|----------|
| Run completes with match% < threshold (e.g. 95%) | Alert: "Reconciliation below threshold" | Email + Teams |
| New critical conflicts detected (not in previous run) | Alert: "X new critical conflicts" | Email + Teams |
| Run fails | Error: "Reconciliation pipeline failed" | Email to pipeline owner |
| Run completes, all good | No notification | — (avoid noise) |
| First run of the week (Monday summary) | Digest: weekly trend | Email |

#### Notification Content

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ RDM Reconciliation Alert
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run:       2026-06-27 06:00 UTC (SKB mode)
Match:     92.3% (threshold: 95%)
Conflicts: 847 records across 12 fields
New since last run: +23 conflicts

Top conflict fields:
  • Field Status Group: 234 conflicts (27.6%)
  • Sort Key: 189 conflicts (22.3%)
  • Open Item Management: 156 conflicts (18.4%)

🔗 View in RDM App:
https://rdm-app-test.azuredatabricks.net/?run_id=abc123
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

#### Implementation

| Method | Pros | Cons | Recommendation |
|--------|------|------|----------------|
| Databricks SQL Alert | Native, low-code | Limited formatting | Good for simple thresholds |
| Python (SMTP) in Job task | Full control, rich HTML | Needs SMTP relay | ✅ **Recommended** |
| Microsoft Teams Webhook | Easy, visible | No email fallback | Use in addition to email |
| Power Automate | Enterprise-grade | Separate platform | Consider for Phase 3 |

### Jira Workflow

#### Current State (Already Working)

The existing `JiraService` creates stories with:
- Conflict field name and rate
- Sample conflict rows (table in Jira description)
- CSV attachment with all conflict rows
- App link for full investigation
- Labels: `rdm-conflict`, `data-quality`, `automated`

#### Phase 2 Enhancements

| Enhancement | Description | Trigger |
|-------------|-------------|----------|
| Auto-create for critical | If severity=critical AND conflict_rate > 30%, auto-create Jira | Job task (system-triggered) |
| User-triggered from app | User reviews conflict → clicks "Create Jira" (existing flow) | App UI (user-triggered) |
| Link to run_id | Jira ticket includes `run_id` for traceability | Both |
| Resolution tracking | When Jira status changes → update `resolution_status` in Delta | Jira webhook → Databricks API |
| De-duplication | Don't create duplicate tickets for same field+key if open ticket exists | Jira search before create |

#### Information in Jira Tickets

```
[Auto-created fields]
- Summary: [RDM] Data conflict: {field} — {count} of {total} records differ
- Priority: Critical/High/Medium/Low (based on conflict rate + business rules)
- Labels: rdm-conflict, data-quality, automated, {mode}, {run_date}
- Description: conflict report + sample data + app link
- Attachment: Full conflict CSV
- Custom fields (if configured):
  - RDM Run ID
  - Conflict Rate %
  - Sources Compared
  - First Detected Date
```

#### Avoid Notification Noise

1. **Threshold-based**: Only notify if match% drops below configurable threshold
2. **Delta-based**: Only notify on NEW conflicts (not previously seen)
3. **Cooldown**: Don't re-notify for same field within 24 hours
4. **Digest mode**: Option for daily digest instead of per-run alerts
5. **Severity filter**: Only notify for critical/high by default

## 11. Governance and Security Considerations

### UAT vs Production Requirements

| Control | UAT (Current) | Production (Target) |
|---------|--------------|---------------------|
| **Unity Catalog access** | User identity (PAT) | Service Principal with minimal grants |
| **App access control** | All workspace users | Group-based (`rdm-tool-users`) via app permissions |
| **Source data sensitivity** | Internal financial reference data | Same + audit logging |
| **Business Partner exclusion** | Not applicable (GL accounts only) | Monitor if scope expands |
| **Audit logging** | App server logs only | `reconciliation_runs` + UC audit logs |
| **Secrets management** | Databricks Secret Scope | Same (already using `github-secrets`) |
| **Service Principal** | `sp-data-comparer-deploy` for CI/CD | Add `sp-rdm-pipeline` for scheduled jobs |
| **Network isolation** | Standard workspace | Consider Private Endpoints for SAP/SharePoint |
| **Data encryption** | At-rest (ADLS default) | At-rest + in-transit (TLS) |

### Unity Catalog Permissions Design

```sql
-- Schema-level grants
GRANT USE SCHEMA ON data_mesh_hub.rdm TO `rdm-tool-users`;
GRANT SELECT ON SCHEMA data_mesh_hub.rdm TO `rdm-tool-users`;

-- Pipeline SP needs write access
GRANT USE SCHEMA ON data_mesh_hub.rdm TO `sp-rdm-pipeline`;
GRANT CREATE TABLE ON SCHEMA data_mesh_hub.rdm TO `sp-rdm-pipeline`;
GRANT MODIFY ON SCHEMA data_mesh_hub.rdm TO `sp-rdm-pipeline`;

-- Volume access
GRANT READ VOLUME ON VOLUME data_mesh_hub.rdm.raw TO `sp-rdm-pipeline`;
GRANT WRITE VOLUME ON VOLUME data_mesh_hub.rdm.raw TO `sp-rdm-pipeline`;
GRANT READ VOLUME ON VOLUME data_mesh_hub.rdm.uploads TO `rdm-tool-users`;
GRANT WRITE VOLUME ON VOLUME data_mesh_hub.rdm.uploads TO `rdm-tool-users`;
```

### App Security Model

| Layer | Mechanism | Notes |
|-------|-----------|-------|
| Authentication | Databricks SSO (via X-Forwarded-Email header) | Already implemented |
| Authorization | App permissions (CAN_USE grant to group) | Configured in `rdm_app.app.yml` |
| Data access from app | App runs as SP → UC permissions on SP | SP identity determines table access |
| User actions (Jira) | Logged with user email from proxy headers | Already implemented |
| Secret access | Databricks Secret Scope (`github-secrets`) | App SP needs scope access |

### Lineage and Audit

- **UC Lineage**: Automatic for Delta tables created by Spark jobs
- **Custom lineage**: `reconciliation_runs` tracks source_version → result mapping
- **User actions**: App logs all compare/export/jira actions with user identity
- **Time travel**: Delta 30-day history enables point-in-time investigation

## 12. Architecture Options Comparison

### Option 1: Minimal Change (File-Based Replacement)

**Description:** Replace manual uploads with pre-staged files in UC Volume. The app reads from Volume instead of user upload. Comparison still runs inside the app.

| Aspect | Assessment |
|--------|------------|
| **Changes required** | Ingestion notebooks deposit files to Volume; `FileService` reads from Volume path instead of upload | 
| **Benefits** | Minimal code changes; fast to implement; app logic unchanged |
| **Drawbacks** | No run history; results still ephemeral; no audit trail; app restart = data loss; can't support > 1 user comparing simultaneously; no notifications |
| **Complexity** | Low |
| **Maintainability** | Poor — still session-bound, no persistence, no scheduled execution |
| **Recommended for** | Quick PoC only; NOT suitable for production reconciliation |

### Option 2: Full Separation (Jobs + App as Viewer) ✅ RECOMMENDED

**Description:** Ingestion, standardization, and scheduled comparison run as Databricks Jobs. Results persist in Delta tables. The app reads results from Delta and retains ad-hoc upload capability.

| Aspect | Assessment |
|--------|------------|
| **Changes required** | New rdm schema + tables; ingestion notebooks; comparison job (reuses diff logic); app adds Delta read mode |
| **Benefits** | Full audit trail; run history; trend analysis; Genie-ready; supports notifications; survives app restart; enables multiple users; production-grade |
| **Drawbacks** | More development effort; requires schema design; job monitoring |
| **Complexity** | Medium |
| **Maintainability** | Excellent — clear separation of concerns, UC-governed, version-controlled |
| **Recommended for** | ✅ Production reconciliation, enterprise UAT, team collaboration |

### Option 3: Full Platform (SDP / Streaming)

**Description:** Use Lakeflow Spark Declarative Pipelines (SDP) with streaming tables, materialized views, and expectations for the entire flow.

| Aspect | Assessment |
|--------|------------|
| **Changes required** | Rewrite all logic as SDP pipeline; streaming tables for Bronze; MVs for Silver/Gold |
| **Benefits** | Built-in DQ expectations; automatic retry; lineage; optimized incremental processing |
| **Drawbacks** | Over-engineered for batch reference data (changes weekly/monthly); steep learning curve; harder to debug comparison logic; less flexible for ad-hoc |
| **Complexity** | High |
| **Maintainability** | Good for streaming workloads; overkill for weekly reference data reconciliation |
| **Recommended for** | Only if data changes frequently (hourly/real-time) — NOT the case here |

### Final Comparison Matrix

| Criterion | Option 1 | **Option 2** ✅ | Option 3 |
|-----------|----------|------------|----------|
| Implementation effort | 1 week | 4-6 weeks | 8-10 weeks |
| Run history | ❌ | ✅ | ✅ |
| Audit trail | ❌ | ✅ | ✅ |
| Notification support | ❌ | ✅ | ✅ |
| Genie/AI enablement | ❌ | ✅ | ✅ |
| Ad-hoc comparison | ✅ | ✅ | ⚠️ Limited |
| Multi-user support | ❌ | ✅ | ✅ |
| Production readiness | ❌ | ✅ | ✅ |
| Data freshness | File-based | Scheduled (daily) | Near real-time |
| Suits reference data workload | ⚠️ | ✅ | ❌ Over-engineered |

## 13. Recommended Implementation Roadmap

### Phase 2.0 — Foundation (Weeks 1-4)

| Week | Deliverable | Tasks |
|------|-------------|-------|
| **1** | UC Schema + Tables | Create `data_mesh_hub.rdm` schema; create all Gold tables (runs, results, summary); create config tables (field_mappings, transform_registry) |
| **1** | Seed config tables | Migrate `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP` → `field_mappings`; migrate transforms → `transform_registry` |
| **2** | Extract `rdm_core` module | Refactor `diff_service.py` into importable module; create Delta persistence adapter |
| **2** | Comparison Job (notebook) | Create notebook that reads Silver tables → runs comparison → writes to Gold |
| **3** | Ingestion notebooks (COA) | SharePoint Graph API → Raw Volume → Bronze Delta |
| **3** | Ingestion notebooks (SAP) | SAP OData → Raw Volume (JSON) → Bronze Delta (flattened) |
| **4** | Normalization notebook | Bronze → Silver (apply field_mappings + transform_registry) |
| **4** | Job orchestration | Create multi-task job with dependency chain |

### Phase 2.1 — App Integration (Weeks 5-6)

| Week | Deliverable | Tasks |
|------|-------------|-------|
| **5** | App reads Delta results | Add "Results Viewer" mode; app queries `reconciliation_results` via SQL connector |
| **5** | Run selector UI | Dropdown to select run_id; show run metadata (match%, timestamp) |
| **6** | LLM agent on Delta | Swap SQLite backend for Databricks SQL endpoint queries |
| **6** | Manual rerun trigger | Button in app → triggers Databricks Job via API |

### Phase 2.2 — Notifications & Actions (Weeks 7-8)

| Week | Deliverable | Tasks |
|------|-------------|-------|
| **7** | Email notifications | Task 8 in job: send email on threshold breach |
| **7** | Teams webhook | Post conflict summary to Teams channel |
| **8** | Jira enhancements | Auto-create for critical; de-duplication; run_id in tickets |
| **8** | Resolution tracking | User marks conflicts in app → updates Delta table |

### Phase 2.3 — AI & Analytics (Weeks 9-10)

| Week | Deliverable | Tasks |
|------|-------------|-------|
| **9** | Genie Space | Create Genie space over Gold tables |
| **9** | Trend dashboard | AI/BI dashboard showing match% trend, top fields, run history |
| **10** | Cross-run analysis | Agent tool: compare two runs, highlight new/resolved conflicts |
| **10** | Weekly digest | Automated summary email with trend + recommendations |

### Immediate Next Steps (This Sprint)

1. ✅ **Complete current deployment** (push wheels, validate CI/CD on test workspace)
2. Create `data_mesh_hub.rdm` schema and volume in test workspace
3. Create Gold tables DDL (can run from UC_Bootstrap notebook)
4. Seed `field_mappings` table from existing `SKA_EMBEDDED_MAP`/`SKB_EMBEDDED_MAP`
5. Create first ingestion notebook (start with DataPool — simplest source)

## 14. Open Questions / Decisions Needed

| # | Question | Impact | Default Recommendation |
|---|----------|--------|------------------------|
| 1 | **SharePoint access method**: Graph API vs direct ADLS mount vs Power Automate export? | Ingestion architecture | Graph API (direct, no intermediary) |
| 2 | **SAP extraction**: OData API vs RFC/BAPI vs scheduled SAP extract to ADLS? | Ingestion complexity | OData API if available; else scheduled extract |
| 3 | **DataPool format**: Is it already available as Parquet in ADLS, or needs extraction? | Bronze complexity | Confirm with DataPool team |
| 4 | **Comparison frequency**: Daily? Weekly? On-demand only? | Job scheduling | Daily for production; weekly sufficient for UAT |
| 5 | **Match threshold for alerts**: What % triggers notification? | Notification design | 95% (configurable) |
| 6 | **Severity classification rules**: How to categorize critical vs high vs medium? | Result enrichment | Critical: key fields (account group, type); High: posting-relevant; Medium: descriptive |
| 7 | **UC catalog ownership**: Can `sp-rdm-pipeline` get CREATE TABLE on `data_mesh_hub`? | Permissions | Needs catalog owner (`data-mesh-cicd` SP) to grant |
| 8 | **Multi-entity scope**: Phase 2 includes cost centers, profit centers beyond SKA/SKB? | Schema design | Start with SKA/SKB only; design for extensibility |
| 9 | **Jira project**: Dedicated RDM project or shared data quality project? | Jira configuration | Dedicated: cleaner queries, better reporting |
| 10 | **User resolution workflow**: Accept/Escalate/Suppress — what statuses are needed? | Result table schema | open → acknowledged → escalated → resolved → suppressed |
| 11 | **Historical retention**: How long to keep reconciliation results? | Storage cost | Indefinite for Gold; 90 days for Bronze/Silver |
| 12 | **Test workspace readiness**: Is `data_mesh_hub` catalog available in `dbx-dps-raise-dev`? | Deployment | Run UC_Bootstrap notebook in test workspace first |

---

## Summary

**Go with Option 2**: Jobs handle ingestion + scheduled comparison; App becomes a dual-mode viewer (scheduled results + ad-hoc uploads). Delta Lake throughout. Genie space for self-service analytics. Notifications on threshold breach only.

The existing `diff_service.py` comparison logic is solid — extract it as a shared module, don't rewrite it. The app UI needs minimal changes (add a results viewer tab). The biggest new work is ingestion notebooks and the job orchestration.

**Total estimated effort**: 8-10 weeks for a single developer, with Phase 2.0 (foundation) deliverable in 4 weeks.
