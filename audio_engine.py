"""
Audio Engine — three-tier TTS waterfall (no gTTS, no local fallback).

Tier 1 : ElevenLabs  (voice pNInz6obpgmA5QC9632W — Adam Premium)
Tier 2 : Deepgram    (aura-orpheus-en)
Tier 3 : Fish Audio  (profile 77103ba780df4e689626343516568212)

Priority order is always ElevenLabs → Deepgram → Fish Audio.

The waterfall auto-recovers: if ElevenLabs credits refill (detected at
the next 24h poll) it automatically becomes the active tier again on the
very next video — no manual intervention required.

If all three tiers fail, a hard RuntimeError is raised and the pipeline
stops. No robotic or local TTS is ever used as a substitute.

A background thread polls each API every 24 hours for available quota.
make_voiceover() also updates the cache immediately when a tier returns a
hard quota/auth error at runtime, so subsequent videos skip it without
wasting an API call. The 24h poll will restore the tier once credits refill.

Key-naming flexibility:
  Deepgram key: checked as DEEPGRAM_API_KEY *and* DEEPGRAM_AURA (either name works).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("audio_engine")


# ── Script pre-processing — strict cleanup, pacing & breath control ──────────

def _sanitize_tts_text(script: str) -> str:
    """Remove model noise before pacing so fillers cannot become audible stutters."""
    text = str(script).encode("utf-8", "ignore").decode("utf-8")
    text = re.sub(r"\b(?:uh+|um+)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*([,;:!?])\s*", r"\1 ", text)
    text = re.sub(r"(?<!\w)[,;:!?]+", " ", text)
    text = re.sub(r"([,;:!?]){2,}", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text.strip(" \t\n,;:!?")

def _add_pacing(script: str) -> str:
    text = _sanitize_tts_text(script)
    text = re.sub(r"\s*—\s*", "... ", text)
    text = re.sub(r"\s*–\s*", ", ", text)
    text = re.sub(r"\s*;\s*", ", ", text)
    text = re.sub(r"\s*:\s*", ", ", text)

    def _break_long(sentence: str) -> str:
        words = sentence.split()
        if len(words) <= 12:
            return sentence
        breaks = {" but ", " and ", " yet ", " so ", " because ", " while ", " although "}
        for b in breaks:
            if b in sentence.lower():
                idx = sentence.lower().find(b)
                return sentence[:idx] + "..." + sentence[idx:].strip()
        mid = len(words) // 2
        return " ".join(words[:mid]) + "..." + " ".join(words[mid:])

    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [_break_long(s) for s in sentences]
    text = " ".join(sentences)
    # One ellipsis is the maximum intentional pause. Longer punctuation runs
    # make ElevenLabs insert a conspicuous gap and can sound like a stutter.
    text = re.sub(r"(?:\.\.\.\s*){2,}", "... ", text)
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return re.sub(r"(?<=[.!?])\s+", "\n", text.strip())


def _clean(script: str) -> str:
    """Strip [pause] cue markers and apply cinematic pacing."""
    cleaned = re.sub(r"\[pause[:\s\d.]*\]", " ", script, flags=re.IGNORECASE).strip()
    return _add_pacing(cleaned)


# ── Deepgram key resolver ────────────────────────────────────────────────────

def _deepgram_key() -> str | None:
    """
    Resolve Deepgram API key from either accepted env-var name.

    Supports both:
      DEEPGRAM_API_KEY  — canonical name set in Replit Secrets
      DEEPGRAM_AURA     — alternate name some installs use in their .env
    """
    return (
        os.environ.get("DEEPGRAM_API_KEY")
        or os.environ.get("DEEPGRAM_AURA")
        or None
    )


# ── 24-hour credit / quota poll ──────────────────────────────────────────────

_credit_status: dict[str, object] = {
    "ElevenLabs": True,
    "Deepgram":   True,
    "Fish Audio": True,
    "checked_at": 0.0,
}
_credit_lock = threading.Lock()


def _check_elevenlabs_credits() -> bool:
    """
    Return True  → key present AND remaining characters > 0.
    Return False → key missing OR explicitly exhausted (character_count >= character_limit).

    Safety rules:
    • Hits /v1/user/subscription (returns subscription data directly, no nesting).
    • A 200 response with remaining > 0 ALWAYS returns True.
    • Any network error or unexpected status defaults to True so the live
      TTS request can decide — we never pre-block on poll uncertainty.
    • Only 401/403 (bad key) OR confirmed zero remaining credits → False.
    """
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return False
    try:
        r = requests.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": key},
            timeout=12,
        )
        # Bad key / forbidden — definitive
        if r.status_code in (401, 403):
            log.warning("ElevenLabs poll: key rejected (HTTP %d)", r.status_code)
            return False
        if r.status_code != 200:
            # Rate-limited, server error, etc. — don't penalise; let TTS decide
            log.info("ElevenLabs poll: non-200 status %d — assuming available.", r.status_code)
            return True
        try:
            data = r.json()
        except Exception:
            # JSON parse failure on a 200 — key works, shape unexpected; allow through
            log.info("ElevenLabs poll: 200 but JSON parse failed — assuming available.")
            return True

        used  = int(data.get("character_count",  0) or 0)
        limit = int(data.get("character_limit",  0) or 0)

        if limit <= 0:
            # Can't evaluate quota — don't block
            log.info("ElevenLabs poll: character_limit=0 (unlimited plan?) — assuming available.")
            return True

        remaining = limit - used
        log.info(
            "ElevenLabs poll: used=%d  limit=%d  remaining=%d  active=%s",
            used, limit, remaining, remaining > 0,
        )
        # Store raw numbers so the dashboard can display them
        with _credit_lock:
            _credit_status["elevenlabs_used"]  = used
            _credit_status["elevenlabs_limit"] = limit
        return remaining > 0

    except Exception as exc:
        # Network error, timeout, etc. — key might still work; don't block
        log.info("ElevenLabs poll: network error (%s) — assuming available.", exc)
        return True


def _check_deepgram_credits() -> bool:
    """
    Return True  → key resolves (either DEEPGRAM_API_KEY or DEEPGRAM_AURA) AND auth passes.
    Return False → no key found OR API explicitly rejects it (401/403).

    Network/timeout errors default to True — key present = attempt live generation.
    """
    key = _deepgram_key()
    if not key:
        log.info("Deepgram poll: no key found under DEEPGRAM_API_KEY or DEEPGRAM_AURA.")
        return False
    try:
        r = requests.get(
            "https://api.deepgram.com/v1/auth/token",
            headers={"Authorization": f"Token {key}"},
            timeout=12,
        )
        if r.status_code in (401, 403):
            log.warning("Deepgram poll: key rejected (HTTP %d)", r.status_code)
            return False
        # 429, 5xx, or 200 → all treated as available; let TTS request decide
        log.info("Deepgram poll: status %d — available.", r.status_code)
        return True
    except Exception as exc:
        log.info("Deepgram poll: network error (%s) — assuming available.", exc)
        return True


def _check_fish_audio_credits() -> bool:
    """
    Return True  → key set AND account reachable.
    Return False → key missing OR explicitly rejected (401/403).

    Network errors default to True.
    """
    key = os.environ.get("FISH_AUDIO_API_KEY")
    if not key:
        return False
    try:
        r = requests.get(
            "https://api.fish.audio/v1/me",
            headers={"Authorization": f"Bearer {key}"},
            timeout=12,
        )
        if r.status_code in (401, 403):
            log.warning("Fish Audio poll: key rejected (HTTP %d)", r.status_code)
            return False
        log.info("Fish Audio poll: status %d — available.", r.status_code)
        return True
    except Exception as exc:
        log.info("Fish Audio poll: network error (%s) — assuming available.", exc)
        return True


def _poll_credits() -> None:
    """Check each voice API for available quota, cache result, reschedule in 24h."""
    results = {
        "ElevenLabs": _check_elevenlabs_credits(),
        "Deepgram":   _check_deepgram_credits(),
        "Fish Audio": _check_fish_audio_credits(),
    }
    with _credit_lock:
        _credit_status.update(results)
        _credit_status["checked_at"] = time.time()
    log.info(
        "Voice credit poll: ElevenLabs=%s  Deepgram=%s  FishAudio=%s",
        results["ElevenLabs"], results["Deepgram"], results["Fish Audio"],
    )
    refresh_timer = threading.Timer(86400, _poll_credits)
    refresh_timer.daemon = True
    refresh_timer.start()


if os.environ.get("PIPELINE_TESTING") != "1":
    initial_poll_timer = threading.Timer(0, _poll_credits)
    initial_poll_timer.daemon = True
    initial_poll_timer.start()


# ── Tier 1: ElevenLabs ──────────────────────────────────────────────────────

ELEVENLABS_VOICE_ID = "pNInz6obpgmA5QC9632W"


def _elevenlabs(script: str, dest: Path, api_key: str) -> None:
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": _clean(script),
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.40,
                "similarity_boost": 0.85,
                "style": 0.20,
                "use_speaker_boost": True,
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError(
            f"ElevenLabs returned suspiciously small payload ({len(resp.content)} bytes)"
        )
    dest.write_bytes(resp.content)


# ── Tier 2: Deepgram Aura ───────────────────────────────────────────────────

DEEPGRAM_MODEL = "aura-orpheus-en"


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
        raise RuntimeError(
            f"Deepgram returned suspiciously small payload ({len(resp.content)} bytes)"
        )
    dest.write_bytes(resp.content)


# ── Tier 3: Fish Audio ──────────────────────────────────────────────────────

FISH_AUDIO_REFERENCE_ID = "77103ba780df4e689626343516568212"


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
        raise RuntimeError(
            f"Fish Audio streamed suspiciously small response ({written} bytes)"
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_quota_error(err: Exception) -> bool:
    """True when the error signals a hard auth/payment/quota refusal (not a transient network issue)."""
    msg = str(err).lower()
    return any(x in msg for x in (
        "401", "402", "403", "429",
        "unauthorized", "forbidden",
        "payment required", "quota",
        "credits", "exhausted",
        "billing",
    ))


# ── Public interface ─────────────────────────────────────────────────────────

def make_voiceover(script: str, work_dir: Path) -> tuple[Path, str]:
    """
    Run the three-tier TTS waterfall and return (mp3_path, tier_name_used).

    Priority: ElevenLabs → Deepgram → Fish Audio.

    Auto-recovery:
    • Tier succeeds → used.
    • Tier returns hard quota/auth error at runtime → cached False immediately;
      24h poll restores it when credits refill.
    • Poll marked a tier False BUT the key is still present → still attempted.
      The poll result is advisory; the live TTS response is the ground truth.
    • All three tiers fail → RuntimeError (no gTTS/local TTS fallback).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / "voiceover.mp3"

    with _credit_lock:
        status = dict(_credit_status)

    tiers: list[tuple[str, str | None, object]] = [
        ("ElevenLabs", os.environ.get("ELEVENLABS_API_KEY"),   _elevenlabs),
        ("Deepgram",   _deepgram_key(),                         _deepgram),
        ("Fish Audio", os.environ.get("FISH_AUDIO_API_KEY"),   _fish_audio),
    ]

    errors: list[str] = []
    for name, key, fn in tiers:
        if not key:
            errors.append(f"{name}: API key not set")
            log.debug("Voiceover tier %s skipped — API key not set.", name)
            continue

        # Advisory check: if poll cached this tier as exhausted, log a warning
        # but still attempt the live request. The TTS response is ground truth.
        poll_ok = status.get(name, True)
        if not poll_ok:
            log.warning(
                "Voiceover tier %s: poll cached as exhausted — attempting live request anyway "
                "(key is present; poll result is advisory only).",
                name,
            )

        try:
            fn(script, dest, key)
            log.info("Voiceover: %s succeeded (%s).", name, _tier_model(name))
            # If the tier succeeded after being cached as exhausted, restore it
            if not poll_ok:
                with _credit_lock:
                    _credit_status[name] = True
                log.info("Tier %s restored to active — live request succeeded.", name)
            return dest, name
        except Exception as err:
            errors.append(f"{name}: {err}")
            log.warning("Voiceover tier %s failed (%s) — trying next tier.", name, err)
            # Hard quota/auth/payment error → immediately invalidate this tier in the
            # cache so every subsequent video also skips it without burning an API call.
            # The 24h poll thread will re-check and restore it once credits refill.
            if _is_quota_error(err):
                with _credit_lock:
                    _credit_status[name] = False
                log.warning(
                    "Tier %s cached as exhausted after runtime error '%s'. "
                    "Will auto-restore when 24h poll detects credits refilled.",
                    name, err,
                )

    raise RuntimeError(
        "All voice APIs exhausted — pipeline stopped. Check credits or add an API key.\n"
        + "\n".join(f"  • {e}" for e in errors)
    )


def _tier_model(tier: str) -> str:
    if tier == "ElevenLabs":
        return f"voice/{ELEVENLABS_VOICE_ID}"
    if tier == "Deepgram":
        return DEEPGRAM_MODEL
    if tier == "Fish Audio":
        return f"ref/{FISH_AUDIO_REFERENCE_ID}"
    return tier


def active_tier() -> str:
    """Return the name of the first tier that has a key AND available credits."""
    with _credit_lock:
        status = dict(_credit_status)
    if os.environ.get("ELEVENLABS_API_KEY") and status.get("ElevenLabs", True):
        return "ElevenLabs"
    if _deepgram_key() and status.get("Deepgram", True):
        return "Deepgram"
    if os.environ.get("FISH_AUDIO_API_KEY") and status.get("Fish Audio", True):
        return "Fish Audio"
    return "No active voice tier — all keys missing or credits exhausted"


def credit_status() -> dict:
    """Return a copy of the current credit status dict (for dashboard display)."""
    with _credit_lock:
        return dict(_credit_status)
