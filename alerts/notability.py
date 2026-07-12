"""Cross-outlet duplicate-story suppression.

Dedupe elsewhere in this codebase (alerts/dedupe.py) is per-article: BBC's
write-up of a story and CNN's write-up of the *same real-world story* have
different URLs/ids, so they're two unrelated events as far as that store
is concerned. As more outlets get added, a single big story (e.g. a major
earthquake or a war escalation) can trigger one Discord notification per
outlet covering it -- this module catches that specific case: if a new
notable event's headline looks like the same story as one we recently
notified about (from a *different* outlet), it's suppressed here (still
archived/marked seen as usual, just not posted again).

This is a plain keyword-overlap heuristic, not real language understanding
-- zero cost, no external service, and reasonably good at catching close
headline rewrites, but it will miss genuine duplicates phrased very
differently and can occasionally suppress two distinct stories that happen
to share several significant words. Tune SIMILARITY_THRESHOLD if it's
over- or under-suppressing in practice.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW_HOURS = 36
SIMILARITY_THRESHOLD = 0.5
MIN_WORD_LEN = 4

_WORD_RE = re.compile(r"[a-z0-9]+")

# Small, deliberately generic stopword list -- common function words that
# would otherwise dominate the overlap score without saying anything about
# which *story* a headline is about.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that",
    "this", "these", "those", "with", "from", "into", "onto", "over",
    "under", "after", "before", "amid", "amidst", "about", "against",
    "between", "during", "through", "without", "within", "along", "among",
    "for", "not", "are", "was", "were", "been", "being", "has", "have",
    "had", "will", "would", "could", "should", "shall", "may", "might",
    "must", "can", "says", "said", "say", "new", "more", "most", "some",
    "what", "when", "where", "which", "while", "who", "whom", "whose",
    "why", "how", "its", "his", "her", "their", "your", "our", "you",
    "they", "them", "there", "here", "also", "just", "still", "even",
    "only", "such", "into", "out", "up", "down",
}


def suppress_similar_stories(events: list[dict], state_path: Path) -> tuple[list[dict], list[dict]]:
    """Given candidate notable events (already sorted oldest-first), return
    (keep, suppressed): `keep` is safe to actually post, `suppressed` looks
    like a repeat of a recently-notified story from a different outlet and
    should be archived without posting."""
    history = _load_history(state_path)
    now = datetime.now(timezone.utc)
    history = _prune(history, now)

    keep: list[dict] = []
    suppressed: list[dict] = []

    for event in events:
        keywords = _keywords(event["title"])
        match = _find_match(event, keywords, history)
        if match is not None:
            suppressed.append(event)
            continue
        keep.append(event)
        history.append(
            {
                "source": event["source"],
                "keywords": sorted(keywords),
                "time_utc": event["time_utc"],
            }
        )

    _save_history(state_path, history)
    return keep, suppressed


def _find_match(event: dict, keywords: set[str], history: list[dict]) -> dict | None:
    if not keywords:
        return None
    for entry in history:
        if entry["source"] == event["source"]:
            continue  # same-outlet duplicates are already handled by id-based dedupe
        overlap = keywords & set(entry["keywords"])
        union = keywords | set(entry["keywords"])
        similarity = len(overlap) / len(union) if union else 0.0
        if similarity >= SIMILARITY_THRESHOLD:
            return entry
    return None


def _keywords(title: str) -> set[str]:
    words = _WORD_RE.findall(title.lower())
    return {w for w in words if len(w) >= MIN_WORD_LEN and w not in _STOPWORDS}


def _prune(history: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    pruned = []
    for entry in history:
        try:
            entry_time = datetime.strptime(entry["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (KeyError, ValueError):
            continue
        if entry_time >= cutoff:
            pruned.append(entry)
    return pruned


def _load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(path: Path, history: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
