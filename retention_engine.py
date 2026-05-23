"""
Retention Analytics Engine — 48-hour hook performance feedback loop.

Every 48 hours this engine:
  1. Fetches views, watch-time, and average retention % for every uploaded
     video via the YouTube Data API and YouTube Analytics API.
  2. Scores each video's hook against a retention formula:
       score = (avg_retention_pct * 0.5) + (views_per_hour * 0.3) + (watch_minutes * 0.2)
  3. Writes scored hook patterns into data/hook_memory.json so the next
     script-generation cycle can draw from what actually held viewers.
  4. Exposes get_prompt_enrichment() — called by viral_engine before every
     script generation to inject the top 5 retention-winning hook patterns
     and the current average retention benchmark.

All errors are caught silently. This module never crashes the render thread.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("retention_engine")

HOOK_MEMORY_FILE   = Path("data/hook_memory.json")
RETENTION_FILE     = Path("data/retention_scores.json")
UPLOADS_FILE       = Path("data/uploads.json")
SETTLE_WINDOW_HOURS = 48
_lock = threading.Lock()


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _hours_since(iso_ts: str) -> float:
    try:
        uploaded = dt.datetime.fromisoformat(iso_ts.rstrip("Z")).replace(tzinfo=dt.timezone.utc)
        delta = dt.datetime.now(dt.timezone.utc) - uploaded
        return delta.total_seconds() / 3600
    except Exception:
        return 0.0


# ── YouTube data fetching ────────────────────────────────────────────────────

def _fetch_stats(video_id: str) -> dict[str, Any]:
    try:
        from youtube_auth import get_youtube_service
        yt = get_youtube_service()
        resp = yt.videos().list(part="statistics,snippet", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return {}
        stats   = items[0].get("statistics", {})
        snippet = items[0].get("snippet", {})
        return {
            "views":       int(stats.get("viewCount",    0)),
            "likes":       int(stats.get("likeCount",    0)),
            "comments":    int(stats.get("commentCount", 0)),
            "published_at": snippet.get("publishedAt", ""),
        }
    except Exception as err:
        log.debug("retention: stats fetch failed for %s: %s", video_id, err)
        return {}


def _fetch_retention(video_id: str) -> dict[str, Any]:
    try:
        from youtube_auth import get_youtube_analytics_service
        ya    = get_youtube_analytics_service()
        end   = dt.date.today()
        start = end - dt.timedelta(days=90)
        resp  = ya.reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
            dimensions="video",
            filters=f"video=={video_id}",
        ).execute()
        rows = resp.get("rows") or []
        if not rows:
            return {}
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        row = dict(zip(headers, rows[0]))
        return {
            "retention_pct":    float(row.get("averageViewPercentage") or 0),
            "avg_view_seconds": float(row.get("averageViewDuration")   or 0),
            "watch_minutes":    float(row.get("estimatedMinutesWatched") or 0),
        }
    except Exception as err:
        log.debug("retention: analytics fetch failed for %s: %s", video_id, err)
        return {}


# ── scoring ──────────────────────────────────────────────────────────────────

def _score(views: int, retention_pct: float, watch_minutes: float, hours_live: float) -> float:
    """
    Retention-weighted engagement score:
      - retention_pct  (0-100) weighted at 50% — the single strongest signal
      - views_per_hour weighted at 30% — velocity matters for the algorithm
      - watch_minutes  weighted at 20% — absolute watch time signal
    """
    if hours_live <= 0:
        hours_live = 1
    views_per_hour = views / hours_live
    return (retention_pct * 0.50) + (min(views_per_hour, 500) * 0.06) + (min(watch_minutes, 1000) * 0.02)


# ── core analysis loop ────────────────────────────────────────────────────────

def run_retention_analysis() -> dict[str, Any]:
    """
    Main entry point — called by the 48-hour scheduler job and can also be
    triggered manually from the dashboard.

    Returns a summary dict with counts and top hooks.
    """
    uploads = _read_json(UPLOADS_FILE, [])
    if not uploads:
        log.info("retention: no uploads to analyse.")
        return {"analysed": 0, "top_hooks": []}

    now_scores: list[dict[str, Any]] = []
    analysed = 0

    for record in uploads:
        vid     = record.get("video_id", "")
        hook    = record.get("hook") or record.get("title", "")
        seed    = record.get("seed", "")
        ts      = record.get("uploaded_at", "")

        if not vid or not hook:
            continue

        hours_live = _hours_since(ts)
        if hours_live < SETTLE_WINDOW_HOURS:
            log.debug("retention: %s only %.1fh old — skipping", vid, hours_live)
            continue

        stats     = _fetch_stats(vid)
        analytics = _fetch_retention(vid)

        views         = stats.get("views", 0)
        retention_pct = analytics.get("retention_pct", 0.0)
        watch_minutes = analytics.get("watch_minutes", 0.0)
        avg_view_sec  = analytics.get("avg_view_seconds", 0.0)

        score = _score(views, retention_pct, watch_minutes, hours_live)

        entry: dict[str, Any] = {
            "video_id":       vid,
            "hook":           hook,
            "seed":           seed,
            "score":          round(score, 3),
            "views":          views,
            "retention_pct":  round(retention_pct, 1),
            "avg_view_sec":   round(avg_view_sec, 1),
            "watch_minutes":  round(watch_minutes, 1),
            "hours_live":     round(hours_live, 1),
            "analysed_at":    dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        now_scores.append(entry)
        analysed += 1

    if not now_scores:
        return {"analysed": 0, "top_hooks": []}

    # Merge into retention_scores.json (deduplicate by video_id, keep highest score)
    with _lock:
        existing = _read_json(RETENTION_FILE, [])
        by_id: dict[str, dict] = {e["video_id"]: e for e in existing}
        for entry in now_scores:
            vid = entry["video_id"]
            if vid not in by_id or entry["score"] > by_id[vid].get("score", 0):
                by_id[vid] = entry
        merged = sorted(by_id.values(), key=lambda x: x["score"], reverse=True)
        _write_json(RETENTION_FILE, merged[:500])

        # Update hook_memory.json with retention-scored entries
        memory = _read_json(HOOK_MEMORY_FILE, [])
        mem_by_hook: dict[str, dict] = {m.get("hook", ""): m for m in memory}
        for entry in now_scores:
            hook = entry["hook"]
            existing_mem = mem_by_hook.get(hook, {})
            mem_by_hook[hook] = {
                **existing_mem,
                "hook":           hook,
                "views":          entry["views"],
                "seed":           entry["seed"],
                "retention_pct":  entry["retention_pct"],
                "retention_score": entry["score"],
                "updated_at":     entry["analysed_at"],
            }
        sorted_mem = sorted(mem_by_hook.values(), key=lambda x: x.get("retention_score", 0), reverse=True)
        _write_json(HOOK_MEMORY_FILE, sorted_mem[:200])

    top_hooks = [e["hook"] for e in merged[:5]]
    log.info("retention: analysed %d videos — top hook: %r (score=%.1f)",
             analysed, top_hooks[0] if top_hooks else "", merged[0]["score"] if merged else 0)

    return {"analysed": analysed, "top_hooks": top_hooks, "scores": merged[:10]}


# ── prompt enrichment API ─────────────────────────────────────────────────────

def get_prompt_enrichment() -> dict[str, Any]:
    """
    Called by viral_engine before every script generation.
    Returns:
      - top_hooks:         list of up to 5 best-retention hook phrases
      - avg_retention_pct: channel average retention % benchmark
      - target_retention:  goal to beat in the next video
      - insight:           1-line guidance string for the prompt
    """
    scores = _read_json(RETENTION_FILE, [])
    if not scores:
        return {"top_hooks": [], "avg_retention_pct": None, "target_retention": 45.0, "insight": ""}

    retention_vals = [s["retention_pct"] for s in scores if s.get("retention_pct", 0) > 0]
    avg_retention  = round(sum(retention_vals) / len(retention_vals), 1) if retention_vals else None
    top_hooks      = [s["hook"] for s in scores[:5] if s.get("hook")]

    if avg_retention is not None:
        target = round(min(avg_retention * 1.15, 85.0), 1)
        if avg_retention >= 50:
            insight = (f"Your best hooks average {avg_retention}% retention. "
                       "Amplify the pattern: open with a sharper contradiction or number-based claim.")
        elif avg_retention >= 35:
            insight = (f"Average retention is {avg_retention}%. "
                       "Focus on tightening the first 3 seconds — cut all slow openers.")
        else:
            insight = (f"Retention is {avg_retention}% — below benchmark. "
                       "Open with the most extreme single sentence in the script, nothing else.")
    else:
        target  = 45.0
        insight = ""

    return {
        "top_hooks":         top_hooks,
        "avg_retention_pct": avg_retention,
        "target_retention":  target,
        "insight":           insight,
    }


def latest_scores(n: int = 20) -> list[dict[str, Any]]:
    """Return the n most recent retention scores for dashboard display."""
    return _read_json(RETENTION_FILE, [])[:n]
