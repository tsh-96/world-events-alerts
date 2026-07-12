"""GDACS (Global Disaster Alert and Coordination System) source.

Parses the GDACS RSS feed, which carries disaster metadata via the
`gdacs:` namespace and coordinates via the W3C basic-geo `geo:` namespace.
feedparser flattens both onto each entry (e.g. `gdacs:eventtype` ->
`entry.gdacs_eventtype`, `geo:lat` -> `entry.geo_lat`).
"""

from __future__ import annotations

import logging

import feedparser
import requests

from alerts.normalize import make_event, strip_html, struct_time_to_iso

logger = logging.getLogger(__name__)

SOURCE = "gdacs"

DEFAULT_FEED_URL = "https://www.gdacs.org/xml/rss.xml"
REQUEST_TIMEOUT_SECONDS = 15
# Alert level 1=Green (minor, no significant impact expected), 2=Orange,
# 3=Red. Green fires constantly worldwide (satellite-detected, mostly
# agricultural/minor burns) and drowns out everything else if not filtered.
DEFAULT_MIN_SEVERITY = 1

# gdacs:eventtype -> our kind. Deliberately overlaps with USGS for EQ: the
# two sources use different, non-colliding ids, so both are kept (see
# README "Source overlap" note) rather than skipping GDACS earthquakes.
EVENT_TYPE_TO_KIND = {
    "EQ": "earthquake",
    "TC": "cyclone",
    "FL": "flood",
    "VO": "volcano",
    "WF": "wildfire",
    "DR": "drought",
    "TS": "tsunami",
}

ALERT_LEVEL_TO_SEVERITY = {
    "green": 1,
    "orange": 2,
    "red": 3,
}


def fetch(feed_url: str = DEFAULT_FEED_URL, min_severity: int = DEFAULT_MIN_SEVERITY) -> list[dict]:
    try:
        response = requests.get(
            feed_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "world-events-alerts (github.com repo bot)"},
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception:
        logger.exception("gdacs: failed to fetch %s", feed_url)
        return []

    events = []
    for entry in parsed.entries:
        try:
            event = _normalize_entry(entry)
        except Exception:
            logger.exception("gdacs: failed to normalize entry %r", entry.get("id"))
            continue
        if event is None:
            continue
        if event["severity"] is not None and event["severity"] < min_severity:
            continue
        events.append(event)
    return events


def _normalize_entry(entry) -> dict | None:
    event_id = entry.get("gdacs_eventid")
    event_type = entry.get("gdacs_eventtype")
    if not event_id or not event_type:
        logger.warning("gdacs: entry missing eventid/eventtype, skipping: %r", entry.get("link"))
        return None

    episode_id = entry.get("gdacs_episodeid")
    native_id = f"{event_id}:{episode_id}" if episode_id else str(event_id)

    lat = _to_float(entry.get("geo_lat"))
    lon = _to_float(entry.get("geo_long"))
    if lat is None or lon is None:
        logger.warning("gdacs: entry %s missing coordinates, skipping", native_id)
        return None

    kind = EVENT_TYPE_TO_KIND.get(event_type, event_type.lower())

    alert_level = (entry.get("gdacs_alertlevel") or "").lower()
    severity = ALERT_LEVEL_TO_SEVERITY.get(alert_level)

    country = entry.get("gdacs_iso3") or None
    place = entry.get("gdacs_country") or None

    time_utc = struct_time_to_iso(entry.get("published_parsed")) or struct_time_to_iso(
        entry.get("updated_parsed")
    )
    if time_utc is None:
        logger.warning("gdacs: entry %s missing publish time, skipping", native_id)
        return None

    return make_event(
        source=SOURCE,
        native_id=native_id,
        kind=kind,
        severity=severity,
        title=strip_html(entry.get("title")) or "",
        summary=strip_html(entry.get("summary")),
        lat=lat,
        lon=lon,
        place=place,
        country=country,
        time_utc=time_utc,
        url=entry.get("link"),
    )


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
