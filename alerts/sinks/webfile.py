"""Webfile sink: maintains a rolling-window JSON file of recent events
(`public/events.json`), which the poll workflow commits back to this
public repo. A static website can then fetch it straight from
raw.githubusercontent.com (served with CORS enabled) -- no server, no
database, no webhook endpoint, no secrets. This is the integration the
map-website side chose over the POST-based `website` sink for v1, since
that site is static files on Netlify with no backend to receive a POST.

Behavior:
- The file is merged, never blindly overwritten: existing events are
  kept, new ones added (same id never duplicated -- re-deliveries after
  a partially-failed run are harmless), events older than the window are
  pruned, newest first, capped at a max count so the file stays small.
- File shape: {"generated_utc": "...", "events": [EVENT, ...]} where
  each EVENT is the unmodified dict from the schema in README.md.
- Failure behavior matches the other sinks' contract: any problem means
  the whole batch is reported undelivered (return []) and retried next
  run. NOTE for poll.yml: list this sink BEFORE discord in --sinks -- a
  hard failure here then stops the run before Discord posts anything,
  instead of leaving Discord delivered-but-unconfirmed (which would
  repost the same messages next hour).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_PATH_ENV_VAR = "WEBFILE_PATH"  # override in tests; default is in-repo
DEFAULT_FILE_PATH = Path(__file__).resolve().parent.parent.parent / "public" / "events.json"
WINDOW_DAYS_ENV_VAR = "WEBFILE_WINDOW_DAYS"
DEFAULT_WINDOW_DAYS = 7
MAX_EVENTS_ENV_VAR = "WEBFILE_MAX_EVENTS"
DEFAULT_MAX_EVENTS = 1000


def send(events: list[dict]) -> list[dict]:
    """Merge the batch into the rolling file. Returns the whole batch on
    success, [] on any failure (all-or-nothing, same as the other sinks)."""
    path = Path(os.environ.get(FILE_PATH_ENV_VAR) or DEFAULT_FILE_PATH)
    window_days = int(os.environ.get(WINDOW_DAYS_ENV_VAR, DEFAULT_WINDOW_DAYS))
    max_events = int(os.environ.get(MAX_EVENTS_ENV_VAR, DEFAULT_MAX_EVENTS))

    try:
        existing: list[dict] = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8")).get("events", [])

        by_id = {e["id"]: e for e in existing}
        for event in events:
            by_id[event["id"]] = event

        # time_utc is always "YYYY-MM-DDTHH:MM:SSZ", so plain string
        # comparison IS chronological comparison -- no parsing needed.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        merged = [e for e in by_id.values() if (e.get("time_utc") or "") >= cutoff]
        merged.sort(key=lambda e: e["time_utc"], reverse=True)
        merged = merged[:max_events]

        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "events": merged,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # Write-then-rename so a crash mid-write can't leave a truncated
        # (unparseable) file behind for the website to choke on.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(body + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.exception(
            "webfile: failed to update %s; %d event(s) will be retried next run",
            path,
            len(events),
        )
        return []

    logger.info("webfile: %s now holds %d event(s)", path, len(merged))
    return events
