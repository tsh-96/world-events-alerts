"""Shared helpers for turning raw source data into EVENT dicts.

See README.md #event-schema for the authoritative field list. Every source
module should build its events through `make_event` so the schema stays
consistent.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

EVENT_FIELDS = (
    "id",
    "source",
    "kind",
    "severity",
    "title",
    "summary",
    "lat",
    "lon",
    "place",
    "country",
    "time_utc",
    "url",
)


def make_event(
    *,
    source: str,
    native_id: str,
    kind: str,
    severity: float | int | None,
    title: str,
    summary: str | None,
    lat: float | None,
    lon: float | None,
    place: str | None,
    country: str | None,
    time_utc: str,
    url: str,
) -> dict:
    """Build an EVENT dict matching the schema in README.md."""
    return {
        "id": f"{source}:{native_id}",
        "source": source,
        "kind": kind,
        "severity": severity,
        "title": title,
        "summary": summary,
        "lat": lat,
        "lon": lon,
        "place": place,
        "country": country,
        "time_utc": time_utc,
        "url": url,
    }


def epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds (e.g. USGS `properties.time`) to ISO 8601 UTC."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def datetime_to_iso(dt: datetime) -> str:
    """Convert an aware or naive datetime to ISO 8601 UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def struct_time_to_iso(struct_time) -> str | None:
    """Convert a `time.struct_time` (as returned by feedparser) to ISO 8601 UTC."""
    if struct_time is None:
        return None
    dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
    return datetime_to_iso(dt)


def strip_html(text: str | None) -> str | None:
    """Strip HTML tags and unescape entities, collapsing whitespace. News titles
    and summaries can contain arbitrary markup or scripts; this always returns
    plain text, never markup."""
    if text is None:
        return None
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = html.unescape(no_tags)
    collapsed = _WHITESPACE_RE.sub(" ", unescaped).strip()
    return collapsed or None
