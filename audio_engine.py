"""
Audio Engine — four-tier TTS fallback ladder.

Tier 1 : ElevenLabs  (Sage Mentor / Brian — highest quality)
Tier 2 : Deepgram    (Aura model — near-real-time, reliable)
Tier 3 : Fish Audio  (streaming TTS — final paid fallback)
Tier 4 : gTTS        (always-available free safety net)

Each tier is attempted in sequence. Failures are caught and logged
silently — the render thread is never interrupted.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import requests
from gtts import gTTS

log = logging.getLogger("audio_engine")


# ── shared ──────────────────────────────────────────────────────────────────

def _clean(script: str) -> str:
    """Strip [pause] cue markers — they are timing hints, not spoken words."""
    return re.sub(r"\[pause[:\s\d.]*\]", " ", script, flags=re.IGNORECASE).strip()


# ── Tier 1: ElevenLabs ──────────────────────────────────────────────────────

ELEVENLABS_VOICE_ID = "nPczCjzI2devNBz1zQrb"   # Brian — Sage Mentor


def _elevenlabs(script: str, dest: Path, api_key: str) -> None:
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": _clean(script),
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.65,
                "similarity_boost": 0.75,
                "style": 0.30,
                "use_speaker_boost": True,
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError(f"ElevenLabs returned suspiciously small payload ({len(resp.content)} bytes)")
    dest.write_bytes(resp.content)


# ── Tier 2: Deepgram Aura ───────────────────────────────────────────────────

DEEPGRAM_MODEL = "aura-asteria-en"


def _deepgram(script: str, dest: Path, api_key: str) -> None:
    resp = requests.post(
        f"https://api.deepgram.com/v1/speak?model={DEEPGRAM_MODEL}",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        },
        json={"text": _clean(script)},
        timeout=120,
    )
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError(f"Deepgram returned suspiciously small payload ({len(resp.content)} bytes)")
    dest.write_bytes(resp.content)


# ── Tier 3: Fish Audio ──────────────────────────────────────────────────────

FISH_AUDIO_REFERENCE_ID = "7f92f8efb8ec43bf81429cc1c9199cb1"   # default Male US narrator


def _fish_audio(script: str, dest: Path, api_key: str) -> None:
    resp = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "text": _clean(script),
            "reference_id": FISH_AUDIO_REFERENCE_ID,
            "format": "mp3",
            "mp3_bitrate": 128,
        },
        timeout=120,
        stream=True,
    )
    resp.raise_for_status()
    written = 0
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                fh.write(chunk)
                written += len(chunk)
    if written < 1000:
        raise RuntimeError(f"Fish Audio streamed suspiciously small response ({written} bytes)")


# ── Tier 4: gTTS (always available) ─────────────────────────────────────────

def _gtts(script: str, dest: Path) -> None:
    gTTS(text=_clean(script), lang="en", slow=False).save(str(dest))


# ── Public interface ─────────────────────────────────────────────────────────

def make_voiceover(script: str, work_dir: Path) -> Path:
    """
    Run the four-tier TTS fallback ladder and return the path to the saved MP3.
    Errors from each tier are logged quietly and never crash the render thread.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / "voiceover.mp3"

    tiers: list[tuple[str, str | None, callable]] = [
        ("ElevenLabs", os.environ.get("ELEVENLABS_API_KEY"), _elevenlabs),
        ("Deepgram",   os.environ.get("DEEPGRAM_API_KEY"),   _deepgram),
        ("Fish Audio", os.environ.get("FISH_AUDIO_API_KEY"), _fish_audio),
    ]

    for name, key, fn in tiers:
        if not key:
            log.debug("Voiceover tier %s skipped — API key not set.", name)
            continue
        try:
            fn(script, dest, key)
            log.info("Voiceover: %s succeeded.", name)
            return dest
        except Exception as err:
            log.warning("Voiceover tier %s failed (%s) — trying next tier.", name, err)

    # Tier 4 — always available, no key required
    try:
        _gtts(script, dest)
        log.info("Voiceover: gTTS safety-net fallback used.")
        return dest
    except Exception as err:
        raise RuntimeError(f"All four TTS tiers failed. Last error: {err}") from err


def active_tier() -> str:
    """Return the name of the first tier that has a valid key set."""
    if os.environ.get("ELEVENLABS_API_KEY"):
        return "ElevenLabs"
    if os.environ.get("DEEPGRAM_API_KEY"):
        return "Deepgram"
    if os.environ.get("FISH_AUDIO_API_KEY"):
        return "Fish Audio"
    return "gTTS"
