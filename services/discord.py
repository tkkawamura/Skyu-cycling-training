"""Post the self-analysis (and optional photos) to a Discord thread through a channel webhook."""
from __future__ import annotations

import io
import json

import requests

LIMIT = 2000
MAX_FILES = 10
MAX_BYTES = 8 * 1024 * 1024  # stay under Discord's 10MB per-file limit


class DiscordError(RuntimeError):
    pass


def shrink(name: str, data: bytes) -> tuple[str, bytes]:
    """Re-encode big images as JPEG so they fit Discord's upload limit."""
    if len(data) <= MAX_BYTES:
        return name, data
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert("RGB")
        image.thumbnail((2400, 2400))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        stem = name.rsplit(".", 1)[0]
        return f"{stem}.jpg", out.getvalue()
    except Exception:
        return name, data


def post_note(
    webhook_url: str,
    thread_id: str | None,
    text: str | None,
    photos: list[tuple[str, bytes]] | None = None,
) -> None:
    params = {"wait": "true"}
    if thread_id:
        params["thread_id"] = thread_id
    payload = {"content": (text or "").strip()[:LIMIT], "username": "S級コーチ", "allowed_mentions": {"parse": []}}
    files = {}
    for index, (name, data) in enumerate((photos or [])[:MAX_FILES]):
        name, data = shrink(name, data)
        files[f"files[{index}]"] = (name, data)
    if files:
        response = requests.post(
            webhook_url, params=params, data={"payload_json": json.dumps(payload)}, files=files, timeout=30
        )
    else:
        response = requests.post(webhook_url, params=params, json=payload, timeout=10)
    if response.status_code >= 400:
        raise DiscordError(f"Discordへの投稿に失敗しました（{response.status_code}）")
