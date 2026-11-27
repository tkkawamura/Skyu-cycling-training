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
MARKER = "\u2063"  # invisible separator marking where our note starts
OLD_MARKER = "【自己分析】"
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
    """Keep the existing description and replace only our own block at the end.

    Our block starts with an invisible separator (U+2063) so no visible header is needed.
    """
    base = (current or "").split(MARKER)[0].split(OLD_MARKER)[0].rstrip()
    block = f"{MARKER}{note.strip()}"
    return f"{base}\n\n{block}" if base else block


def write_note(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    note: str,
    day: date,
    skip_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Append the note once to every activity of `day` (JST).

    Activities in `skip_ids` (already written earlier today) or whose description
    already contains the note are left alone. Returns (written names, written ids).
    """
    token = access_token(client_id, client_secret, refresh_token)
    headers = {"Authorization": f"Bearer {token}"}
    tz = ZoneInfo("Asia/Tokyo")
    after = int(datetime.combine(day, time.min, tz).timestamp())
    before = int(datetime.combine(day, time.max, tz).timestamp())
    response = requests.get(
        f"{API}/athlete/activities",
        headers=headers,
        params={"after": after, "before": before, "per_page": 50},
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaError(f"Stravaのアクティビティ取得に失敗しました（{response.status_code}）")
    names, ids = [], []
    for item in response.json():
        activity_id = str(item["id"])
        if activity_id in (skip_ids or set()):
            continue
        detail = requests.get(f"{API}/activities/{activity_id}", headers=headers, timeout=TIMEOUT)
        if detail.status_code >= 400:
            raise StravaError(f"Stravaのアクティビティ取得に失敗しました（{detail.status_code}）")
        current = detail.json().get("description") or ""
        if note.strip() in current:
            ids.append(activity_id)
            continue
        description = merge_description(current, note)
        update = requests.put(
            f"{API}/activities/{activity_id}", headers=headers, json={"description": description}, timeout=TIMEOUT
        )
        if update.status_code >= 400:
            raise StravaError(f"Stravaへの書き込みに失敗しました（{update.status_code}）")
        names.append(str(item.get("name") or activity_id))
        ids.append(activity_id)
    return names, ids
