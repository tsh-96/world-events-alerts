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
PACE_WINDOW_MINUTES_ENV_VAR = "DISCORD_PACE_WINDOW_MINUTES"
REQUEST_TIMEOUT_SECONDS = 15

# One embed per Discord message -- with the per-field caps below, a single
# embed is always far under Discord's combined-message character limit, so
# there is no batching/char-budget bookkeeping to get wrong.
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 350
MAX_PLACE_CHARS = 100

# When there's more than one new event, spread the posts evenly across this
# many minutes (leaving a safety buffer before the *next* scheduled check)
# instead of firing them all within seconds -- avoids a wall of messages
# landing at once after a quiet stretch. Gaps are capped so a small handful
# of events don't wait needlessly long. Tune this to roughly match how often
# the workflow actually runs (see .github/workflows/poll.yml).
DEFAULT_PACE_WINDOW_MINUTES = 45
PACE_MAX_INTERVAL_SECONDS = 600

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


def send(events: list[dict]) -> list[dict]:
    """Post events to Discord, paced out over time, and return the subset
    actually delivered. Callers must only mark returned events as "seen" --
    an event that fails to post has to stay eligible for retry on the next
    poll, not silently vanish into the dedupe store."""
    if not events:
        return []

    webhook_url = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if not webhook_url:
        logger.error("discord: %s is not set, skipping %d event(s)", WEBHOOK_URL_ENV_VAR, len(events))
        return []

    interval_seconds = _pacing_interval(len(events))
    if interval_seconds:
        logger.info(
            "discord: pacing %d event(s) ~%.0fs apart", len(events), interval_seconds
        )

    sent: list[dict] = []
    for i, event in enumerate(events):
        if _post_one(webhook_url, event):
            sent.append(event)
        if interval_seconds and i + 1 < len(events):
            time.sleep(interval_seconds)
    return sent


def _pacing_interval(event_count: int) -> float:
    """Seconds to wait between posts so `event_count` posts spread evenly
    across the configured pacing window, capped so a small handful of
    events never wait needlessly long."""
    if event_count <= 1:
        return 0.0
    window_minutes = float(
        os.environ.get(PACE_WINDOW_MINUTES_ENV_VAR, DEFAULT_PACE_WINDOW_MINUTES)
    )
    window_seconds = window_minutes * 60
    return min(PACE_MAX_INTERVAL_SECONDS, window_seconds / (event_count - 1))


def _post_one(webhook_url: str, event: dict, retries_left: int = 3) -> bool:
    payload = {"embeds": [_to_embed(event)]}
    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("discord: request failed for event %s", event["id"])
        return False

    if response.status_code == 429:
        if retries_left <= 0:
            logger.error("discord: rate-limited repeatedly, dropping event %s", event["id"])
            return False
        retry_after = _extract_retry_after(response)
        logger.warning("discord: rate-limited, waiting %.1fs", retry_after)
        time.sleep(retry_after)
        return _post_one(webhook_url, event, retries_left=retries_left - 1)

    if not response.ok:
        logger.error(
            "discord: webhook post failed with status %d for event %s: %s",
            response.status_code,
            event["id"],
            response.text[:500],
        )
        return False

    return True


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
        fields.append(
            {"name": "Place", "value": _truncate(str(event["place"]), MAX_PLACE_CHARS), "inline": True}
        )
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
