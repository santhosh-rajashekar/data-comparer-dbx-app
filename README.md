# RDM 3-Way Diff — Databricks App

Reference & Master Data Quality Assurance tool that performs 3-way reconciliation across COA (Chart of Accounts), FAQ (SAP), and DataPool sources.

## Architecture

- **Frontend**: Flask + custom HTML/JS (preserved from original HTML app)
- **Backend**: Python services (file parsing, diff engine, LLM agent)
- **AI**: Databricks Foundation Model API with tool-calling agent
- **Deployment**: Databricks Apps via Declarative Automation Bundles (DABs)

## Project Structure

```
├── databricks.yml              # Bundle config (targets: dev, test, prp, prd)
├── resources/
│   └── rdm_app.app.yml         # App resource definition
├── src/rdm_app/
│   ├── app.py                  # Flask entry point
│   ├── app.yaml                # Databricks App runtime config
│   ├── requirements.txt        # Python dependencies
│   ├── services/
│   │   ├── llm_service.py      # Tool-calling LLM agent
│   │   ├── diff_service.py     # 3-way comparison engine
│   │   └── file_service.py     # Excel/CSV parsing
│   ├── templates/index.html    # Main UI template
│   └── static/
│       ├── css/rdm.css         # Stylesheet
│       └── js/
│           ├── rdm-ui.js       # UI logic
│           └── rdm-chat.js     # Chat panel
└── .github/workflows/
    ├── deploy.yml              # CI/CD: validate → dev → test → prp → prd
    └── pr-validate.yml         # PR checks: validate all targets
```

## Deployment

### Manual (CLI)
```bash
databricks bundle deploy --target dev
databricks bundle run rdm_3way_diff --target dev
```

### CI/CD (GitHub Actions)
Merge to `main` triggers: validate → dev → test → prp → prd (each with environment approval gates).

## GitHub Environment Setup

Create these environments in GitHub repo settings with required secrets:

| Environment | Secrets Required | Approval |
|-------------|-----------------|----------|
| `dev` | `DATABRICKS_HOST`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET` | None (auto) |
| `test` | `DATABRICKS_HOST`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET` | Optional |
| `prp` | `DATABRICKS_HOST`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET` | Required |
| `prd` | `DATABRICKS_HOST`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET` | Required (2 reviewers) |

### Service Principal Permissions
The SP needs:
- `CAN_MANAGE` on the Databricks App
- Access to the Foundation Model API serving endpoint
- Workspace-level permissions to deploy bundles

## Features

- **3-Way Reconciliation**: Compare data across COA, FAQ (SAP), and DataPool
- **AI Agent**: Tool-calling agent with 6 tools (field stats, SQL queries, sample conflicts)
- **Data Transforms**: AI-generated normalization rules to reduce false conflicts
- **Export**: Excel (with conflict highlighting) and CSV
- **Session Cache**: Browser IndexedDB caching to skip re-upload during development
