"""
Trend Hunter — scrapes the top 5 trending Wealth/Luxury Shorts on YouTube
every 12 hours via the YouTube Data API. Results are stored in
data/trending.json and fed into the script generator as seed enrichment.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from youtube_auth import get_youtube_service

log = logging.getLogger("trend_hunter")

TRENDING_FILE = Path("data/trending.json")
TRENDING_FILE.parent.mkdir(exist_ok=True)

SEARCH_QUERIES = ["#Wealth Shorts", "#Luxury mindset", "dark psychology money", "wealth secrets"]
REFRESH_INTERVAL = 12 * 3600
_lock = threading.Lock()
_last_refresh: float = 0.0


def _read() -> dict[str, Any]:
    if not TRENDING_FILE.exists() or TRENDING_FILE.stat().st_size == 0:
        return {"videos": [], "keywords": [], "updated_at": None}
    try:
        return json.loads(TRENDING_FILE.read_text())
    except json.JSONDecodeError:
        return {"videos": [], "keywords": [], "updated_at": None}


def _write(data: dict[str, Any]) -> None:
    TRENDING_FILE.write_text(json.dumps(data, indent=2))


def _extract_keywords_from_title(title: str) -> list[str]:
    import re
    stop = {"the", "a", "an", "is", "are", "was", "were", "you", "your", "why",
            "how", "this", "that", "they", "them", "will", "what", "when",
            "with", "for", "and", "but", "its", "it's", "in", "of", "to"}
    words = re.findall(r"[a-z]+", title.lower())
    return [w for w in words if w not in stop and len(w) > 3]


def fetch_trending() -> dict[str, Any]:
    global _last_refresh
    try:
        yt = get_youtube_service()
        seen_ids: set[str] = set()
        videos: list[dict[str, Any]] = []
        for query in SEARCH_QUERIES:
            if len(videos) >= 10:
                break
            try:
                resp = yt.search().list(
                    part="snippet",
                    q=query,
                    type="video",
                    videoDuration="short",
                    order="viewCount",
                    maxResults=5,
                ).execute()
                for item in resp.get("items", []):
                    vid_id = item.get("id", {}).get("videoId")
                    if not vid_id or vid_id in seen_ids:
                        continue
                    seen_ids.add(vid_id)
                    snippet = item.get("snippet", {})
                    videos.append({
                        "video_id": vid_id,
                        "title": snippet.get("title", ""),
                        "channel": snippet.get("channelTitle", ""),
                        "url": f"https://youtu.be/{vid_id}",
                        "query": query,
                    })
            except Exception as err:
                log.warning("Search failed for %r: %s", query, err)

        all_kw: list[str] = []
        for v in videos:
            all_kw.extend(_extract_keywords_from_title(v["title"]))

        freq: dict[str, int] = {}
        for kw in all_kw:
            freq[kw] = freq.get(kw, 0) + 1
        top_keywords = [k for k, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)][:20]

        result: dict[str, Any] = {
            "videos": videos[:10],
            "keywords": top_keywords,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with _lock:
            _write(result)
            _last_refresh = time.time()
        log.info("Trend Hunter: fetched %d trending videos, %d keywords", len(videos), len(top_keywords))
        return result
    except Exception as err:
        log.error("Trend Hunter fetch failed: %s", err)
        return _read()


def get_trending(force: bool = False) -> dict[str, Any]:
    global _last_refresh
    with _lock:
        stale = (time.time() - _last_refresh) > REFRESH_INTERVAL
    if force or stale:
        threading.Thread(target=fetch_trending, daemon=True, name="trend-hunter").start()
    return _read()


def get_trending_seed_enrichment() -> str:
    """Returns a short phrase that can be appended to a Gemini seed prompt."""
    data = _read()
    kws = data.get("keywords", [])[:8]
    if not kws:
        return ""
    return f"Currently trending keywords: {', '.join(kws)}."


# ─────────────────────────────────────────────
#  SPIKE DETECTION — Auto-Seeder support
# ─────────────────────────────────────────────

SPIKE_LOG = Path("data/spike_log.json")


def _read_spike_log() -> list[dict]:
    if not SPIKE_LOG.exists() or SPIKE_LOG.stat().st_size == 0:
        return []
    try:
        return json.loads(SPIKE_LOG.read_text())
    except json.JSONDecodeError:
        return []


def _append_spike_log(entry: dict) -> None:
    entries = _read_spike_log()
    entries.insert(0, entry)
    SPIKE_LOG.write_text(json.dumps(entries[:200], indent=2))


def snapshot_keyword_frequencies() -> dict[str, int]:
    """Save current keyword frequencies as the baseline for the next cycle."""
    data = _read()
    freq: dict[str, int] = data.get("keyword_freq", {})
    data["prev_keyword_freq"] = freq
    with _lock:
        _write(data)
    return freq


def detect_spike(threshold: float = 3.0) -> list[dict[str, object]]:
    """
    Compare current keyword frequencies against the previous cycle snapshot.
    Returns a list of spiking keyword dicts: {keyword, prev, current, ratio}.
    A spike is triggered when current >= threshold * prev (or prev == 0 and current >= 3).
    """
    data = _read()
    current: dict[str, int] = data.get("keyword_freq", {})
    prev: dict[str, int] = data.get("prev_keyword_freq", {})

    if not current:
        return []

    spikes: list[dict[str, object]] = []
    for kw, cnt in current.items():
        p = prev.get(kw, 0)
        if p == 0:
            if cnt >= 3:
                spikes.append({"keyword": kw, "prev": 0, "current": cnt, "ratio": None})
        elif cnt / p >= threshold:
            spikes.append({"keyword": kw, "prev": p, "current": cnt, "ratio": round(cnt / p, 2)})

    spikes.sort(key=lambda x: x["current"], reverse=True)
    return spikes


def fetch_trending_with_freq() -> dict[str, Any]:
    """
    Like fetch_trending() but also updates keyword_freq in the stored data
    so detect_spike() has fresh counts to compare.
    """
    result = fetch_trending()
    # rebuild freq map from the keywords list (count occurrences)
    all_kw: list[str] = []
    for v in result.get("videos", []):
        all_kw.extend(_extract_keywords_from_title(v["title"]))
    freq: dict[str, int] = {}
    for kw in all_kw:
        freq[kw] = freq.get(kw, 0) + 1
    with _lock:
        stored = _read()
        stored["keyword_freq"] = freq
        _write(stored)
    return result


def log_spike_upload(keywords: list[str], seed: str, job_id: str) -> None:
    _append_spike_log({
        "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "keywords": keywords,
        "seed": seed,
        "job_id": job_id,
    })


# ─────────────────────────────────────────────
#  PINNED KEYWORD — auto-feeds next scheduled run
# ─────────────────────────────────────────────

PINNED_KW_FILE = Path("data/pinned_keyword.json")


def pin_keyword(kw: str) -> None:
    """Save a keyword that the next scheduled run will use as its seed."""
    PINNED_KW_FILE.write_text(json.dumps({
        "keyword": kw.strip(),
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))
    log.info("Pinned keyword for next run: %r", kw)


def get_pinned_keyword() -> str | None:
    """Return the currently pinned keyword, or None if none is set."""
    if not PINNED_KW_FILE.exists() or PINNED_KW_FILE.stat().st_size == 0:
        return None
    try:
        data = json.loads(PINNED_KW_FILE.read_text())
        return data.get("keyword") or None
    except (json.JSONDecodeError, OSError):
        return None


def get_pinned_keyword_record() -> dict | None:
    """Return the full pinned keyword record (keyword + pinned_at), or None."""
    if not PINNED_KW_FILE.exists() or PINNED_KW_FILE.stat().st_size == 0:
        return None
    try:
        data = json.loads(PINNED_KW_FILE.read_text())
        return data if data.get("keyword") else None
    except (json.JSONDecodeError, OSError):
        return None


def clear_pinned_keyword() -> None:
    """Clear the pinned keyword after it has been consumed by a scheduled run."""
    if PINNED_KW_FILE.exists():
        PINNED_KW_FILE.write_text(json.dumps({}))
    log.info("Pinned keyword cleared after use.")
