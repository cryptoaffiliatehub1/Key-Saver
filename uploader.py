"""
Autonomous Upload Queue — 100% server-side, zero browser dependency.

How it works:
  1. Any part of the engine calls  uploader.enqueue(...)  to add a video.
  2. A persistent background worker thread reads data/upload_queue.json
     every POLL_INTERVAL seconds and processes pending items in order.
  3. The queue survives server restarts — items are written to disk before
     the worker picks them up, so nothing is lost if the process crashes.
  4. Failed uploads are retried up to MAX_RETRIES times with exponential
     back-off.  After MAX_RETRIES the item is moved to the dead-letter
     section of the queue file and a system alert is recorded.
  5. Everything runs inside a daemon thread started once at import time
     (called explicitly via  start()  from main.py).  It is completely
     independent of Flask, browser sessions, or active HTTP connections.

Queue file schema (data/upload_queue.json):
  {
    "pending":   [ <item>, ... ],
    "completed": [ <item>, ... ],
    "failed":    [ <item>, ... ]
  }

Item schema:
  {
    "id":          str,          # job id
    "video_path":  str,          # absolute path to the .mp4 file
    "title":       str,
    "description": str,
    "tags":        [str, ...],
    "enqueued_at": float,        # unix timestamp
    "attempts":    int,
    "last_error":  str | null,
    "next_retry":  float         # unix timestamp — worker skips until this passes
  }
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import dashboard
import youtube_auth

log = logging.getLogger("uploader")

QUEUE_FILE   = Path("data/upload_queue.json")
POLL_INTERVAL = 30          # seconds between queue scans
MAX_RETRIES   = 4
RETRY_BACKOFF = [60, 300, 900, 3600]   # wait after attempt 1/2/3/4

_lock    = threading.Lock()
_started = False


# ── Queue persistence ────────────────────────────────────────────────────────

def _read_queue() -> dict[str, list]:
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    if not QUEUE_FILE.exists() or QUEUE_FILE.stat().st_size == 0:
        return {"pending": [], "completed": [], "failed": []}
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:
        return {"pending": [], "completed": [], "failed": []}


def _write_queue(q: dict[str, list]) -> None:
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=2))
    tmp.replace(QUEUE_FILE)


# ── Public API ───────────────────────────────────────────────────────────────

def enqueue(
    video_path: str | Path,
    title: str,
    description: str,
    tags: list[str],
    job_id: str | None = None,
) -> str:
    """
    Add a video to the persistent upload queue.
    Returns the queue item id.  Safe to call from any thread.
    """
    item_id = job_id or uuid.uuid4().hex[:8]
    item: dict[str, Any] = {
        "id":          item_id,
        "video_path":  str(video_path),
        "title":       title,
        "description": description,
        "tags":        list(tags),
        "enqueued_at": time.time(),
        "attempts":    0,
        "last_error":  None,
        "next_retry":  0.0,
    }
    with _lock:
        q = _read_queue()
        q["pending"].append(item)
        _write_queue(q)
    log.info("Uploader: enqueued job %s — %r", item_id, title[:60])
    return item_id


def queue_depth() -> int:
    """Return number of pending upload items."""
    return len(_read_queue().get("pending", []))


def recent_uploads(n: int = 10) -> list[dict]:
    """Return the n most-recently completed uploads."""
    return _read_queue().get("completed", [])[-n:]


# ── Worker ───────────────────────────────────────────────────────────────────

def _process_item(item: dict[str, Any]) -> bool:
    """
    Attempt to upload one item.  Returns True on success, False on failure.
    Errors are logged quietly — never raised to the caller.
    """
    video_path = Path(item["video_path"])
    if not video_path.exists():
        log.error("Uploader: video file missing for job %s: %s", item["id"], video_path)
        item["last_error"] = f"File not found: {video_path}"
        return False

    if not youtube_auth.has_token():
        log.warning("Uploader: YouTube not connected — skipping job %s", item["id"])
        item["last_error"] = "YouTube not connected"
        return False

    try:
        from googleapiclient.http import MediaFileUpload
        service = youtube_auth.get_youtube_service()
        full_tags = list(item["tags"]) + ["WealthVault", "WealthVaultEntry"]
        body = {
            "snippet": {
                "title":           item["title"][:100],
                "description":     item["description"][:4900],
                "tags":            full_tags[:30],
                "categoryId":      "27",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus":             "public",
                "selfDeclaredMadeForKids":   False,
                "madeForKids":               False,
            },
        }
        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
        req   = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                log.debug("Uploader job %s: %.0f%%", item["id"], status.progress() * 100)
        video_id = response.get("id", "")
        log.info("Uploader: job %s uploaded → https://youtu.be/%s", item["id"], video_id)
        item["video_id"]    = video_id
        item["uploaded_at"] = time.time()
        try:
            dashboard.record_upload(
                video_id=video_id,
                title=item["title"],
                description=item["description"],
                seed=item.get("seed", ""),
                hook=item.get("hook", ""),
            )
        except Exception as e:
            log.warning("Uploader: dashboard record failed: %s", e)
        try:
            import affiliate_comments
            affiliate_comments.post_affiliate_comment(video_id, title=item["title"])
        except Exception as e:
            log.warning("Uploader: affiliate comment failed: %s", e)
        return True

    except Exception as err:
        # Silent catch — log and signal retry
        if youtube_auth.is_token_error(err):
            log.warning("Uploader: token error on job %s — attempting refresh. %s", item["id"], err)
            refreshed = youtube_auth.try_refresh_credentials()
            if not refreshed:
                item["last_error"] = f"Token invalid/revoked: {err}"
                dashboard.record_system_alert(
                    "token_error",
                    f"YouTube token expired for upload job {item['id']} — re-authorize at /youtube/auth.",
                    details=str(err),
                )
                return False
        item["last_error"] = str(err)
        log.warning("Uploader: job %s failed (attempt %d): %s", item["id"], item["attempts"], err)
        return False


def _worker_loop() -> None:
    log.info("Uploader worker started — polling every %ds.", POLL_INTERVAL)
    while True:
        try:
            with _lock:
                q = _read_queue()
                now = time.time()
                still_pending: list[dict] = []
                for item in q["pending"]:
                    if item.get("next_retry", 0) > now:
                        still_pending.append(item)
                        continue
                    item["attempts"] += 1
                    success = _process_item(item)
                    if success:
                        q["completed"].append(item)
                        q["completed"] = q["completed"][-200:]
                    elif item["attempts"] >= MAX_RETRIES:
                        log.error("Uploader: job %s dead-lettered after %d attempts.", item["id"], item["attempts"])
                        dashboard.record_system_alert(
                            "upload_failed",
                            f"Upload job {item['id']} failed after {MAX_RETRIES} attempts: {item.get('last_error','')}",
                        )
                        q["failed"].append(item)
                        q["failed"] = q["failed"][-100:]
                    else:
                        backoff = RETRY_BACKOFF[min(item["attempts"] - 1, len(RETRY_BACKOFF) - 1)]
                        item["next_retry"] = now + backoff
                        log.info("Uploader: job %s retry in %ds.", item["id"], backoff)
                        still_pending.append(item)
                q["pending"] = still_pending
                _write_queue(q)
        except Exception as err:
            log.error("Uploader worker loop error (non-fatal): %s", err)
        time.sleep(POLL_INTERVAL)


# ── Startup ──────────────────────────────────────────────────────────────────

def start() -> None:
    """
    Start the autonomous background upload worker.
    Safe to call multiple times — only one worker thread is ever started.
    Called once from main.py at server boot.  Completely independent of
    browser sessions, active HTTP connections, or client-side state.
    """
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_worker_loop, daemon=True, name="uploader-worker")
    t.start()
    log.info("Uploader: autonomous background worker running (pid-independent daemon thread).")
