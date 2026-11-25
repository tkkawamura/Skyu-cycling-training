"""Post the self-analysis to a Discord thread through a channel webhook."""
from __future__ import annotations

import requests

LIMIT = 2000


class DiscordError(RuntimeError):
    pass


def post_note(webhook_url: str, thread_id: str | None, text: str) -> None:
    params = {"wait": "true"}
    if thread_id:
        params["thread_id"] = thread_id
    response = requests.post(
        webhook_url,
        params=params,
        json={"content": text.strip()[:LIMIT], "username": "S級コーチ", "allowed_mentions": {"parse": []}},
        timeout=10,
    )
    if response.status_code >= 400:
        raise DiscordError(f"Discordへの投稿に失敗しました（{response.status_code}）")
