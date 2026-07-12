"""Duplicate-story suppression, within one outlet and across outlets.

Dedupe elsewhere in this codebase (alerts/dedupe.py) is per-article: two
different write-ups of the *same real-world story* -- whether from two
different outlets, or the same outlet publishing several articles about
one event (e.g. five separate Al Jazeera pieces about one head of state's
death) -- have different URLs/ids, so they're unrelated events as far as
that store is concerned and would all post individually. This module
catches that: if a new notable event's headline looks like the same story
as one or more events we recently notified about (from any outlet,
including the same one), it's suppressed once we've already posted
MAX_SIMILAR_PER_STORY notifications about it (still archived/marked seen
as usual, just not posted again) -- allows the first mention through
immediately, plus one follow-up, rather than every outlet's (or the same
outlet's every) angle on it.

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
MAX_SIMILAR_PER_STORY = 2

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
    like the same story as MAX_SIMILAR_PER_STORY or more already-notified
    events (from any outlet) and should be archived without posting."""
    history = _load_history(state_path)
    now = datetime.now(timezone.utc)
    history = _prune(history, now)

    keep: list[dict] = []
    suppressed: list[dict] = []

    for event in events:
        keywords = _keywords(event["title"])
        if keywords and _count_similar(keywords, history) >= MAX_SIMILAR_PER_STORY:
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


def _count_similar(keywords: set[str], history: list[dict]) -> int:
    count = 0
    for entry in history:
        entry_keywords = set(entry["keywords"])
        overlap = keywords & entry_keywords
        # Overlap coefficient (shared words / size of the SMALLER headline),
        # not Jaccard (shared / union) -- two headlines about the same
        # story often share a tight cluster of proper nouns (names, places)
        # but otherwise use completely different words to describe what
        # happened. Jaccard's union in the denominator penalizes that extra
        # wording even when the entity overlap is a near-total match;
        # overlap coefficient only asks "does the smaller headline's word
        # set sit almost entirely inside the bigger one," which is a much
        # better fit for "same story, different framing/outlet."
        smaller = min(len(keywords), len(entry_keywords))
        similarity = len(overlap) / smaller if smaller else 0.0
        if similarity >= SIMILARITY_THRESHOLD:
            count += 1
    return count


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
