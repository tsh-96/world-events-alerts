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
# News and earth events age differently: headlines are stale in days,
# while the map site wants quakes/disasters to linger (its Earth-events
# tab falls back to a two-week view when the last week was quiet). The
# caps are per class so a busy news week can never push a quake out of
# the file early; the earth cap is far above any realistic two weeks.
NEWS_WINDOW_DAYS_ENV_VAR = "WEBFILE_NEWS_WINDOW_DAYS"
DEFAULT_NEWS_WINDOW_DAYS = 7
EARTH_WINDOW_DAYS_ENV_VAR = "WEBFILE_EARTH_WINDOW_DAYS"
DEFAULT_EARTH_WINDOW_DAYS = 14
MAX_NEWS_ENV_VAR = "WEBFILE_MAX_NEWS"
DEFAULT_MAX_NEWS = 600
MAX_EARTH_ENV_VAR = "WEBFILE_MAX_EARTH"
DEFAULT_MAX_EARTH = 400


def send(events: list[dict]) -> list[dict]:
    """Merge the batch into the rolling file. Returns the whole batch on
    success, [] on any failure (all-or-nothing, same as the other sinks)."""
    path = Path(os.environ.get(FILE_PATH_ENV_VAR) or DEFAULT_FILE_PATH)
    news_days = int(os.environ.get(NEWS_WINDOW_DAYS_ENV_VAR, DEFAULT_NEWS_WINDOW_DAYS))
    earth_days = int(os.environ.get(EARTH_WINDOW_DAYS_ENV_VAR, DEFAULT_EARTH_WINDOW_DAYS))
    max_news = int(os.environ.get(MAX_NEWS_ENV_VAR, DEFAULT_MAX_NEWS))
    max_earth = int(os.environ.get(MAX_EARTH_ENV_VAR, DEFAULT_MAX_EARTH))

    try:
        existing: list[dict] = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8")).get("events", [])

        by_id = {e["id"]: e for e in existing}
        for event in events:
            by_id[event["id"]] = event

        # time_utc is always "YYYY-MM-DDTHH:MM:SSZ", so plain string
        # comparison IS chronological comparison -- no parsing needed.
        def cutoff(days: int) -> str:
            return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        news_cutoff, earth_cutoff = cutoff(news_days), cutoff(earth_days)
        news, earth = [], []
        for e in by_id.values():
            if e.get("kind") == "news":
                if (e.get("time_utc") or "") >= news_cutoff:
                    news.append(e)
            elif (e.get("time_utc") or "") >= earth_cutoff:
                earth.append(e)
        news.sort(key=lambda e: e["time_utc"], reverse=True)
        earth.sort(key=lambda e: e["time_utc"], reverse=True)
        merged = news[:max_news] + earth[:max_earth]
        merged.sort(key=lambda e: e["time_utc"], reverse=True)

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
