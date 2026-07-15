"""Website sink: POSTs new notable events as one JSON batch to an external
HTTP endpoint, so another project (e.g. the live world-events map website)
can consume the same normalized events without this repo needing to know
anything about how that project stores or displays them. See
WEBSITE_INTEGRATION.md for the receiving side's contract.

Off by default -- do nothing unless WEBSITE_WEBHOOK_URL is set AND
`website` is passed to --sinks (e.g. `--sinks discord website`). Not
wired into poll.yml yet; add it there once a real endpoint exists.
"""

from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV_VAR = "WEBSITE_WEBHOOK_URL"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3


def send(events: list[dict]) -> list[dict]:
    """POST every event (unmodified EVENT dicts) as one JSON batch. All or
    nothing: either the whole batch is confirmed delivered (2xx response)
    or none of it is -- a partial failure on the receiving end can't split
    a run's events between "seen, kept" and "seen, silently dropped"."""
    if not events:
        return []

    url = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if not url:
        logger.error("website: %s is not set, skipping %d event(s)", WEBHOOK_URL_ENV_VAR, len(events))
        return []

    if _post(url, events):
        return events
    return []


def _post(url: str, events: list[dict], retries_left: int = MAX_RETRIES) -> bool:
    payload = {"events": events}
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("website: request failed for %d event(s)", len(events))
        return False

    if response.status_code == 429 and retries_left > 0:
        retry_after = _extract_retry_after(response)
        logger.warning("website: rate-limited, waiting %.1fs", retry_after)
        time.sleep(retry_after)
        return _post(url, events, retries_left=retries_left - 1)

    if not response.ok:
        logger.error(
            "website: webhook post failed with status %d for %d event(s): %s",
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
