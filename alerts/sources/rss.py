"""Generic RSS/Atom news source, config-driven via config/feeds.yaml.

Adding a news source is a config change (feeds.yaml), not a code change.
No geocoding here by design -- lat/lon/place stay null; a future consumer
(e.g. the map website) can add that step.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import feedparser
import requests
import yaml

from alerts.normalize import make_event, strip_html, struct_time_to_iso

logger = logging.getLogger(__name__)

DEFAULT_FEEDS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "feeds.yaml"
DEFAULT_CACHE_PATH = Path(__file__).parent.parent / "state" / "rss_cache.json"
DEFAULT_KIND = "news"
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "world-events-alerts/1.0 (+https://github.com/; polite RSS poller, "
    "see repo for contact)"
)


def fetch(
    feeds_config_path: str | Path = DEFAULT_FEEDS_CONFIG_PATH,
    cache_path: str | Path = DEFAULT_CACHE_PATH,
) -> list[dict]:
    """Fetch every configured feed. An outlet can list more than one feed
    under the same `slug` (e.g. its full section feed plus its curated
    top-stories feed) -- when the same article shows up in both, they're
    merged into a single event rather than posted twice, and that event is
    `notable` if it appeared in *any* feed marked `notify: true`, and
    `prod_ready` if it appeared in any feed marked `prod: true`, for that
    outlet."""
    feeds = _load_feeds_config(feeds_config_path)
    cache = _load_cache(cache_path)

    events_by_id: dict[str, dict] = {}
    for feed_cfg in feeds:
        slug = feed_cfg.get("slug")
        url = feed_cfg.get("url")
        kind = feed_cfg.get("kind", DEFAULT_KIND)
        notify = bool(feed_cfg.get("notify", False))
        prod = bool(feed_cfg.get("prod", False))
        if not slug or not url:
            logger.warning("rss: skipping malformed feed config entry: %r", feed_cfg)
            continue

        try:
            feed_events = _fetch_one_feed(slug, url, kind, notify, prod, cache)
        except Exception:
            logger.exception("rss: failed to fetch feed %s (%s)", slug, url)
            continue
        logger.info(
            "rss: %s -> %d item(s) (notify=%s, prod=%s, %s)", slug, len(feed_events), notify, prod, url
        )

        for event in feed_events:
            existing = events_by_id.get(event["id"])
            if existing is None:
                events_by_id[event["id"]] = event
            else:
                if event["notable"] and not existing["notable"]:
                    existing["notable"] = True
                if event["prod_ready"] and not existing["prod_ready"]:
                    existing["prod_ready"] = True

    _save_cache(cache_path, cache)
    return list(events_by_id.values())


def _fetch_one_feed(slug: str, url: str, kind: str, notify: bool, prod: bool, cache: dict) -> list[dict]:
    # Cached by URL, not slug -- an outlet can have more than one feed URL
    # sharing a slug, and each needs its own independent conditional-GET
    # cache entry.
    cached = cache.get(url, {})
    headers = {"User-Agent": USER_AGENT}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("modified"):
        headers["If-Modified-Since"] = cached["modified"]

    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=headers)

    if response.status_code == 304:
        return []

    response.raise_for_status()

    new_cache_entry = dict(cached)
    if "ETag" in response.headers:
        new_cache_entry["etag"] = response.headers["ETag"]
    if "Last-Modified" in response.headers:
        new_cache_entry["modified"] = response.headers["Last-Modified"]
    cache[url] = new_cache_entry

    parsed = feedparser.parse(response.content)

    events = []
    for item in parsed.entries:
        try:
            event = _normalize_item(slug, kind, notify, prod, item)
        except Exception:
            logger.exception("rss: failed to normalize item from %s: %r", slug, item.get("link"))
            continue
        if event is not None:
            events.append(event)
    return events


def _normalize_item(slug: str, kind: str, notify: bool, prod: bool, item) -> dict | None:
    native_id = item.get("id") or item.get("link")
    if not native_id:
        logger.warning("rss: %s item has no guid/id/link, skipping", slug)
        return None

    time_utc = struct_time_to_iso(item.get("published_parsed")) or struct_time_to_iso(
        item.get("updated_parsed")
    )
    if time_utc is None:
        logger.warning("rss: %s item %s missing publish time, skipping", slug, native_id)
        return None

    title = strip_html(item.get("title"))
    if not title:
        logger.warning("rss: %s item %s missing title, skipping", slug, native_id)
        return None

    return make_event(
        source=slug,
        native_id=native_id,
        kind=kind,
        severity=None,
        title=title,
        summary=strip_html(item.get("summary")),
        lat=None,
        lon=None,
        place=None,
        country=None,
        time_utc=time_utc,
        url=item.get("link"),
        notable=notify,
        prod_ready=prod,
    )


def _load_feeds_config(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        logger.warning("rss: feeds config not found at %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", [])


def _load_cache(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("rss: failed to load cache %s, starting fresh", path)
        return {}


def _save_cache(path: str | Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
