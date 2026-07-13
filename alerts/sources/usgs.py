"""USGS earthquake source.

Fetches the USGS GeoJSON summary feed and normalizes each feature into an
EVENT dict. Pure fetch-and-normalize: no sinks, no state.
"""

from __future__ import annotations

import logging

import requests

from alerts.normalize import epoch_ms_to_iso, make_event

logger = logging.getLogger(__name__)

SOURCE = "usgs"

# "all_hour" / "all_day" / "2.5_day" / "significant_month" etc are all valid
# feed names under this base URL; see
# https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
DEFAULT_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
DEFAULT_MIN_MAGNITUDE = 0.0
REQUEST_TIMEOUT_SECONDS = 15


def fetch(
    feed_url: str = DEFAULT_FEED_URL,
    min_magnitude: float = DEFAULT_MIN_MAGNITUDE,
) -> list[dict]:
    try:
        response = requests.get(
            feed_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "world-events-alerts (github.com repo bot)"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.exception("usgs: failed to fetch %s", feed_url)
        return []

    features = data.get("features", [])
    events = []
    for feature in features:
        try:
            event = _normalize_feature(feature)
        except Exception:
            logger.exception("usgs: failed to normalize feature %r", feature.get("id"))
            continue
        if event is None:
            continue
        if event["severity"] is not None and event["severity"] < min_magnitude:
            continue
        events.append(event)
    logger.info(
        "usgs: %d item(s) at/above min magnitude %s (%d raw feature(s) in feed, %s)",
        len(events),
        min_magnitude,
        len(features),
        feed_url,
    )
    return events


def _normalize_feature(feature: dict) -> dict | None:
    native_id = feature.get("id")
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or [None, None, None]

    if native_id is None or props.get("time") is None:
        return None

    lon, lat = coords[0], coords[1]

    return make_event(
        source=SOURCE,
        native_id=native_id,
        kind="earthquake",
        severity=props.get("mag"),
        title=props.get("title") or props.get("place") or f"M{props.get('mag')} earthquake",
        summary=None,
        lat=lat,
        lon=lon,
        place=props.get("place"),
        country=None,
        time_utc=epoch_ms_to_iso(props["time"]),
        url=props.get("url"),
    )
