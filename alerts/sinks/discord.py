"""Discord sink: posts events as embeds to a dev channel and a prod channel,
usually one embed per message but bundling several into one message when
the pacing budget can't give everything its own slot (see send() below).

Two channels, not mirrors of each other:
  DISCORD_WEBHOOK_URL    -- "dev": gets every notable event. Used for
                            trying out new/unproven sources before they're
                            trusted.
  DISCORD_WEBHOOK_URL_2  -- "prod": gets only notable events also flagged
                            `prod_ready` (see alerts/normalize.py). Promote
                            a source from dev-only to prod by flipping its
                            `prod: true` in alerts/config/feeds.yaml once
                            you're happy with what it's been posting to dev.
  DISCORD_WEBHOOK_URL_3, _4, ... -- additional prod mirrors, if ever
                            needed; everything past the first webhook is
                            "prod tier".
  DISCORD_PROD_ENABLED   -- master switch for the whole prod tier. Unless
                            this is exactly "true", NOTHING posts to prod
                            no matter what any individual source's `prod`
                            flag says -- a single place to guarantee prod
                            stays silent while reviewing new sources in
                            dev, instead of relying on every source's flag
                            being set correctly.

Webhook URLs are SECRETS -- never log them, never put them in an exception
message that might reach logs/CI output. Dev and prod are paced and
delivery-confirmed independently (see _deliver_to_channel): an event only
counts as fully "seen" once dev has it, and once prod has it too if it was
prod-eligible in the first place.
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
PROD_ENABLED_ENV_VAR = "DISCORD_PROD_ENABLED"
STATE_DIR_ENV_VAR = "ALERTS_STATE_DIR"
DEFAULT_STATE_DIR = Path(__file__).parent.parent / "state"
LAST_POST_STATE_FILENAME_TEMPLATE = "discord_last_post_{channel}.json"
REQUEST_TIMEOUT_SECONDS = 15

DEV_CHANNEL = "dev"
PROD_CHANNEL = "prod"

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
# than PACE_MIN_INTERVAL_SECONDS. There's only one check per hour (see
# poll.yml), so nothing is ever deferred to "next check" -- every event
# this run collects gets posted THIS run. If there isn't enough of the
# budget left to give each event its own message, extra events are bundled
# into the same message for some of the slots (see
# _assign_slots/_build_message_batches) rather than pushed to later.
# Dev and prod each get their own independent pacing pass (different
# content, different timing), sharing this same budget/floor/ceiling.
#
# PACE_MIN_INTERVAL_SECONDS is also the *cross-run* minimum gap, tracked
# separately per channel: a single new event found on its own would
# otherwise post immediately with no pacing at all, so two separate runs
# landing close together could both fire right away. The last successful
# post time per channel is persisted (see LAST_POST_STATE_FILENAME_TEMPLATE)
# so even a lone event waits out the rest of this gap since that channel's
# last post, not just gaps *within* one run's batch.
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
    """Deliver events to the dev and prod channels independently and return
    the subset fully delivered (dev, and prod too if the event was
    prod-eligible). Callers must only mark returned events as "seen" -- an
    event that fails to post, or that this run didn't get to, has to stay
    eligible for the next poll, not silently vanish into the dedupe store."""
    if not events:
        return []

    dev_urls = _dev_webhook_urls()
    prod_urls = _prod_webhook_urls()
    if not dev_urls and not prod_urls:
        logger.error("discord: %s is not set, skipping %d event(s)", WEBHOOK_URL_ENV_VAR, len(events))
        return []

    prod_eligible_ids = {event["id"] for event in events if event.get("prod_ready", True)}
    prod_events = [event for event in events if event["id"] in prod_eligible_ids]

    delivered_dev_ids = (
        _deliver_to_channel(DEV_CHANNEL, dev_urls, events) if dev_urls else {event["id"] for event in events}
    )
    delivered_prod_ids = _deliver_to_channel(PROD_CHANNEL, prod_urls, prod_events) if prod_urls else set()

    delivered = []
    for event in events:
        dev_ok = event["id"] in delivered_dev_ids
        prod_ok = (
            event["id"] not in prod_eligible_ids or not prod_urls or event["id"] in delivered_prod_ids
        )
        if dev_ok and prod_ok:
            delivered.append(event)
    return delivered


def _deliver_to_channel(channel: str, webhook_urls: list[str], events: list[dict]) -> set[str]:
    """Run the full paced posting pipeline for one channel's own event list
    and return the ids actually delivered to every webhook configured for
    that channel."""
    if not events:
        return set()

    slots = _assign_slots(events)
    logger.info(
        "discord: [%s] posting %d event(s) across %d message(s) this run", channel, len(events), len(slots)
    )

    budget_seconds = _pace_budget_seconds()
    start = time.time()
    delivered_ids: set[str] = set()
    for i, slot_events in enumerate(slots):
        if i == 0:
            _wait_for_cross_run_gap(channel)
        for batch in _build_message_batches(slot_events):
            # A plain list comprehension, not a short-circuiting all(...) --
            # every webhook in this channel must actually be attempted,
            # even if an earlier one failed, so one broken channel mirror
            # can't silently starve the others of a message they'd
            # otherwise have gotten.
            results = [_post_batch(url, batch) for url in webhook_urls]
            if all(results):
                delivered_ids.update(event["id"] for event in batch)
                _save_last_post_time(channel, time.time())
        if i + 1 < len(slots):
            remaining_gaps = len(slots) - 1 - i
            remaining_budget = max(0.0, budget_seconds - (time.time() - start))
            gap = _random_gap(remaining_gaps, remaining_budget)
            logger.info("discord: [%s] waiting %.0fs before the next post", channel, gap)
            time.sleep(gap)
    return delivered_ids


def _wait_for_cross_run_gap(channel: str) -> None:
    """Even a lone new event must not post within PACE_MIN_INTERVAL_SECONDS
    of that channel's last successfully delivered post, no matter which run
    sent it. Without this, a single-event run always posted with zero
    delay, which is how two separate runs a minute apart could both fire
    immediately and end up visually merged in Discord."""
    last_post_epoch = _load_last_post_time(channel)
    if last_post_epoch is None:
        return
    remaining = PACE_MIN_INTERVAL_SECONDS - (time.time() - last_post_epoch)
    if remaining > 0:
        logger.info(
            "discord: [%s] waiting %.0fs since the previous check's last post to keep messages spaced out",
            channel,
            remaining,
        )
        time.sleep(remaining)


def _last_post_state_path(channel: str) -> Path:
    state_dir = Path(os.environ.get(STATE_DIR_ENV_VAR, DEFAULT_STATE_DIR))
    return state_dir / LAST_POST_STATE_FILENAME_TEMPLATE.format(channel=channel)


def _load_last_post_time(channel: str) -> float | None:
    path = _last_post_state_path(channel)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("last_post_epoch")
    except Exception:
        logger.exception("discord: failed to read last-post timestamp for %s, treating as unknown", channel)
        return None


def _save_last_post_time(channel: str, epoch: float) -> None:
    path = _last_post_state_path(channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_post_epoch": epoch}, f)


def _dev_webhook_urls() -> list[str]:
    primary = os.environ.get(WEBHOOK_URL_ENV_VAR)
    return [primary] if primary else []


def _prod_webhook_urls() -> list[str]:
    """DISCORD_WEBHOOK_URL_2, _3, ... -- everything past the first webhook
    is prod tier. Returns [] unconditionally unless DISCORD_PROD_ENABLED is
    exactly "true", regardless of what's configured -- a single master
    switch so prod can be guaranteed silent while reviewing new sources in
    dev, without depending on every source's `prod` flag being correct."""
    if os.environ.get(PROD_ENABLED_ENV_VAR, "").strip().lower() != "true":
        return []
    urls = []
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
