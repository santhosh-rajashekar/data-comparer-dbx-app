# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Setup: SP, Permissions and GitHub Secrets
# MAGIC %md
# MAGIC # Setup: Service Principal, Permissions & GitHub Secrets
# MAGIC
# MAGIC Run this notebook **once** from the Databricks workspace to configure all CI/CD prerequisites for the `data-comparer-dbx-app` GitHub Actions workflow.
# MAGIC
# MAGIC | Step | What it does |
# MAGIC |------|--------------|
# MAGIC | 1 | Check identity + install pynacl |
# MAGIC | 2 | Find or create SP `sp-data-comparer-deploy` |
# MAGIC | 3 | Generate M2M OAuth secret OR PAT fallback |
# MAGIC | 4 | Grant SP workspace, UC, and app permissions |
# MAGIC | 5 | Create GitHub environments (dev / test / prp / prd / dev-raise) |
# MAGIC | 6 | Create GitHub repo secrets |
# MAGIC | 7 | Trigger deploy workflow for `dev` and monitor |
# MAGIC
# MAGIC > **Prerequisites**: The secret scope `github-secrets` must contain key `github-pat`  
# MAGIC > with a PAT that has `repo` + `admin:secrets` scopes on `santhosh-rajashekar/data-comparer-dbx-app`.

# COMMAND ----------

# DBTITLE 1,Step 1 — Check identity + install pynacl
import sys, subprocess, json, base64, time, requests
from databricks.sdk import WorkspaceClient, AccountClient

w  = WorkspaceClient()
me = w.current_user.me()
print(f"Workspace : {w.config.host}")
is_admin = any(getattr(g, 'display', '') == 'admins' for g in (me.groups or []))
print(f"User      : {me.user_name}  (admin={is_admin})")

# Install pynacl (needed for GitHub secret encryption)
try:
    from nacl import encoding, public as nacl_public
    print("pynacl    : already available ✅")
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pynacl", "-q"])
    from nacl import encoding, public as nacl_public
    print("pynacl    : installed ✅")

# Read GitHub PAT from secret scope
gh_token = dbutils.secrets.get("github-secrets", "github-pat")
print(f"GitHub PAT: {'*'*8}{gh_token[-4:]} ✅")

# Verify PAT has access to the repo
r = requests.get(
    "https://api.github.com/repos/santhosh-rajashekar/data-comparer-dbx-app",
    headers={"Authorization": f"token {gh_token}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"})
r.raise_for_status()
repo = r.json()
print(f"GitHub repo: {repo['full_name']} (default branch: {repo['default_branch']}) ✅")
print(f"PAT scopes : {r.headers.get('X-OAuth-Scopes', 'n/a')}")

# Store for later cells
REPO   = "santhosh-rajashekar/data-comparer-dbx-app"
GH_HDR = {"Authorization": f"token {gh_token}",
           "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}

# COMMAND ----------

# DBTITLE 0,Step 2 — Find or create SP sp-data-comparer-deploy
SP_NAME = "sp-data-comparer-deploy"

existing = list(w.service_principals.list(filter=f"displayName eq '{SP_NAME}'"))
if existing:
    sp = existing[0]
    print(f"✅ SP already exists: {sp.display_name}")
else:
    sp = w.service_principals.create(display_name=SP_NAME, active=True)
    print(f"✅ SP created: {sp.display_name}")

print(f"   id             : {sp.id}")
print(f"   application_id : {sp.application_id}")

# COMMAND ----------

# DBTITLE 1,Step 3 — Get auth credentials (M2M OAuth or PAT fallback)
# Try account-level M2M OAuth first (requires account admin).
# If unavailable, generate a workspace PAT as fallback and update
# the workflow to use DATABRICKS_TOKEN instead of CLIENT_ID/SECRET.

client_id     = None
client_secret = None
pat_token     = None
use_oauth     = False

try:
    a = AccountClient()
    cred = a.service_principal_secrets.create(service_principal_id=sp.id)
    client_id     = str(sp.application_id)
    client_secret = cred.secret
    use_oauth     = True
    print("✅ M2M OAuth credentials created")
    print(f"   DATABRICKS_CLIENT_ID     : {client_id}")
    print(f"   DATABRICKS_CLIENT_SECRET : {'*'*24} (captured)")
except Exception as e:
    print(f"⚠️  M2M not available ({type(e).__name__}) — using PAT fallback")
    t = w.tokens.create(
        comment="GitHub Actions deploy — data-comparer-dbx-app",
        lifetime_seconds=365 * 24 * 3600)
    pat_token = t.token_value
    print(f"✅ PAT generated (id: {t.token_info.token_id}, lifetime: 1 year)")
    print("   NOTE: workflow will use DATABRICKS_TOKEN instead of CLIENT_ID/SECRET")

print(f"\nAuth mode: {'OAuth M2M' if use_oauth else 'PAT token'}")

# COMMAND ----------

# DBTITLE 1,Step 4 — Grant SP workspace, UC and app permissions
from databricks.sdk.service.iam import PermissionLevel

sp_name = f"servicePrincipal/{sp.application_id}"

# ── 4a. Add SP as workspace admin (needed to deploy apps/jobs) ──
try:
    w.groups.get  # probe
    admins = next((g for g in w.groups.list(filter="displayName eq 'admins'") if g.display_name == "admins"), None)
    if admins:
        from databricks.sdk.service.iam import ComplexValue
        w.groups.patch(
            id=admins.id,
            operations=[{"op": "add", "path": "members",
                         "value": [{"value": str(sp.id)}]}])
        print(f"✅ SP added to workspace admins group")
except Exception as e:
    print(f"⚠️  Admin group update: {e}")

# ── 4b. UC permissions ─────────────────────────────────────────
# UC references SPs by their application_id UUID, not display name
sp_app_id = str(sp.application_id)

for sql in [
    f"GRANT USE CATALOG ON CATALOG data_mesh_hub TO `{sp_app_id}`",
    f"GRANT USE SCHEMA  ON SCHEMA  data_mesh_hub.rdm TO `{sp_app_id}`",
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME data_mesh_hub.rdm.uploads TO `{sp_app_id}`",
    f"GRANT QUERY ON SERVING ENDPOINT `databricks-claude-sonnet-4-5` TO `{sp_app_id}`",
]:
    try:
        spark.sql(sql)
        print(f"✅ {sql[:70]}...")
    except Exception as e:
        print(f"⚠️  {sql[:60]}: {e}")

print("\nPermissions applied.")

# COMMAND ----------

# DBTITLE 1,Step 5 — Create GitHub environments
# Creates environments with optional protection rules.
# dev/test/prp are unprotected (auto-deploy).
# prd requires a manual reviewer before deploy runs.
# dev-raise is manual-dispatch only (no auto-deploy on push).

# dev  = existing workspace (current)
# test = dbx-dps-raise-dev (target)
ENVS = [
    {"name": "dev"},
    {"name": "test"},
]

for env in ENVS:
    r = requests.put(
        f"https://api.github.com/repos/{REPO}/environments/{env['name']}",
        headers=GH_HDR,
        json={"deployment_branch_policy": None})
    status = "✅" if r.status_code in (200, 201) else f"❌ {r.status_code}"
    print(f"{status}  environment: {env['name']}")

# COMMAND ----------

# DBTITLE 1,Step 6 — Create GitHub repo secrets
from nacl import encoding, public as nacl_public

# ── Fill in workspace credentials below ──────────────────────────────
# dev  = workspace you deploy FROM / develop on
# test = target workspace (dbx-dps-raise-dev)
#
# Get PAT: workspace UI → User Settings → Developer → Access Tokens

DEV_HOST  = "https://adb-7405616457611961.1.azuredatabricks.net/"   # <── new dev workspace URL  e.g. https://adb-xxxx.x.azuredatabricks.net
DEV_TOKEN = ""   # <── PAT from dev workspace

TEST_HOST  = "https://adb-7405614118453546.6.azuredatabricks.net"  # dbx-dps-raise-dev
TEST_TOKEN = ""

# ── Non-secret config (GitHub Variables — visible, not encrypted) ───
# These drive BUNDLE_VAR_* in the workflow. Override per environment as needed.
DEV_VARS  = {"HUB_CATALOG": "data_mesh_hub",
             "SERVING_ENDPOINT": "databricks-claude-sonnet-4-5"}
TEST_VARS = {"HUB_CATALOG": "data_mesh_hub",
             "SERVING_ENDPOINT": "databricks-claude-sonnet-4-5"}

# ── Sensitive config (GitHub Secrets) ─────────────────────────────
DEV_FLASK_SECRET  = "<REDACTED_SET_VIA_SECRETS_SCOPE>"
TEST_FLASK_SECRET = "<REDACTED_SET_VIA_SECRETS_SCOPE>"

# ─────────────────────────────────────────────────────────────────────

if not DEV_HOST or not DEV_TOKEN:
    raise ValueError("Fill in DEV_HOST and DEV_TOKEN above before running.")

def _encrypt(pub_key_b64: str, secret: str) -> str:
    pub = nacl_public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
    return base64.b64encode(nacl_public.SealedBox(pub).encrypt(secret.encode())).decode()

def put_env_secret(env_name: str, name: str, value: str):
    """Create/update an encrypted secret in a GitHub environment."""
    pk = requests.get(
        f"https://api.github.com/repos/{REPO}/environments/{env_name}/secrets/public-key",
        headers=GH_HDR).json()
    r  = requests.put(
        f"https://api.github.com/repos/{REPO}/environments/{env_name}/secrets/{name}",
        headers=GH_HDR,
        json={"encrypted_value": _encrypt(pk["key"], value), "key_id": pk["key_id"]})
    ok = r.status_code in (201, 204)
    print(f"  {'✅' if ok else '❌'} env[{env_name:6}] secret  {name:30} (HTTP {r.status_code})")

def put_env_var(env_name: str, name: str, value: str):
    """Create/update a plain (non-secret) variable in a GitHub environment."""
    base_url = f"https://api.github.com/repos/{REPO}/environments/{env_name}/variables"
    # Try PATCH (update) first; fall back to POST (create)
    r = requests.patch(f"{base_url}/{name}", headers=GH_HDR, json={"name": name, "value": value})
    if r.status_code == 404:
        r = requests.post(base_url, headers=GH_HDR, json={"name": name, "value": value})
    ok = r.status_code in (200, 201, 204)
    print(f"  {'✅' if ok else '❌'} env[{env_name:6}] var     {name:30} = {value}  (HTTP {r.status_code})")

# ── dev environment ───────────────────────────────────────────────
print("\n── dev environment ──")
put_env_secret("dev", "DATABRICKS_HOST",  DEV_HOST)
put_env_secret("dev", "DATABRICKS_TOKEN", DEV_TOKEN)
put_env_secret("dev", "FLASK_SECRET",     DEV_FLASK_SECRET)
for k, v in DEV_VARS.items():
    put_env_var("dev", k, v)

# ── test environment ───────────────────────────────────────────────
print("\n── test environment ──")
put_env_secret("test", "DATABRICKS_HOST",  TEST_HOST)
put_env_secret("test", "DATABRICKS_TOKEN", TEST_TOKEN)
put_env_secret("test", "FLASK_SECRET",     TEST_FLASK_SECRET)
for k, v in TEST_VARS.items():
    put_env_var("test", k, v)

print("\n✅ All secrets and variables set. Run Step 8 to push databricks.yml + deploy.yml.")

# COMMAND ----------

# DBTITLE 1,Step 7 — Trigger deploy workflow for dev + monitor
import time

# Trigger workflow_dispatch for 'test' target (dbx-dps-raise-dev)
trigger = requests.post(
    f"https://api.github.com/repos/{REPO}/actions/workflows/deploy.yml/dispatches",
    headers=GH_HDR,
    json={"ref": "main", "inputs": {"target": "test"}})

if trigger.status_code == 204:
    print("✅ Workflow triggered (target=test). Waiting for run to appear...")
else:
    print(f"❌ Trigger failed: {trigger.status_code} {trigger.text}")
    raise SystemExit

time.sleep(5)  # let GitHub register the new run

# Poll latest run for deploy.yml
for attempt in range(18):   # up to 3 minutes
    runs_r = requests.get(
        f"https://api.github.com/repos/{REPO}/actions/workflows/deploy.yml/runs",
        headers=GH_HDR, params={"per_page": 1, "event": "workflow_dispatch"})
    runs = runs_r.json().get("workflow_runs", [])
    if not runs:
        print("  Waiting for run...")
        time.sleep(10)
        continue

    run   = runs[0]
    run_id= run["id"]
    status= run["status"]
    concl = run["conclusion"]
    url   = run["html_url"]

    print(f"  Run #{run_id} | status={status} | conclusion={concl or 'pending'}")

    if status == "completed":
        icon = "✅" if concl == "success" else "❌"
        print(f"\n{icon} Workflow finished: {concl.upper()}")
        print(f"   View run: {url}")
        break
    time.sleep(10)
else:
    print(f"\n⏱ Still running after 3 min. Check: {url}")

# COMMAND ----------

# DBTITLE 1,Step 8 — Push changed files to GitHub via Contents API
# ── Push ALL files in a SINGLE commit via GitHub Git Data API ────────────────
# Uses the low-level tree/commit API so all file changes land in one commit,
# which triggers exactly one workflow run (not one per file).
# ─────────────────────────────────────────────────────────────────────────────
import base64, requests

gh_token = dbutils.secrets.get("github-secrets", "github-pat")
REPO     = "santhosh-rajashekar/data-comparer-dbx-app"
BRANCH   = "main"
GH_HDR   = {"Authorization": f"token {gh_token}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
BASE     = "/Workspace/Users/skarotirajashekar@godevsuite060.onmicrosoft.com/data-comparer-dbx-app"

FILES = [
    "databricks.yml",
    "resources/rdm_app.app.yml",
    ".github/workflows/deploy.yml",
    ".github/workflows/pr-validate.yml",
    "src/rdm_app/requirements.txt",
    "src/rdm_app/pyxlsb-1.0.10-py2.py3-none-any.whl",
    "src/rdm_app/rapidfuzz-3.13.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    "src/rdm_app/xlsxwriter-3.2.9-py3-none-any.whl",
]
COMMIT_MSG = "chore: update bundle config, app env injection, force-lock deploy"
API = f"https://api.github.com/repos/{REPO}"

# ── Step 1: get current HEAD commit + tree ───────────────────────────────────
ref_r   = requests.get(f"{API}/git/refs/heads/{BRANCH}", headers=GH_HDR)
ref_r.raise_for_status()
head_sha    = ref_r.json()["object"]["sha"]
commit_r    = requests.get(f"{API}/git/commits/{head_sha}", headers=GH_HDR)
base_tree   = commit_r.json()["tree"]["sha"]
print(f"HEAD: {head_sha[:8]}  base tree: {base_tree[:8]}")

# ── Step 2: create a blob for each file ──────────────────────────────────────
tree_items = []
for rel_path in FILES:
    with open(f"{BASE}/{rel_path}", "rb") as f:
        content = f.read()
    blob_r = requests.post(f"{API}/git/blobs", headers=GH_HDR,
                           json={"content": base64.b64encode(content).decode(),
                                 "encoding": "base64"})
    blob_r.raise_for_status()
    tree_items.append({"path": rel_path, "mode": "100644",
                       "type": "blob", "sha": blob_r.json()["sha"]})
    print(f"  blob: {rel_path}")

# ── Step 3: create new tree ───────────────────────────────────────────────────
tree_r = requests.post(f"{API}/git/trees", headers=GH_HDR,
                       json={"base_tree": base_tree, "tree": tree_items})
tree_r.raise_for_status()
new_tree = tree_r.json()["sha"]
print(f"\nnew tree: {new_tree[:8]}")

# ── Step 4: create commit ─────────────────────────────────────────────────────
commit_r = requests.post(f"{API}/git/commits", headers=GH_HDR,
                         json={"message": COMMIT_MSG,
                               "tree": new_tree,
                               "parents": [head_sha]})
commit_r.raise_for_status()
new_sha = commit_r.json()["sha"]
print(f"commit:   {new_sha[:8]}")

# ── Step 5: advance branch ref ───────────────────────────────────────────────
ref_upd = requests.patch(f"{API}/git/refs/heads/{BRANCH}", headers=GH_HDR,
                         json={"sha": new_sha})
ref_upd.raise_for_status()
print(f"\n✅ Pushed 1 commit ({new_sha[:8]}) with {len(FILES)} file(s)")
print(f"   deploy-test will trigger once.")
print(f"   https://github.com/{REPO}/actions")

# COMMAND ----------

# DBTITLE 1,Utility — Delete all workflow runs except one
import requests, time

gh_token = dbutils.secrets.get("github-secrets", "github-pat")
REPO     = "santhosh-rajashekar/data-comparer-dbx-app"
GH_HDR   = {"Authorization": f"token {gh_token}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}

KEEP_RUN_ID = 28266264744  # the successful run to preserve

# Fetch all runs across all pages
all_runs, page = [], 1
while True:
    r = requests.get(f"https://api.github.com/repos/{REPO}/actions/runs",
                     headers=GH_HDR, params={"per_page": 100, "page": page})
    r.raise_for_status()
    batch = r.json().get("workflow_runs", [])
    if not batch:
        break
    all_runs.extend(batch)
    page += 1

to_delete = [r for r in all_runs if r["id"] != KEEP_RUN_ID]
print(f"Total runs : {len(all_runs)}")
print(f"Keeping    : #{KEEP_RUN_ID}")
print(f"Deleting   : {len(to_delete)}\n")

deleted, failed = 0, 0
for run in to_delete:
    d = requests.delete(
        f"https://api.github.com/repos/{REPO}/actions/runs/{run['id']}",
        headers=GH_HDR)
    if d.status_code == 204:
        deleted += 1
    else:
        print(f"  \u274c #{run['id']}  HTTP {d.status_code}")
        failed += 1
    time.sleep(0.1)  # stay under rate limit

print(f"\n\u2705 Deleted {deleted} run(s)" + (f"  \u274c {failed} failed" if failed else ""))