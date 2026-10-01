"""
MiniMax Hailuo AI video generation.

Appends cinematic quality modifiers and negative constraints to every
prompt before hitting the API, eliminating "AI slop" artefacts.

Key rotation: tries MINIMAX_KEY_1, then KEY_2, then KEY_3 in order.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("minimax_engine")

# ─── Quality Modifiers ────────────────────────────────────────────────────────
# Appended automatically to every prompt string.
QUALITY_MODIFIERS = (
    "cinematic lighting, photorealistic, 35mm film photograph, "
    "subtle natural skin textures, detailed fabric weave, realistic hand anatomy, "
    "structurally consistent background geometry, natural depth of field"
)

# Negative constraints expressed as instructional avoidance text
# (MiniMax uses a single text prompt; no separate negative field)
NEGATIVE_CONSTRAINTS = (
    "avoid: deformed fingers, melted hands, airbrushed skin, CGI render, "
    "plastic texture, warped walls, smooth cartoon look"
)

# ─── API Endpoints ────────────────────────────────────────────────────────────
MINIMAX_API_URL    = "https://api.minimaxi.chat/v1/video_generation"
MINIMAX_STATUS_URL = "https://api.minimaxi.chat/v1/query/video_generation"
MINIMAX_FILE_URL   = "https://api.minimaxi.chat/v1/files/retrieve"

_KEY_ENV_VARS = ["MINIMAX_KEY_1", "MINIMAX_KEY_2", "MINIMAX_KEY_3"]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _active_key() -> str | None:
    """Return the first non-empty MiniMax key from the rotation."""
    for env in _KEY_ENV_VARS:
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return None


def build_prompt(user_prompt: str) -> str:
    """
    Inject quality modifiers and negative constraints into the prompt.
    This is the single enforcement point — every API call goes through here.
    """
    return (
        f"{user_prompt.strip()}. "
        f"{QUALITY_MODIFIERS}. "
        f"{NEGATIVE_CONSTRAINTS}."
    )


# ─── Public API ───────────────────────────────────────────────────────────────

def generate_clip(prompt: str, dest: Path, timeout_s: int = 300) -> bool:
    """
    Request one AI video clip from MiniMax Hailuo.

    Args:
        prompt:    User-facing topic / scene description (quality modifiers added automatically).
        dest:      Path where the .mp4 should be saved.
        timeout_s: Max seconds to wait for the generation task to complete.

    Returns:
        True  — file written to `dest` and > 50 KB.
        False — API unavailable, quota error, or timeout (caller should fall
                back to Pexels / Pixabay).
    """
    key = _active_key()
    if not key:
        log.info("No MiniMax API key configured — skipping AI clip generation.")
        return False

    full_prompt = build_prompt(prompt)
    log.info("MiniMax: requesting clip — prompt=%r", full_prompt[:120])

    # 1. Submit generation task
    try:
        resp = requests.post(
            MINIMAX_API_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "video-01", "prompt": full_prompt},
            timeout=30,
        )
        if resp.status_code in (402, 429, 401):
            log.warning("MiniMax key exhausted / quota error (%d) — skipping.", resp.status_code)
            return False
        resp.raise_for_status()
        task_id = resp.json().get("task_id")
        if not task_id:
            log.warning("MiniMax: no task_id in response: %s", resp.text[:300])
            return False
        log.info("MiniMax: task submitted — task_id=%s", task_id)
    except requests.RequestException as err:
        log.warning("MiniMax generation request failed: %s", err)
        return False

    # 2. Poll until done
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status_resp = requests.get(
                MINIMAX_STATUS_URL,
                headers={"Authorization": f"Bearer {key}"},
                params={"task_id": task_id},
                timeout=20,
            )
            status_resp.raise_for_status()
            data = status_resp.json()
            status = data.get("status")

            if status == "Success":
                file_id = data.get("file_id")
                if not file_id:
                    log.warning("MiniMax task succeeded but no file_id: %s", data)
                    return False
                # 3. Download the rendered file
                dl = requests.get(
                    MINIMAX_FILE_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    params={"file_id": file_id},
                    timeout=120,
                    stream=True,
                )
                dl.raise_for_status()
                dest.write_bytes(dl.content)
                size = dest.stat().st_size
                log.info("MiniMax clip saved: %s (%d KB)", dest.name, size // 1024)
                return size > 50_000

            if status in ("Fail", "Unknown"):
                log.warning("MiniMax task %s failed: %s", task_id, data)
                return False

            log.debug("MiniMax task %s status=%s — polling…", task_id, status)
            time.sleep(10)

        except requests.RequestException as poll_err:
            log.warning("MiniMax poll error: %s", poll_err)
            time.sleep(10)

    log.warning("MiniMax task %s timed out after %ds.", task_id, timeout_s)
    return False
