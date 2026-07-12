"""Discord sink: posts one embed per event to a webhook.

The webhook URL is a SECRET (env var DISCORD_WEBHOOK_URL) -- never log it,
never put it in an exception message that might reach logs/CI output.
"""

from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV_VAR = "DISCORD_WEBHOOK_URL"
REQUEST_TIMEOUT_SECONDS = 15
MAX_EMBEDS_PER_MESSAGE = 10
SLEEP_BETWEEN_REQUESTS_SECONDS = 2.0

# Discord embed field limits (title/description get truncated to these).
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096

COLOR_BY_KIND = {
    "earthquake": 0xE74C3C,  # red
    "flood": 0x3498DB,  # blue
    "cyclone": 0xE67E22,  # orange
    "volcano": 0x8B0000,  # dark red
    "wildfire": 0xD35400,  # burnt orange
    "drought": 0xB8860B,  # dark goldenrod
    "tsunami": 0x1ABC9C,  # teal
    "news": 0x95A5A6,  # grey
}
DEFAULT_COLOR = 0x95A5A6  # grey


def send(events: list[dict]) -> None:
    if not events:
        return

    webhook_url = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if not webhook_url:
        logger.error("discord: %s is not set, skipping %d event(s)", WEBHOOK_URL_ENV_VAR, len(events))
        return

    for i in range(0, len(events), MAX_EMBEDS_PER_MESSAGE):
        batch = events[i : i + MAX_EMBEDS_PER_MESSAGE]
        _post_batch(webhook_url, batch)
        if i + MAX_EMBEDS_PER_MESSAGE < len(events):
            time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)


def _post_batch(webhook_url: str, batch: list[dict], retries_left: int = 3) -> None:
    payload = {"embeds": [_to_embed(event) for event in batch]}
    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("discord: request failed for a batch of %d event(s)", len(batch))
        return

    if response.status_code == 429:
        if retries_left <= 0:
            logger.error("discord: rate-limited repeatedly, dropping batch of %d event(s)", len(batch))
            return
        retry_after = _extract_retry_after(response)
        logger.warning("discord: rate-limited, waiting %.1fs", retry_after)
        time.sleep(retry_after)
        _post_batch(webhook_url, batch, retries_left=retries_left - 1)
        return

    if not response.ok:
        logger.error(
            "discord: webhook post failed with status %d for a batch of %d event(s)",
            response.status_code,
            len(batch),
        )


def _extract_retry_after(response: requests.Response) -> float:
    try:
        return float(response.json().get("retry_after", 1.0))
    except Exception:
        return 1.0


def _to_embed(event: dict) -> dict:
    title = _truncate(event["title"], MAX_TITLE_CHARS)
    description = _truncate(event["summary"], MAX_DESCRIPTION_CHARS) if event["summary"] else None

    fields = []
    if event["place"]:
        fields.append({"name": "Place", "value": str(event["place"]), "inline": True})
    if event["severity"] is not None:
        fields.append({"name": "Severity", "value": str(event["severity"]), "inline": True})
    fields.append({"name": "Time (UTC)", "value": event["time_utc"], "inline": True})

    embed = {
        "title": title,
        "url": event["url"],
        "color": COLOR_BY_KIND.get(event["kind"], DEFAULT_COLOR),
        "fields": fields,
        "footer": {"text": event["source"]},
    }
    if description:
        embed["description"] = description
    return embed


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
