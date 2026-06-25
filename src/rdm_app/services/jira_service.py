"""JIRA Integration Service for RDM 3-Way Diff."""
import os, io, csv, json, logging
from typing import Optional
from datetime import datetime
import requests

logger = logging.getLogger(__name__)

class JiraService:
    def __init__(self):
        self.base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        self.project_key = os.environ.get("JIRA_PROJECT_KEY", "")
        self.user_email = os.environ.get("JIRA_USER_EMAIL", "")
        self.api_token = os.environ.get("JIRA_API_TOKEN", "")
        self.app_url = os.environ.get("APP_BASE_URL", "")

    @property
    def configured(self):
        return all([self.base_url, self.project_key, self.user_email, self.api_token])

    def _auth(self):
        return (self.user_email, self.api_token)

    def _api_url(self, path):
        return f"{self.base_url}/rest/api/3/{path.lstrip('/')}"

    def create_conflict_story(self, field, conflict_count, total_records, sample_rows,
                              all_conflict_rows, sources, priority="Medium",
                              summary_override="", additional_context="", labels=None):
        if not self.configured:
            return {"success": False, "error": "JIRA not configured. Set JIRA_BASE_URL, JIRA_PROJECT_KEY, JIRA_USER_EMAIL, JIRA_API_TOKEN."}

        summary = summary_override or f"[RDM] Data conflict: {field} \u2014 {conflict_count} of {total_records} records differ across {'/'.join(sources)}"
        description = self._build_description(field, conflict_count, total_records, sample_rows, sources, additional_context)
        priority_map = {"Critical": "Highest", "High": "High", "Medium": "Medium", "Low": "Low"}

        payload = {"fields": {
            "project": {"key": self.project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": "Story"},
            "priority": {"name": priority_map.get(priority, "Medium")},
            "labels": labels or ["rdm-conflict", "data-quality", "automated"],
        }}

        try:
            resp = requests.post(self._api_url("issue"),
                                 headers={"Accept": "application/json", "Content-Type": "application/json"},
                                 auth=self._auth(), json=payload, timeout=30)
            if resp.status_code not in (200, 201):
                return {"success": False, "error": f"JIRA API error {resp.status_code}: {resp.text[:200]}"}
            issue = resp.json()
            issue_key = issue["key"]
            issue_url = f"{self.base_url}/browse/{issue_key}"
            if all_conflict_rows:
                self._attach_csv(issue_key, field, all_conflict_rows)
            return {"success": True, "key": issue_key, "url": issue_url, "summary": summary}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"Connection error: {str(e)}"}

    def _attach_csv(self, issue_key, field, rows):
        try:
            buffer = io.StringIO()
            if rows:
                writer = csv.DictWriter(buffer, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            csv_bytes = buffer.getvalue().encode("utf-8")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"rdm_conflicts_{field.replace(' ', '_')}_{ts}.csv"
            resp = requests.post(self._api_url(f"issue/{issue_key}/attachments"),
                                 headers={"Accept": "application/json", "X-Atlassian-Token": "no-check"},
                                 auth=self._auth(), files={"file": (filename, csv_bytes, "text/csv")}, timeout=60)
            return {"success": resp.status_code in (200, 201), "filename": filename}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _build_description(self, field, conflict_count, total_records, sample_rows, sources, additional_context):
        pct = round(conflict_count / max(total_records, 1) * 100, 1)
        content = [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Data Conflict Report"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Field: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": field}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Conflict Rate: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": f"{conflict_count:,} of {total_records:,} records ({pct}%)"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Sources: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": ", ".join(sources)}]},
        ]
        if sample_rows:
            headers = list(sample_rows[0].keys())
            header_row = {"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": h}]}]} for h in headers]}
            body_rows = [{"type": "tableRow", "content": [
                {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": str(row.get(h, ""))}]}]} for h in headers]}
                for row in sample_rows[:10]]
            content.append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Sample Conflicts"}]})
            content.append({"type": "table", "content": [header_row] + body_rows})
        if additional_context:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": additional_context}]})
        if self.app_url:
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": "View in RDM App: "},
                {"type": "text", "text": self.app_url, "marks": [{"type": "link", "attrs": {"href": self.app_url}}]}]})
        content.append({"type": "paragraph", "content": [
            {"type": "text", "text": f"\u2014 Auto-generated by RDM Agent on {datetime.now().strftime('%Y-%m-%d %H:%M')}", "marks": [{"type": "em"}]}]})
        return {"type": "doc", "version": 1, "content": content}

    def get_status(self):
        return {"configured": self.configured, "base_url": self.base_url or "(not set)",
                "project_key": self.project_key or "(not set)",
                "user_email": (self.user_email[:3] + "***") if self.user_email else "(not set)"}

    def test_connection(self):
        if not self.configured:
            return {"success": False, "error": "JIRA not configured"}
        try:
            resp = requests.get(self._api_url("myself"),
                                headers={"Accept": "application/json"}, auth=self._auth(), timeout=10)
            if resp.status_code == 200:
                return {"success": True, "user": resp.json().get("displayName", "unknown")}
            return {"success": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
