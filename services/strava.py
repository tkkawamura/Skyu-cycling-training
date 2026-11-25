"""Write the self-analysis into today's Strava activity descriptions.

Setup (once): create an API app at https://www.strava.com/settings/api with
"Authorization Callback Domain" = the Streamlit app's domain, put
STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET into Streamlit secrets, open the app's
"Stravaと連携" link, approve, and paste the shown STRAVA_REFRESH_TOKEN into secrets.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"
SCOPE = "activity:read_all,activity:write"
MARKER = "【自己分析】"
TYPES = ("ride", "run")
TIMEOUT = 10


class StravaError(RuntimeError):
    pass


def authorize_url(client_id: str, redirect_uri: str) -> str:
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "force",
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        data={"client_id": client_id, "client_secret": client_secret, "code": code, "grant_type": "authorization_code"},
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaError(f"Strava連携に失敗しました（{response.status_code}）")
    return response.json()


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaError(f"Stravaのトークン更新に失敗しました（{response.status_code}）。連携をやり直してください")
    return response.json()["access_token"]


def merge_description(current: str | None, note: str) -> str:
    """Keep the existing description and replace only our own block at the end."""
    base = (current or "").split(MARKER)[0].rstrip()
    block = f"{MARKER}\n{note.strip()}"
    return f"{base}\n\n{block}" if base else block


def write_note(client_id: str, client_secret: str, refresh_token: str, note: str, day: date) -> list[str]:
    """Append the note to every ride/run of `day` (JST). Returns the activity names written."""
    token = access_token(client_id, client_secret, refresh_token)
    headers = {"Authorization": f"Bearer {token}"}
    tz = ZoneInfo("Asia/Tokyo")
    after = int(datetime.combine(day, time.min, tz).timestamp())
    before = int(datetime.combine(day, time.max, tz).timestamp())
    response = requests.get(
        f"{API}/athlete/activities",
        headers=headers,
        params={"after": after, "before": before, "per_page": 30},
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaError(f"Stravaのアクティビティ取得に失敗しました（{response.status_code}）")
    written = []
    for item in response.json():
        kind = str(item.get("sport_type") or item.get("type") or "").lower()
        if not any(word in kind for word in TYPES):
            continue
        detail = requests.get(f"{API}/activities/{item['id']}", headers=headers, timeout=TIMEOUT)
        if detail.status_code >= 400:
            raise StravaError(f"Stravaのアクティビティ取得に失敗しました（{detail.status_code}）")
        description = merge_description(detail.json().get("description"), note)
        update = requests.put(
            f"{API}/activities/{item['id']}", headers=headers, json={"description": description}, timeout=TIMEOUT
        )
        if update.status_code >= 400:
            raise StravaError(f"Stravaへの書き込みに失敗しました（{update.status_code}）")
        written.append(str(item.get("name") or item["id"]))
    return written
