"""CLI entrypoint: one poll cycle over configured sources and sinks.

    python -m alerts.run --sinks console
    python -m alerts.run --sinks discord
    python -m alerts.run --dry-run

Scheduler-friendly: runs once and exits. No daemons.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from alerts.dedupe import open_store
from alerts.sinks import console as console_sink

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("ALERTS_STATE_DIR", Path(__file__).parent / "state"))
SEEN_DB_PATH = STATE_DIR / "seen.sqlite3"

SINKS = {
    "console": console_sink,
}


def collect_events() -> list[dict]:
    """Fetch and normalize events from every enabled source. One dead feed
    must never kill the whole run -- each source module already guards its
    own network calls and returns [] on failure."""
    from alerts.sources import gdacs, rss, usgs

    events: list[dict] = []
    events.extend(
        usgs.fetch(
            feed_url=os.environ.get("USGS_FEED_URL", usgs.DEFAULT_FEED_URL),
            min_magnitude=float(
                os.environ.get("USGS_MIN_MAGNITUDE", usgs.DEFAULT_MIN_MAGNITUDE)
            ),
        )
    )
    events.extend(gdacs.fetch(feed_url=os.environ.get("GDACS_FEED_URL", gdacs.DEFAULT_FEED_URL)))
    events.extend(rss.fetch(cache_path=STATE_DIR / "rss_cache.json"))
    return events


def run(sink_names: list[str], dry_run: bool) -> None:
    events = collect_events()
    logger.info("collected %d event(s) from sources", len(events))

    if dry_run:
        console_sink.send(events)
        return

    with open_store(SEEN_DB_PATH) as store:
        if store.is_empty() and events:
            store.mark_seen(events)
            print(f"seeded {len(events)} events")
            return

        new_events = store.filter_unseen(events)
        logger.info("%d new event(s) after dedupe", len(new_events))

        for sink_name in sink_names:
            sink = SINKS[sink_name]
            sink.send(new_events)

        store.mark_seen(new_events)


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll world-event sources and post to sinks.")
    parser.add_argument(
        "--sinks",
        nargs="+",
        default=["console"],
        choices=sorted(SINKS.keys()),
        help="Sinks to send new events to (default: console).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print normalized events to console and skip state writes / sinks.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    run(sink_names=args.sinks, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
