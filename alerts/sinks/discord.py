"""Discord sink: posts events as embeds to one or more webhooks, usually one
embed per message but bundling several into one message when the pacing
budget can't give everything its own slot (see send() below).

Webhook URLs are SECRETS (env vars DISCORD_WEBHOOK_URL, DISCORD_WEBHOOK_URL_2,
DISCORD_WEBHOOK_URL_3, ...) -- never log them, never put them in an exception
message that might reach logs/CI output. An event only counts as delivered
once every configured webhook has received it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV_VAR = "DISCORD_WEBHOOK_URL"
PACE_BUDGET_MINUTES_ENV_VAR = "DISCORD_PACE_WINDOW_MINUTES"
STATE_DIR_ENV_VAR = "ALERTS_STATE_DIR"
DEFAULT_STATE_DIR = Path(__file__).parent.parent / "state"
LAST_POST_STATE_FILENAME = "discord_last_post.json"
REQUEST_TIMEOUT_SECONDS = 15

MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 350
MAX_PLACE_CHARS = 100

# Discord hard limits are 10 embeds and 6000 combined characters per
# message. Kept comfortably under both so the truncation math above doesn't
# need to be exact.
MAX_EMBEDS_PER_MESSAGE = 8
MAX_TOTAL_CHARS_PER_MESSAGE = 5500

# New events post at randomized moments across roughly PACE_BUDGET_MINUTES
# instead of firing within seconds of each other, never closer together
# than PACE_MIN_INTERVAL_SECONDS. Unlike an earlier version of this, nothing
# is ever left for the next check: there's only one check per hour now (see
# poll.yml), so deferring would mean a real story sitting unposted for up
# to another hour. Instead, every event this run collects gets posted THIS
# run -- if there isn't enough of the budget left to give each event its
# own message at the minimum gap, extra events are bundled into the same
# message for some of the slots (see _assign_slots/_build_message_batches)
# rather than pushed to later.
#
# PACE_MIN_INTERVAL_SECONDS is also the *cross-run* minimum gap: a single
# new event found on its own would otherwise post immediately with no
# pacing at all, so two separate runs landing close together (e.g. a manual
# run overlapping the hourly one) could both fire right away. The last
# successful post time is persisted (see LAST_POST_STATE_FILENAME) so even
# a lone event waits out the rest of this gap since the previous run's last
# post, not just gaps *within* one run's batch.
DEFAULT_PACE_BUDGET_MINUTES = 52
PACE_MIN_INTERVAL_SECONDS = 240
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
    """Post every event to every configured webhook this run -- nothing is
    ever deferred to the next check. Returns the subset actually delivered
    (to all webhooks); callers must only mark returned events as "seen" so
    a failed post stays eligible for retry instead of silently vanishing."""
    if not events:
        return []

    webhook_urls = _webhook_urls()
    if not webhook_urls:
        logger.error("discord: %s is not set, skipping %d event(s)", WEBHOOK_URL_ENV_VAR, len(events))
        return []

    slots = _assign_slots(events)
    logger.info("discord: posting %d event(s) across %d message(s) this run", len(events), len(slots))

    budget_seconds = _pace_budget_seconds()
    start = time.time()
    sent: list[dict] = []
    for i, slot_events in enumerate(slots):
        if i == 0:
            _wait_for_cross_run_gap()
        for batch in _build_message_batches(slot_events):
            # A plain list comprehension, not a short-circuiting all(...) --
            # every configured webhook must actually be attempted, even if
            # an earlier one failed, so one broken channel can't silently
            # starve the others of a message they'd otherwise have gotten.
            results = [_post_batch(url, batch) for url in webhook_urls]
            if all(results):
                sent.extend(batch)
                _save_last_post_time(time.time())
        if i + 1 < len(slots):
            remaining_gaps = len(slots) - 1 - i
            remaining_budget = max(0.0, budget_seconds - (time.time() - start))
            gap = _random_gap(remaining_gaps, remaining_budget)
            logger.info("discord: waiting %.0fs before the next post", gap)
            time.sleep(gap)
    return sent


def _wait_for_cross_run_gap() -> None:
    """Even a lone new event must not post within PACE_MIN_INTERVAL_SECONDS
    of the last successfully delivered post, no matter which run (or which
    trigger -- normal schedule vs. a backup timer) sent that earlier post.
    Without this, a single-event run always posted with zero delay, which
    is how two separate runs a minute apart could both fire immediately and
    end up visually merged in Discord."""
    last_post_epoch = _load_last_post_time()
    if last_post_epoch is None:
        return
    remaining = PACE_MIN_INTERVAL_SECONDS - (time.time() - last_post_epoch)
    if remaining > 0:
        logger.info(
            "discord: waiting %.0fs since the previous check's last post to keep messages spaced out",
            remaining,
        )
        time.sleep(remaining)


def _last_post_state_path() -> Path:
    state_dir = Path(os.environ.get(STATE_DIR_ENV_VAR, DEFAULT_STATE_DIR))
    return state_dir / LAST_POST_STATE_FILENAME


def _load_last_post_time() -> float | None:
    path = _last_post_state_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("last_post_epoch")
    except Exception:
        logger.exception("discord: failed to read last-post timestamp, treating as unknown")
        return None


def _save_last_post_time(epoch: float) -> None:
    path = _last_post_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_post_epoch": epoch}, f)


def _webhook_urls() -> list[str]:
    """DISCORD_WEBHOOK_URL, plus DISCORD_WEBHOOK_URL_2, _3, ... for any
    additional channels/servers, each mirroring the exact same feed."""
    urls = []
    primary = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if primary:
        urls.append(primary)
    i = 2
    while True:
        extra = os.environ.get(f"{WEBHOOK_URL_ENV_VAR}_{i}")
        if not extra:
            break
        urls.append(extra)
        i += 1
    return urls


def _pace_budget_seconds() -> float:
    return float(os.environ.get(PACE_BUDGET_MINUTES_ENV_VAR, DEFAULT_PACE_BUDGET_MINUTES)) * 60


def _assign_slots(events: list[dict]) -> list[list[dict]]:
    """Split events (already in chronological order) into as many posting
    slots as fit in the pacing budget at the minimum gap. If there are more
    events than that, extra events are bundled onto slots (multiple events
    -> one Discord message) instead of leaving anything for next check."""
    if not events:
        return []
    if len(events) <= 1:
        return [events]

    budget_seconds = _pace_budget_seconds()
    slot_count = max(1, min(len(events), int(budget_seconds // PACE_MIN_INTERVAL_SECONDS) + 1))
    base, extra = divmod(len(events), slot_count)

    slots: list[list[dict]] = []
    idx = 0
    for i in range(slot_count):
        size = base + (1 if i < extra else 0)
        if size == 0:
            continue
        slots.append(events[idx : idx + size])
        idx += size
    return slots


def _random_gap(remaining_gaps: int, remaining_budget_seconds: float) -> float:
    """A randomized gap in [PACE_MIN_INTERVAL_SECONDS, PACE_MAX_INTERVAL_SECONDS].
    Re-planned at every step from how much budget and how many gaps are
    left, so the run stays inside its pacing budget even if earlier gaps
    happened to land on the high side -- never a perfectly even metronome,
    never below the minimum.

    The range is built so its *average* never exceeds the recomputed
    target (high = 2*target_avg - low keeps the uniform distribution's mean
    exactly at target_avg): when there's real slack (few events relative to
    the budget), that still allows plenty of randomness up to the max gap;
    when every slot is already needed just to fit within budget (target_avg
    at the floor), it falls back to the floor with no jitter, since any
    randomness there could only push the run over its budget, never under."""
    if remaining_gaps <= 0:
        return PACE_MIN_INTERVAL_SECONDS
    target_avg = remaining_budget_seconds / remaining_gaps
    low = PACE_MIN_INTERVAL_SECONDS
    if target_avg <= low:
        return low
    high = min(PACE_MAX_INTERVAL_SECONDS, 2 * target_avg - low)
    if high <= low:
        return low
    return random.uniform(low, high)


def _build_message_batches(events: list[dict]) -> list[list[dict]]:
    """Pack one slot's events into as few Discord messages as possible
    while staying under the embeds-per-message and combined-character
    limits -- almost always just one message; only a very large slot (e.g.
    an unusually big backlog bundled together) spills into a second."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for event in events:
        chars = _embed_char_count(event)
        if current and (
            len(current) >= MAX_EMBEDS_PER_MESSAGE or current_chars + chars > MAX_TOTAL_CHARS_PER_MESSAGE
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(event)
        current_chars += chars
    if current:
        batches.append(current)
    return batches


def _embed_char_count(event: dict) -> int:
    embed = _to_embed(event)
    total = len(embed.get("title", "")) + len(embed.get("description", "") or "")
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    return total


def _post_batch(webhook_url: str, events: list[dict], retries_left: int = 3) -> bool:
    payload = {"embeds": [_to_embed(event) for event in events]}
    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("discord: request failed for event(s) %s", [e["id"] for e in events])
        return False

    if response.status_code == 429:
        if retries_left <= 0:
            logger.error("discord: rate-limited repeatedly, dropping %d event(s)", len(events))
            return False
        retry_after = _extract_retry_after(response)
        logger.warning("discord: rate-limited, waiting %.1fs", retry_after)
        time.sleep(retry_after)
        return _post_batch(webhook_url, events, retries_left=retries_left - 1)

    if not response.ok:
        logger.error(
            "discord: webhook post failed with status %d for %d event(s): %s",
            response.status_code,
            len(events),
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
