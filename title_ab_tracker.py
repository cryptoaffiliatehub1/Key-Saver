"""
Title A/B Tracker — real-view feedback loop for the SEO Oracle.

Every 24 hours this module:
  1. Reads uploads.json to find videos that are 24h+ old.
  2. Fetches current view counts via the YouTube Data API.
  3. Calls seo_oracle.record_winner() for any video that has crossed
     the VIEW_THRESHOLD — feeding real performance data back into the
     hook_memory scoring model so future title generation gets smarter.
  4. Persists a tracking ledger (data/ab_tracker.json) so each video
     is only recorded once — no duplicate inflation of the memory.
  5. Exposes get_top_patterns() — called by the admin dashboard to render
     a Title Intelligence panel showing what hooks are winning.

Error philosophy: every external call is wrapped. This module NEVER
crashes the server or the render thread.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import seo_oracle

log = logging.getLogger("title_ab_tracker")

UPLOADS_FILE   = Path("data/uploads.json")
TRACKER_FILE   = Path("data/ab_tracker.json")
HOOK_MEMORY    = Path("data/hook_memory.json")
MIN_AGE_HOURS  = 24      # only score videos that have been live for at least 24 h
VIEW_THRESHOLD = 100     # minimum views before we call record_winner()
_lock = threading.Lock()


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _hours_since(iso_ts: str) -> float:
    try:
        uploaded = dt.datetime.fromisoformat(iso_ts.rstrip("Z")).replace(
            tzinfo=dt.timezone.utc
        )
        return (dt.datetime.now(dt.timezone.utc) - uploaded).total_seconds() / 3600
    except Exception:
        return 0.0


# ── YouTube stats fetch ───────────────────────────────────────────────────────

def _fetch_views(video_id: str) -> int | None:
    """Return current view count for video_id, or None on error."""
    try:
        from youtube_auth import get_youtube_service
        yt   = get_youtube_service()
        resp = yt.videos().list(part="statistics", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return None
        return int(items[0].get("statistics", {}).get("viewCount", 0))
    except Exception as exc:
        log.debug("title_ab_tracker: view fetch failed for %s: %s", video_id, exc)
        return None


# ── Pattern analysis ──────────────────────────────────────────────────────────

_PSYCH_TRIGGERS = {
    "curiosity":  re.compile(r"\b(secret|hidden|they don'?t|silent|why|truth|dark)\b", re.I),
    "fomo":       re.compile(r"\b(if you|before|don'?t miss|losing|you'?re already)\b", re.I),
    "social":     re.compile(r"\b(1%|elite|billionaire|rich|everyone|they all)\b", re.I),
    "urgency":    re.compile(r"\b(now|stop|immediately|today|warning|destroy)\b", re.I),
    "identity":   re.compile(r"\b(you'?re|your|broke|failure|poor|weak|behind)\b", re.I),
}

_EMOJI_RE = re.compile(
    "[\U00002600-\U000027BF"
    "\U0001F300-\U0001F9FF"
    "\U00002702-\U000027B0]+",
    flags=re.UNICODE,
)


def _classify_trigger(title: str) -> str:
    """Return the dominant psychological trigger in a title."""
    best, best_n = "curiosity", 0
    for name, pattern in _PSYCH_TRIGGERS.items():
        n = len(pattern.findall(title))
        if n > best_n:
            best, best_n = name, n
    return best


def _has_emoji(title: str) -> bool:
    return bool(_EMOJI_RE.search(title))


# ── Core poll loop ────────────────────────────────────────────────────────────

def poll_and_record() -> dict[str, Any]:
    """
    Main entry point — called by the 24-hour scheduler job and the manual
    dashboard trigger.

    Returns a summary dict:
        {
          "checked":   <int>,   # videos inspected
          "recorded":  <int>,   # new winners written to hook_memory
          "skipped":   <int>,   # already recorded or below threshold
          "top_title": <str>,   # highest-view title in this batch
          "top_views": <int>,
        }
    """
    with _lock:
        uploads = _read_json(UPLOADS_FILE, [])
        ledger  = _read_json(TRACKER_FILE, {})   # {video_id: {"recorded": bool, "views": int}}

        checked  = 0
        recorded = 0
        skipped  = 0
        top_title = ""
        top_views = 0

        for rec in uploads:
            vid   = rec.get("video_id", "")
            title = rec.get("title", "")
            seed  = rec.get("seed", "")
            ts    = rec.get("uploaded_at", "")

            if not vid or not title:
                continue
            if _hours_since(ts) < MIN_AGE_HOURS:
                continue

            # Already surpassed threshold and recorded — skip
            if ledger.get(vid, {}).get("recorded"):
                skipped += 1
                continue

            checked += 1
            views = _fetch_views(vid)
            if views is None:
                continue

            # Update ledger regardless
            ledger[vid] = {"recorded": False, "views": views, "title": title}

            if views >= VIEW_THRESHOLD:
                try:
                    seo_oracle.record_winner(title, views, seed)
                    ledger[vid]["recorded"] = True
                    recorded += 1
                    log.info(
                        "title_ab_tracker: recorded winner %r  views=%d  seed=%r",
                        title, views, seed,
                    )
                except Exception as exc:
                    log.warning("title_ab_tracker: record_winner failed: %s", exc)
            else:
                skipped += 1

            if views > top_views:
                top_views = views
                top_title = title

        _write_json(TRACKER_FILE, ledger)
        log.info(
            "title_ab_tracker: poll done — checked=%d  recorded=%d  skipped=%d",
            checked, recorded, skipped,
        )
        return {
            "checked": checked,
            "recorded": recorded,
            "skipped": skipped,
            "top_title": top_title,
            "top_views": top_views,
        }


# ── Pattern intelligence for dashboard ───────────────────────────────────────

def get_top_patterns(n: int = 5) -> list[dict[str, Any]]:
    """
    Read hook_memory.json and return the top-n title patterns ranked by views.

    Each entry:
        {
          "title":    str,
          "views":    int,
          "seed":     str,
          "trigger":  str,   # curiosity | fomo | social | urgency | identity
          "has_emoji": bool,
        }
    """
    memory = _read_json(HOOK_MEMORY, [])
    if not memory:
        return []
    # Sort by views desc, deduplicate by title
    seen: set[str] = set()
    ranked: list[dict[str, Any]] = []
    for entry in sorted(memory, key=lambda x: x.get("views", 0), reverse=True):
        t = entry.get("title", "")
        if not t or t in seen:
            continue
        seen.add(t)
        ranked.append({
            "title":     t,
            "views":     int(entry.get("views", 0)),
            "seed":      entry.get("seed", ""),
            "trigger":   _classify_trigger(t),
            "has_emoji": _has_emoji(t),
        })
        if len(ranked) >= n:
            break
    return ranked


def get_trigger_breakdown() -> dict[str, int]:
    """
    Count how many winning titles belong to each trigger category.
    Used for the bar-chart summary in the dashboard.
    """
    memory = _read_json(HOOK_MEMORY, [])
    counts: Counter[str] = Counter()
    for entry in memory:
        t = entry.get("title", "")
        if t:
            counts[_classify_trigger(t)] += 1
    return dict(counts)


def get_summary() -> dict[str, Any]:
    """Lightweight summary for dashboard rendering — no external calls."""
    ledger  = _read_json(TRACKER_FILE, {})
    memory  = _read_json(HOOK_MEMORY,  [])
    tracked = len([v for v in ledger.values() if v.get("recorded")])
    return {
        "tracked_winners": tracked,
        "memory_size":     len(memory),
        "top_patterns":    get_top_patterns(5),
        "trigger_counts":  get_trigger_breakdown(),
    }
