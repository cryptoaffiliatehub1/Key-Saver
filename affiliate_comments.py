"""
Affiliate Pinned Comment System.

After every successful upload, automatically posts a high-CTR affiliate
comment on the Short. The comment ID is stored so the UI can deep-link
straight to YouTube Studio for one-click pinning.

YouTube's public API does not expose a "pin comment" endpoint, so the
comment is posted via the API and the user is given a direct Studio link
to pin it in one click (takes ~3 seconds).

Settings are loaded from data/settings.json (editable from the UI) and
fall back to the AFFILIATE_URL environment variable.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from youtube_auth import get_youtube_service

log = logging.getLogger("affiliate_comments")

SETTINGS_FILE = Path("data/settings.json")
COMMENTS_LOG = Path("data/affiliate_comments.json")
SETTINGS_FILE.parent.mkdir(exist_ok=True)

# ── Comment templates ─────────────────────────────────────────────────────────
# Cycle through templates to avoid repetition across videos.
COMMENT_TEMPLATES = [
    """{cta}

The tool I personally use to compound wealth passively 👇
{url}

Drop a comment if you want the full breakdown.""",

    """If this hit different, the free resource below changed everything for me.
{url}

Most people scroll past this. The ones who don't — win. 🏆
{cta}""",

    """WEALTH VAULT RESOURCE 🔐
{url}

This is exactly what the 1% use. {cta}
Reply "VAULT" and I'll send the deep-dive.""",

    """The strategy in this video is just the surface.
The full playbook (free access right now) 👇
{url}

{cta} — act before this gets taken down.""",

    """👑 For the ones who are serious:
{url}

{cta} — this is how you accelerate everything shown above.""",
]

DEFAULT_CTA = "Crypto Affiliate Hub — Start compounding today."


def _load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists() and SETTINGS_FILE.stat().st_size > 0:
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_settings(data: dict[str, Any]) -> None:
    current = _load_settings()
    current.update(data)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))


def get_affiliate_url() -> str:
    settings = _load_settings()
    return (
        settings.get("affiliate_url")
        or __import__("os").environ.get("AFFILIATE_URL")
        or ""
    )


def get_affiliate_cta() -> str:
    settings = _load_settings()
    return settings.get("affiliate_cta") or DEFAULT_CTA


def _pick_template(video_index: int) -> str:
    return COMMENT_TEMPLATES[video_index % len(COMMENT_TEMPLATES)]


def _build_comment_text(url: str, cta: str, video_index: int = 0) -> str:
    template = _pick_template(video_index)
    return template.format(url=url, cta=cta)


def _read_log() -> list[dict[str, Any]]:
    if not COMMENTS_LOG.exists() or COMMENTS_LOG.stat().st_size == 0:
        return []
    try:
        return json.loads(COMMENTS_LOG.read_text())
    except json.JSONDecodeError:
        return []


def _append_log(entry: dict[str, Any]) -> None:
    records = _read_log()
    records.append(entry)
    COMMENTS_LOG.write_text(json.dumps(records[-200:], indent=2))


def post_affiliate_comment(video_id: str, title: str = "") -> dict[str, Any]:
    """
    Posts the affiliate comment on the video. Returns a result dict with
    comment_id, studio_pin_url, and status.
    """
    url = get_affiliate_url()
    if not url:
        log.warning("No affiliate URL configured — skipping comment for %s", video_id)
        return {"status": "skipped", "reason": "no affiliate URL set", "video_id": video_id}

    cta = get_affiliate_cta()
    video_index = len(_read_log())
    comment_text = _build_comment_text(url, cta, video_index)

    try:
        yt = get_youtube_service()
        body = {
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {"textOriginal": comment_text}
                }
            }
        }
        resp = yt.commentThreads().insert(part="snippet", body=body).execute()
        comment_id = resp.get("id", "")
        thread_id = resp.get("id", "")

        # YouTube Studio deep-link for one-click pinning
        studio_pin_url = f"https://studio.youtube.com/video/{video_id}/comments"

        result: dict[str, Any] = {
            "status": "posted",
            "video_id": video_id,
            "comment_id": comment_id,
            "thread_id": thread_id,
            "comment_text": comment_text,
            "studio_pin_url": studio_pin_url,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _append_log(result)
        log.info("Affiliate comment posted on %s (comment_id=%s)", video_id, comment_id)
        return result

    except Exception as err:
        log.error("Failed to post affiliate comment on %s: %s", video_id, err)
        result = {
            "status": "error",
            "video_id": video_id,
            "error": str(err),
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _append_log(result)
        return result


def list_comments() -> list[dict[str, Any]]:
    return list(reversed(_read_log()))
