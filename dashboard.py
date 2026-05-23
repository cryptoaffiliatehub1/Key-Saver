"""
Performance dashboard for uploaded YouTube Shorts.

Persists every successful upload in data/uploads.json and queries the
YouTube Data + YouTube Analytics APIs for views, retention, and CTR.

System alerts (invalid_grant, auto-patch events, 429 bridges) are stored
in data/system_alerts.json and surfaced on the Command Center dashboard.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

from youtube_auth import get_youtube_analytics_service, get_youtube_service

log = logging.getLogger("dashboard")

UPLOADS_FILE = Path("data/uploads.json")
ALERTS_FILE = Path("data/system_alerts.json")
UPLOADS_FILE.parent.mkdir(exist_ok=True)
_lock = threading.Lock()
_alert_lock = threading.Lock()


# ─── Uploads ────────────────────────────────────────────────────────────────

def _read() -> list[dict[str, Any]]:
    if not UPLOADS_FILE.exists() or UPLOADS_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(UPLOADS_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _write(records: list[dict[str, Any]]) -> None:
    UPLOADS_FILE.write_text(json.dumps(records, indent=2))


def record_upload(video_id: str, title: str, description: str, seed: str, hook: str | None = None) -> None:
    if not video_id:
        return
    with _lock:
        records = _read()
        records.append({
            "video_id": video_id,
            "title": title,
            "hook": hook or (title.split(":")[0] if title else ""),
            "seed": seed,
            "uploaded_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        _write(records)


def list_uploads() -> list[dict[str, Any]]:
    with _lock:
        return list(reversed(_read()))


# ─── System Alerts ──────────────────────────────────────────────────────────

def _read_alerts() -> list[dict[str, Any]]:
    if not ALERTS_FILE.exists() or ALERTS_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(ALERTS_FILE.read_text())
    except json.JSONDecodeError:
        return []


def record_system_alert(
    category: str,
    message: str,
    details: str = "",
    patch_suggestion: str = "",
) -> None:
    """
    Persist a system alert to data/system_alerts.json.

    Categories: "token_error", "rate_limit", "crash", "auto_patch", "info"
    """
    with _alert_lock:
        alerts = _read_alerts()
        alerts.insert(0, {
            "category": category,
            "message": message,
            "details": details,
            "patch_suggestion": patch_suggestion,
            "ts": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "acknowledged": False,
        })
        # Keep at most 50 alerts
        ALERTS_FILE.write_text(json.dumps(alerts[:50], indent=2))
    log.warning("SYSTEM ALERT [%s]: %s", category, message)


def list_alerts(unacknowledged_only: bool = False) -> list[dict[str, Any]]:
    with _alert_lock:
        alerts = _read_alerts()
    if unacknowledged_only:
        return [a for a in alerts if not a.get("acknowledged")]
    return alerts


def acknowledge_alert(index: int) -> None:
    with _alert_lock:
        alerts = _read_alerts()
        if 0 <= index < len(alerts):
            alerts[index]["acknowledged"] = True
            ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


# ─── Analytics ──────────────────────────────────────────────────────────────

def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fetch_data_stats(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not video_ids:
        return {}
    yt = get_youtube_service()
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        resp = yt.videos().list(part="statistics,snippet,contentDetails", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            out[item["id"]] = {
                "views": _safe_int(stats.get("viewCount")),
                "likes": _safe_int(stats.get("likeCount")),
                "comments": _safe_int(stats.get("commentCount")),
                "title": snippet.get("title", ""),
                "thumbnail": (snippet.get("thumbnails", {}).get("medium") or {}).get("url"),
                "published_at": snippet.get("publishedAt"),
            }
    return out


def _fetch_analytics(video_id: str) -> dict[str, Any]:
    try:
        ya = get_youtube_analytics_service()
        end = dt.date.today()
        start = end - dt.timedelta(days=28)
        resp = ya.reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
            dimensions="video",
            filters=f"video=={video_id}",
        ).execute()
        rows = resp.get("rows") or []
        if not rows:
            return {"retention_pct": None, "avg_view_seconds": None, "watch_minutes": 0}
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        row = dict(zip(headers, rows[0]))
        return {
            "retention_pct": row.get("averageViewPercentage"),
            "avg_view_seconds": row.get("averageViewDuration"),
            "watch_minutes": row.get("estimatedMinutesWatched"),
        }
    except Exception as err:
        log.warning("analytics fetch failed for %s: %s", video_id, err)
        return {"retention_pct": None, "avg_view_seconds": None, "watch_minutes": 0, "error": str(err)}


def _fetch_ctr(video_id: str) -> dict[str, Any]:
    try:
        ya = get_youtube_analytics_service()
        end = dt.date.today()
        start = end - dt.timedelta(days=28)
        resp = ya.reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="cardImpressions,cardClickRate",
            dimensions="video",
            filters=f"video=={video_id}",
        ).execute()
        rows = resp.get("rows") or []
        if not rows:
            return {"impressions": None, "ctr": None}
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        row = dict(zip(headers, rows[0]))
        return {"impressions": row.get("cardImpressions"), "ctr": row.get("cardClickRate")}
    except Exception:
        return {"impressions": None, "ctr": None}


def build_dashboard() -> dict[str, Any]:
    records = list_uploads()
    rows: list[dict[str, Any]] = []
    if not records:
        return {"rows": [], "totals": {"videos": 0, "views": 0, "likes": 0, "avg_retention": None}}

    video_ids = [r["video_id"] for r in records]
    try:
        data_stats = _fetch_data_stats(video_ids)
    except Exception as err:
        log.warning("data api stats failed: %s", err)
        data_stats = {}

    total_views = 0
    total_likes = 0
    total_comments = 0
    total_watch_minutes = 0.0
    retention_values: list[float] = []
    ctr_values: list[float] = []

    for rec in records:
        vid = rec["video_id"]
        stats = data_stats.get(vid, {})
        analytics = _fetch_analytics(vid)
        ctr_data = _fetch_ctr(vid)
        retention = analytics.get("retention_pct")
        if isinstance(retention, (int, float)):
            retention_values.append(float(retention))
        ctr_val = ctr_data.get("ctr")
        if isinstance(ctr_val, (int, float)):
            ctr_values.append(float(ctr_val))
        views = stats.get("views", 0)
        wm = analytics.get("watch_minutes") or 0
        total_views += views
        total_likes += stats.get("likes", 0)
        total_comments += stats.get("comments", 0)
        total_watch_minutes += float(wm)
        rows.append({
            **rec,
            "title": stats.get("title") or rec["title"],
            "thumbnail": stats.get("thumbnail"),
            "url": f"https://youtu.be/{vid}",
            "studio_url": f"https://studio.youtube.com/video/{vid}/edit",
            "published_at": stats.get("published_at"),
            "views": views,
            "likes": stats.get("likes", 0),
            "comments": stats.get("comments", 0),
            "retention_pct": retention,
            "avg_view_seconds": analytics.get("avg_view_seconds"),
            "watch_minutes": wm,
            "impressions": ctr_data.get("impressions"),
            "ctr": ctr_val,
            "analytics_error": analytics.get("error"),
        })

    rows.sort(key=lambda r: r.get("views", 0), reverse=True)
    avg_retention = (sum(retention_values) / len(retention_values)) if retention_values else None
    avg_ctr = (sum(ctr_values) / len(ctr_values)) if ctr_values else None
    max_views = rows[0]["views"] if rows else 1
    for r in rows:
        r["views_pct"] = round((r["views"] / max(max_views, 1)) * 100, 1)

    return {
        "rows": rows,
        "totals": {
            "videos": len(records),
            "views": total_views,
            "likes": total_likes,
            "comments": total_comments,
            "watch_minutes": round(total_watch_minutes),
            "avg_retention": avg_retention,
            "avg_ctr": avg_ctr,
        },
    }
