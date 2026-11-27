"""Save each day's report data to the repository's `logs` branch.

The `logs` branch keeps the raw daily reports (the data part of the Claude paste
text) so the weekly review task can read them. It is a separate branch so the
Streamlit app (deployed from `main`) is not redeployed on every save.
"""
from __future__ import annotations

import base64
from datetime import date
from urllib.parse import quote

import requests

API = "https://api.github.com"
BRANCH = "logs"
TIMEOUT = 10


class GitHubLogError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _ensure_branch(token: str, repo: str) -> None:
    headers = _headers(token)
    ref = requests.get(f"{API}/repos/{repo}/git/ref/heads/{BRANCH}", headers=headers, timeout=TIMEOUT)
    if ref.status_code == 200:
        return
    main = requests.get(f"{API}/repos/{repo}/git/ref/heads/main", headers=headers, timeout=TIMEOUT)
    if main.status_code >= 400:
        raise GitHubLogError(f"GitHubに接続できませんでした（{main.status_code}）")
    created = requests.post(
        f"{API}/repos/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{BRANCH}", "sha": main.json()["object"]["sha"]},
        timeout=TIMEOUT,
    )
    if created.status_code >= 400:
        raise GitHubLogError(f"GitHubのlogsブランチを作れませんでした（{created.status_code}）")


def save_report(token: str, repo: str, day: date, text: str) -> str:
    """Create or overwrite logs/日報/YYYY-MM-DD.md on the logs branch. Returns the path."""
    _ensure_branch(token, repo)
    headers = _headers(token)
    path = f"logs/日報/{day.isoformat()}.md"
    url = f"{API}/repos/{repo}/contents/{quote(path)}"
    existing = requests.get(url, headers=headers, params={"ref": BRANCH}, timeout=TIMEOUT)
    body = {
        "message": f"Daily report {day.isoformat()}",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]
    saved = requests.put(url, headers=headers, json=body, timeout=TIMEOUT)
    if saved.status_code >= 400:
        raise GitHubLogError(f"GitHubへの日報の保存に失敗しました（{saved.status_code}）")
    return path
