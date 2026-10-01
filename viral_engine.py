"""
Viral Engine for YouTube Shorts — Dark Psychology & Behavioral Persuasion niche.

Pipeline:
1. Gemini (or OpenRouter fallback on 429) writes a hook+loop script.
2. ElevenLabs → Deepgram → Fish Audio waterfall narrates it (no gTTS fallback).
3. Pexels + Pixabay supply ≥20 unique HD clips (no repeats, quality-gated).
4. MoviePy stitches clips + word-by-word captions + ghost watermark.
5. SEO Oracle generates 5 title variants and auto-picks the best.
6. After 5-minute RAM cooldown, the Short is uploaded with episodic metadata.
7. Cleanup of all raw clips and temp files.
"""
from __future__ import annotations

import json
import logging
import os
import os as _os
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import github_backup
import google.generativeai as genai
import requests
from googleapiclient.http import MediaFileUpload

import ab_tester
import affiliate_comments
import asset_tracker
import audio_engine
import dashboard
import minimax_engine
import openrouter_fallback
import retention_engine
import seo_oracle
import topic_memory
import trend_hunter
import uploader
import youtube_auth
from youtube_auth import get_youtube_service

log = logging.getLogger("viral_engine")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

PREFERRED_GEMINI_MODELS = ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]

# ── Local font resolution ─────────────────────────────────────────────────────
# Rendering is hardcoded to this project-local path. A clean system TTF is
# copied here once; network download is only a last resort during rendering.
CAPTION_FONT_DIR = Path("assets/fonts")
CAPTION_FONT_DIR.mkdir(parents=True, exist_ok=True)
CAPTION_FONT = str(CAPTION_FONT_DIR / "DejaVuSans.ttf")
CAPTION_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

def _ensure_caption_font() -> str:
    if Path(CAPTION_FONT).exists():
        return CAPTION_FONT
    for source in CAPTION_FONT_CANDIDATES:
        if _os.path.exists(source):
            shutil.copy2(source, CAPTION_FONT)
            log.info("Copied clean caption font to %s", CAPTION_FONT)
            return CAPTION_FONT
    try:
        response = requests.get(
            "https://github.com/dejavu-fonts/dejavu/raw/master/ DejaVuSans.ttf".replace(" ", ""),
            timeout=15,
        )
        response.raise_for_status()
        Path(CAPTION_FONT).write_bytes(response.content)
        log.info("Downloaded clean caption font to %s", CAPTION_FONT)
        return CAPTION_FONT
    except Exception as err:
        raise RuntimeError(f"Unable to provision local caption font: {err}") from err


_ensure_caption_font()


# ── Pillow-direct caption renderer ───────────────────────────────────────────
# Bypasses ImageMagick (the source of y→v / p→b / L→. glyph corruption).
# Pillow reads the TTF file directly — zero font-name lookup, zero IM involvement.
#
# Guarantees:
#   • Every glyph from the TTF renders exactly as stored in the file.
#   • Contractions (it's, don't, you're) render with native apostrophes.
#   • No forced uppercase — sentence case is preserved.
    #   • Subtle black drop shadow for readability over bright/dark backgrounds.
#   • Transparent background (RGBA) — text floats over video with no box.

def _make_caption_clip(
    text: str,
    font_path: str,
    font_size: int,
    safe_width: int,
    duration: float,
) -> "ImageClip":
    """
    Render a single caption line as a transparent RGBA ImageClip using Pillow.

    Parameters
    ----------
    text        : The display string for this caption frame.
    font_path   : Absolute path to the .ttf file.
    font_size   : Point size.
    safe_width  : Maximum pixel width of the rendered text band (80% of frame width).
    duration    : Clip duration in seconds.

    Returns a MoviePy ImageClip with alpha channel (transparent background).
    The clip can be positioned with .with_position().
    """
    from PIL import Image, ImageDraw, ImageFont
    import numpy as _np
    from moviepy import ImageClip as _IC

    try:
        pil_font = ImageFont.truetype(font_path, font_size)
    except Exception:
        pil_font = ImageFont.load_default()

    # ── Measure rendered text size ────────────────────────────────────────────
    probe = Image.new("RGBA", (1, 1))
    draw  = ImageDraw.Draw(probe)
    bbox  = draw.multiline_textbbox((0, 0), text, font=pil_font, spacing=8)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    stroke = 1          # 1 px black outline
    pad    = stroke + 4 # breathing room around text
    img_w  = text_w + pad * 2
    img_h  = text_h + pad * 2 + stroke * 2

    img  = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = pad
    y = pad

    # Draw a restrained shadow, then clean white typography on top.
    draw.multiline_text(
        (x + 2, y + 3), text, font=pil_font, fill=(0, 0, 0, 150),
        spacing=8,
    )
    draw.multiline_text(
        (x, y), text, font=pil_font, fill=(255, 255, 255, 255),
        spacing=8, stroke_width=0,
    )

    arr  = _np.array(img)          # shape: (img_h, img_w, 4)
    clip = _IC(arr).with_duration(duration)
    return clip


def sanitize_spoken_script(text: str) -> str:
    """Remove model-only section labels and tags before TTS and captions."""
    spoken = str(text).encode("utf-8", "ignore").decode("utf-8")
    spoken = spoken.replace("—", ", ").replace("–", ", ")
    spoken = re.sub(r"\[[^\]]+\]", " ", spoken)
    spoken = re.sub(r"\b(?:uh+|um+)\b", " ", spoken, flags=re.IGNORECASE)
    spoken = re.sub(
        r"(?im)(?<!\w)\s*(?:hook|body|twist(?:\s*/\s*nuance)?|nuance|cta)"
        r"\s*:\s*",
        " ",
        spoken,
    )
    spoken = re.sub(r"(?im)^\s*(?:section|beat)\s*\d*\s*:\s*", "", spoken)
    spoken = re.sub(r"\*+", "", spoken)
    spoken = re.sub(r"(?<!\w)[,;:!?]+", " ", spoken)
    spoken = re.sub(r"([,;:!?]){2,}", r"\1", spoken)
    spoken = re.sub(r"[ \t]+", " ", spoken)
    spoken = re.sub(r"[ \t]*\n[ \t]*", "\n", spoken)
    return re.sub(r"\n{2,}", "\n", spoken).strip()


def _caption_lines(text: str, font_path: str, font_size: int, safe_width: int) -> tuple[list[str], int]:
    """Wrap clauses by measured pixels, never by slicing a word."""
    from PIL import Image, ImageDraw, ImageFont

    probe = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(probe)
    size = font_size
    words = text.split()
    if not words:
        return [], size
    while size >= 18:
        font = ImageFont.truetype(font_path, size)
        widest = max(
            draw.textbbox((0, 0), word, font=font)[2]
            for word in words
        )
        if widest <= safe_width:
            break
        size -= 2
    font = ImageFont.truetype(font_path, size)
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > safe_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines, size
CLIP_SWAP_SECONDS = 3.0
TARGET_W, TARGET_H = 1080, 1920
UPLOAD_DELAY_SECONDS = 5 * 60
SERIES_TAG = "DarkMindFiles"
WATERMARK_TEXT = "Dark Psychology Files"
WATERMARK_OPACITY = 0.10

HOOK_ARCHETYPES = [
    "The request that quietly changes the power balance...",
    "Why agreeable people get maneuvered...",
    "The hidden frame inside ordinary conversations...",
    "The pressure cue most people mistake for confidence...",
    "The pause that reveals who needs the deal more...",
    "The social trap that makes you defend their position...",
    "The silent tactic used to move your boundary...",
]

SCRIPT_MIN_WORDS = 70
SCRIPT_MAX_WORDS = 90
SCRIPT_TARGET_WORDS = "70-90"
MIN_DURATION = 25
MAX_DURATION = 40

CONCEPT_MATRIX_PROMPT = """
You are the concept strategist for a YouTube Shorts channel about dark psychology,
behavioral persuasion, social manipulation, and behavioral traps.

Target Audience: Adults who want to recognize influence attempts and protect their
agency in negotiations, work, relationships, and online interactions. Focus on
observable behaviors, ethical self-protection, and concrete countermeasures.

Create exactly 5 unique, highly specific concepts for the seed below. Keep each
concept sharp, stoic, and atmospheric. Focus on strategic silence, deception
detection, perception control, and behavioral leverage. Avoid academic jargon,
study citations, motivational fluff, recycled internet tropes, and unsupported
claims. Each concept must fit a deliberate 25-35 second narration.

Rotate through these actionable sub-topics and give every concept exactly one
subtopic. This run must prioritize: {required_subtopic}
Available sub-topics: Dark Psychology in Negotiations, Cognitive Biases & Mind
Games, Asymmetric Influence Techniques, Perception Manipulation & Value Framing,
Spotting Deceptive Leverage.

Do not generate concepts, topics, or angles that overlap with any entry in this
list: {past_topics_json}

Seed: {seed}

Return ONLY a valid JSON array with exactly five objects using these keys:
[
  {{
    "topic": "specific topic",
    "angle": "specific counter-intuitive angle",
    "mechanism": "concrete mechanism, metric, or case study",
    "audience": "who benefits and why"
  }}
]
""".strip()


VIRAL_PROMPT = """
You are a YouTube Shorts strategist for sharp, atmospheric dark psychology and
human behavior insights.
Write a deliberate 25-35 second narrator script that obeys EVERY rule:

1. Focus: strategic silence, deception detection, perception control, and
   behavioral leverage. Teach recognition and ethical self-protection. Never
   teach coercion, abuse, fraud, or exploitation.
2. STRUCTURE AND TIMING:
   - HOOK (0-3s): One striking statement about human behavior or a hidden
      social rule.
   - BODY (3-25s): Short, punchy sentences. Use 3-6 words per line. Use zero
      academic jargon, study citations, motivational fluff, or generic advice.
      Show the observable cue and the self-protective response.
   - CLOSING (25-35s): End with a stoic realization or dark psychology
      execution rule, followed by a natural three-second follow CTA.
3. Open with one of these hook archetypes only when it can support the
   concrete hook rule:
{hook_archetypes}

4. Use a low, stoic, atmospheric tone. Short sentences. Plain language.
5. Build escalating tension across the hook, body, and closing.
6. DURATION REQUIREMENT: exactly {target_words} spoken words. No emojis,
   section headers, labels, or stage directions in the returned dialogue.
7. Do not make unrealistic audience claims or present speculation as fact.

Winning hook patterns from past top performers:
{winning_hooks}

Retention-optimised hooks (highest watch-time % on this channel — prioritise these patterns):
{retention_hooks}

Retention intelligence: {retention_insight}
Target: beat {target_retention}% average view duration on this video.

Trending keywords to weave in naturally:
{trending_kw}

Selected concept:
{selected_concept_json}

Do not generate concepts, topics, or angles that overlap with any entry in this
list: {past_topics_json}

Return ONLY valid JSON with these exact keys:
{{
  "script": "...",
  "first_line": "...",
  "last_line": "...",
  "keywords": ["...", "...", "...", "...", "...", "...", "...", "..."],
  "description": "...",
  "tags": ["..."]
}}
""".strip()


CRITIQUE_PROMPT = """
You are an adversarial editorial and reality-check gate for a sharp, stoic,
atmospheric dark psychology YouTube Short.

Focus on strategic silence, deception detection, perception control, behavioral
leverage, and ethical self-protection. Use plain language.

Identify lazy tropes, academic jargon, unsubstantiated claims, false certainty,
and logical inconsistencies in the draft below. Rewrite the draft in 70-90 plain
spoken words using the three-part structure: striking hook, punchy 3-6 word body
lines, and a stoic closing rule followed by a three-second follow CTA. Do not
add section labels or structural cues.
Do not invent citations or present speculation as fact.

Draft:
{draft}

Return ONLY valid JSON:
{{
  "script": "the fully rewritten script",
  "first_line": "the opening line",
  "last_line": "the final line",
  "keywords": ["at least six useful keywords"]
}}
""".strip()


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:48] or "viral-short"


def _extract_json(text: str) -> dict | list:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


# ---------- 1. Script generation ----------

def _gemini_generate(prompt: str, temperature: float = 0.85) -> dict | list:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    genai.configure(api_key=api_key)
    last_error: str = "unknown"
    for model_name in PREFERRED_GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(
                model_name,
                generation_config={
                    "temperature": temperature,
                    "top_p": 0.92,
                    "max_output_tokens": 1400,
                    "response_mime_type": "application/json",
                },
            )
            response = model.generate_content(prompt)
            return _extract_json(response.text)
        except Exception as error:
            last_error = str(error)
            if openrouter_fallback.is_rate_limit_error(error):
                raise
            continue
    raise RuntimeError(f"Gemini failed: {last_error}")


def _generate_concept_matrix(
    seed: str, past_topics_json: str, required_subtopic: str
) -> list[dict[str, Any]]:
    base_prompt = CONCEPT_MATRIX_PROMPT.format(
        seed=_luxury_prompt_guard(seed),
        past_topics_json=past_topics_json,
        required_subtopic=required_subtopic,
    )

    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            prompt += (
                "\n\nSTRICT RETRY: The prior response was unusable. Return a JSON "
                "array containing exactly five objects now. Do not wrap it in "
                "markdown or explanatory text."
            )

        def _primary() -> list | dict:
            return _gemini_generate(prompt, temperature=0.88)

        data = openrouter_fallback.call_with_fallback(
            prompt, _primary, temperature=0.88, max_tokens=1200
        )
        raw_concepts: Any = data
        if isinstance(data, dict):
            raw_concepts = data.get("concepts")
            if not isinstance(raw_concepts, list):
                for value in data.values():
                    if isinstance(value, list):
                        raw_concepts = value
                        break
        concepts: list[dict[str, Any]] = []
        if isinstance(raw_concepts, list):
            for item in raw_concepts:
                if isinstance(item, dict) and (
                    str(item.get("topic") or item.get("concept") or "").strip()
                ):
                    concepts.append(item)
        if len(concepts) >= 5:
            return concepts[:5]
        log.warning(
            "Concept matrix attempt %d returned %d valid concepts; retrying.",
            attempt + 1, len(concepts),
        )
    raise RuntimeError(
        "Concept matrix returned fewer than five valid concepts after retry."
    )


def _critique_script(draft: str) -> dict[str, Any]:
    prompt = CRITIQUE_PROMPT.format(draft=draft)

    def _primary() -> dict | list:
        return _gemini_generate(prompt, temperature=0.85)

    data = openrouter_fallback.call_with_fallback(
        prompt, _primary, temperature=0.85, max_tokens=2000
    )
    if not isinstance(data, dict):
        raise RuntimeError("Reality gate returned an invalid JSON response.")
    revised = str(data.get("script") or "").strip()
    keywords = data.get("keywords") or []
    if not revised:
        raise RuntimeError("Reality gate returned an empty rewritten script.")
    if not isinstance(keywords, list) or len(keywords) < 6:
        raise RuntimeError("Reality gate returned insufficient keywords.")
    return {
        "script": revised,
        "first_line": str(data.get("first_line") or revised.split(".")[0]).strip(),
        "last_line": str(data.get("last_line") or revised.split(".")[-2]).strip(),
        "keywords": [str(item).strip() for item in keywords[:8] if str(item).strip()],
    }


def generate_viral_script(seed: str) -> dict[str, Any]:
    history = topic_memory.recent_entries(50)
    used_topics = topic_memory.recent_used_topics(
        50, niche=topic_memory.CURRENT_NICHE
    )
    prompt_history = history + [
        {"source": "used_topics.json", **entry} for entry in used_topics
    ]
    past_topics_json = json.dumps(
        prompt_history[-50:], ensure_ascii=False, separators=(",", ":")
    )
    required_subtopic = topic_memory.next_subtopic()
    concepts = _generate_concept_matrix(
        seed, past_topics_json, required_subtopic
    )
    selected_concept, novelty = topic_memory.select_most_novel(
        concepts, history + used_topics
    )
    log.info(
        "Selected concept novelty=%.4f topic=%r",
        novelty,
        selected_concept.get("topic") or selected_concept.get("concept"),
    )
    selected_concept["subtopic"] = required_subtopic
    selected_concept_json = json.dumps(
        selected_concept, ensure_ascii=False, separators=(",", ":")
    )
    topic_memory.append_used_topic(seed, selected_concept)

    winning  = ab_tester.get_winning_hooks(5)
    trending = _dark_psychology_trends(
        trend_hunter.get_trending_seed_enrichment()
    )
    hook_list = "\n".join(f"   - {h}" for h in HOOK_ARCHETYPES)
    winning_str = "\n".join(f"   - {h}" for h in winning) if winning else "   (none yet)"

    # Retention intelligence injection — pulls from 48-hour analytics loop
    try:
        enrichment = retention_engine.get_prompt_enrichment()
    except Exception:
        enrichment = {}
    ret_hooks   = enrichment.get("top_hooks", [])
    ret_insight = enrichment.get("insight", "")
    ret_target  = enrichment.get("target_retention", 45.0)
    retention_hooks_str = (
        "\n".join(f"   - {h}" for h in ret_hooks) if ret_hooks else "   (no data yet — first few videos will build this)"
    )

    prompt = VIRAL_PROMPT.format(
        hook_archetypes=hook_list,
        winning_hooks=winning_str,
        retention_hooks=retention_hooks_str,
        retention_insight=ret_insight or "Keep the first sentence under 10 words and end it on an unresolved tension.",
        target_retention=ret_target,
        trending_kw=trending or "   (not available yet)",
        target_words=SCRIPT_TARGET_WORDS,
        selected_concept_json=selected_concept_json,
        past_topics_json=past_topics_json,
    )

    def _primary() -> dict:
        return _gemini_generate(prompt)

    data: dict | None = None
    script = ""
    for attempt in range(3):
        if attempt == 0:
            data = openrouter_fallback.call_with_fallback(prompt, _primary, temperature=0.85, max_tokens=2000)
        else:
            expand_prompt = prompt + (
                f"\n\nWARNING: Your previous script was too short ({len(script.split())} words). "
                f"You MUST write at least {SCRIPT_MIN_WORDS} words. Expand every beat significantly. "
                "Add more concrete examples, deeper psychological insight, and additional [pause] beats."
            )
            def _expand_primary() -> dict:
                return _gemini_generate(expand_prompt)
            data = openrouter_fallback.call_with_fallback(expand_prompt, _expand_primary, temperature=0.82, max_tokens=2000)

        script = sanitize_spoken_script(str(data.get("script", "")).strip())
        word_count = len(script.split())
        if SCRIPT_MIN_WORDS <= word_count <= SCRIPT_MAX_WORDS:
            log.info("Script generated: %d words (attempt %d)", word_count, attempt + 1)
            break
        if word_count < SCRIPT_MIN_WORDS:
            log.warning(
                "Script too short (%d words < %d min), regenerating (attempt %d/3)...",
                word_count, SCRIPT_MIN_WORDS, attempt + 1,
            )
        else:
            log.warning(
                "Script too long (%d words > %d max), regenerating (attempt %d/3)...",
                word_count, SCRIPT_MAX_WORDS, attempt + 1,
            )

    keywords = data.get("keywords", []) or []
    if not script or len(keywords) < 6:
        raise RuntimeError("Gemini returned an incomplete script.")

    first_line = str(data.get("first_line") or script.split(".")[0]).strip()
    last_line = str(data.get("last_line") or script.split(".")[-2]).strip()

    # Reality gate runs before voiceover, stock downloads, or rendering.
    critique_draft = script
    critique: dict[str, Any] | None = None
    for critique_attempt in range(2):
        critique = _critique_script(critique_draft)
        revised_script = sanitize_spoken_script(critique["script"])
        revised_word_count = len(revised_script.split())
        if SCRIPT_MIN_WORDS <= revised_word_count <= SCRIPT_MAX_WORDS:
            break
        if critique_attempt == 0:
            if revised_word_count > SCRIPT_MAX_WORDS:
                instruction = (
                    "Compress this rewrite to exactly "
                    f"{SCRIPT_TARGET_WORDS} spoken words. Remove repetition, not "
                    "the mechanics, trade-off, nuance, or CTA."
                )
            else:
                instruction = (
                    "Expand this rewrite to exactly "
                    f"{SCRIPT_TARGET_WORDS} spoken words. Add concrete mechanics, "
                    "a realistic trade-off, nuance, and a natural CTA."
                )
            critique_draft = revised_script + "\n\nEDITORIAL CONSTRAINT: " + instruction
            log.warning(
                "Reality gate returned %d words; requesting a bounded rewrite.",
                revised_word_count,
            )
        else:
            raise RuntimeError(
                f"Reality gate returned {revised_word_count} words; expected "
                f"{SCRIPT_MIN_WORDS}-{SCRIPT_MAX_WORDS}."
            )
    if critique is None:
        raise RuntimeError("Reality gate did not return a script.")
    script = sanitize_spoken_script(critique["script"])
    first_line = sanitize_spoken_script(critique["first_line"])
    last_line = sanitize_spoken_script(critique["last_line"])
    keywords = critique["keywords"]
    if not script or not SCRIPT_MIN_WORDS <= len(script.split()) <= SCRIPT_MAX_WORDS:
        raise RuntimeError("Script sanitization produced an out-of-range dialogue.")

    # SEO Oracle: generate 5 title variants and auto-pick best
    title_variants = seo_oracle.generate_title_variants(
        seed,
        first_line,
        script,
        used_titles=topic_memory.recent_used_titles(),
    )
    best_title, scored_titles = seo_oracle.pick_best_title(title_variants)

    # SEO Oracle: LLM-generated description (2-3 sentences, dark-psych keywords)
    # + 3-4 niche hashtags — replaces the raw Gemini description field
    seo_description, seo_hashtags = seo_oracle.generate_seo_metadata(script, seed)
    hashtag_str = " ".join(seo_hashtags)
    full_description = f"{seo_description}\n\n{hashtag_str}"
    clean_best_title = seo_oracle.clean_title(best_title)
    topic_memory.record_entry(seed, selected_concept, clean_best_title)

    return {
        "script": script,
        "first_line": first_line,
        "last_line": last_line,
        "keywords": [str(k).strip() for k in keywords[:8]],
        "title": clean_best_title,
        "title_variants": scored_titles,
        "description": full_description,
        "tags": [t.lstrip("#") for t in seo_hashtags] + [
            "psychology", "shorts", "mindset", "darkpsychology",
            "behavioralpsychology", "persuasion", "socialinfluence", "mindgames",
        ],
    }


def _luxury_prompt_guard(seed: str) -> str:
    prompt = seed.strip()
    if re.search(r"\b(sexy|casual)\b", prompt, re.IGNORECASE):
        return re.sub(
            r"\b(sexy|casual)\b",
            "cinematic dark psychology",
            prompt,
            flags=re.IGNORECASE,
        )
    return prompt


def _dark_psychology_trends(raw: str) -> str:
    """Keep external trend enrichment from pulling prompts back into old niches."""
    blocked = re.compile(
        r"\b(?:crypto|token|tokenomics|wealth|money|billionaire|rich|finance)\w*\b",
        re.IGNORECASE,
    )
    candidates = re.split(r"[,|\n]+", str(raw or ""))
    selected = [
        " ".join(candidate.split())
        for candidate in candidates
        if candidate.strip() and not blocked.search(candidate)
    ]
    return ", ".join(selected[:8]) or (
        "behavioral persuasion, social influence, cognitive bias"
    )


# ---------- 2. Voiceover (delegated to audio_engine) ----------
# Four-tier fallback: ElevenLabs → Deepgram → Fish Audio → gTTS


# ---------- 3. Pexels download ----------

def _mood_media_queries(keywords: list[str]) -> list[str]:
    """Turn narrative keywords into consistently dark, psychology-led searches."""
    queries: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        clean = re.sub(r"[^a-z0-9\s-]", " ", str(keyword).lower())
        clean = " ".join(clean.split())
        if not clean:
            continue
        query = f"cinematic dark psychology behavioral influence {clean}"
        if query not in seen:
            queries.append(query)
            seen.add(query)
    return queries

def _pexels_headers() -> dict[str, str]:
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        raise RuntimeError("PEXELS_API_KEY is not set.")
    return {"Authorization": api_key}


# ── Quality / deduplication constants ───────────────────────────────────────

MIN_CLIP_HEIGHT   = 720       # minimum pixel height to pass quality gate
MIN_CLIP_DURATION = 3.0       # minimum clip duration in seconds
MIN_CLIP_FILESIZE = 200_000   # minimum file size in bytes after download
MIN_CLIPS_REQUIRED = 20       # hard minimum unique clips per video build


def _video_passes_quality(video: dict) -> bool:
    """Pre-download quality gate on Pexels API metadata (avoids downloading junk)."""
    dur = video.get("duration") or 0
    if dur < MIN_CLIP_DURATION:
        return False
    for f in video.get("video_files", []):
        h = f.get("height") or 0
        w = f.get("width") or 0
        if h >= MIN_CLIP_HEIGHT or w >= MIN_CLIP_HEIGHT:
            return True
    return False


def _pixabay_passes_quality(hit: dict) -> bool:
    """Pre-download quality gate on Pixabay API metadata."""
    dur = hit.get("duration") or 0
    if dur < MIN_CLIP_DURATION:
        return False
    videos = hit.get("videos", {})
    for size_key in ("large", "medium"):
        v = videos.get(size_key) or {}
        h = v.get("height") or 0
        w = v.get("width") or 0
        if h >= MIN_CLIP_HEIGHT or w >= MIN_CLIP_HEIGHT:
            return True
    return False


def _pick_video_file(video: dict) -> str | None:
    """Pick the best-quality portrait file from a Pexels video entry (prefers HD)."""
    files = video.get("video_files", []) or []
    # Prefer HD portrait (height >= 720 and portrait orientation)
    hd_portrait = [
        f for f in files
        if (f.get("height") or 0) >= MIN_CLIP_HEIGHT
        and (f.get("height") or 0) >= (f.get("width") or 0)
    ]
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    hd_any = [f for f in files if (f.get("height") or 0) >= MIN_CLIP_HEIGHT or (f.get("width") or 0) >= MIN_CLIP_HEIGHT]
    pool = hd_portrait or portrait or hd_any or files
    pool.sort(key=lambda f: abs((f.get("height") or 0) - 1920))
    for f in pool:
        link = f.get("link")
        if link:
            return link
    return None


def _download_clip(file_url: str, dest: Path) -> bool:
    """Download a single video clip to dest. Returns True on success with adequate size."""
    try:
        with requests.get(file_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        return dest.stat().st_size >= MIN_CLIP_FILESIZE
    except Exception as err:
        log.warning("Clip download failed for %s: %s", file_url, err)
        return False


def download_pexels_clips(
    keywords: list[str],
    work_dir: Path,
    target: int = 25,
    seen_ids: set | None = None,
) -> list[Path]:
    """
    Download unique HD portrait clips from Pexels.

    - Deduplicates by Pexels video ID (shared via seen_ids set).
    - Searches up to 3 pages per keyword at 8 results/page.
    - Every clip must pass the quality gate (≥720p, ≥3 s duration, ≥200 KB).
    """
    if seen_ids is None:
        seen_ids = set()
    headers = _pexels_headers()
    saved: list[Path] = []
    queries = _mood_media_queries(keywords)
    random.shuffle(queries)
    for keyword in queries:
        if len(saved) >= target:
            break
        for page in range(1, 4):
            if len(saved) >= target:
                break
            try:
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers=headers,
                    params={
                        "query": keyword,
                        "orientation": "portrait",
                        "per_page": 8,
                        "page": page,
                    },
                    timeout=25,
                )
                r.raise_for_status()
                videos = r.json().get("videos", []) or []
                if not videos:
                    break
                for video in videos:
                    if len(saved) >= target:
                        break
                    vid_id = video.get("id")
                    if vid_id in seen_ids:
                        continue
                    # Persistent cross-session dedup — reject IDs used in any previous build
                    if asset_tracker.is_used("pexels", vid_id):
                        log.debug("Pexels %s already used in a previous build — skipping.", vid_id)
                        continue
                    if not _video_passes_quality(video):
                        log.debug("Pexels %s failed quality gate (dur=%s)", vid_id, video.get("duration"))
                        continue
                    file_url = _pick_video_file(video)
                    if not file_url:
                        continue
                    dest = work_dir / f"clip_{len(saved):02d}_{_slug(keyword)}.mp4"
                    if _download_clip(file_url, dest):
                        saved.append(dest)
                        seen_ids.add(vid_id)
                        asset_tracker.mark_used("pexels", vid_id)
                        log.info("Pexels: saved %s  id=%s  %d bytes", dest.name, vid_id, dest.stat().st_size)
                    else:
                        dest.unlink(missing_ok=True)
            except requests.RequestException as err:
                log.warning("Pexels error for %r page %d: %s", keyword, page, err)
                break
    return saved


def _download_pixabay_clips(
    keywords: list[str],
    work_dir: Path,
    target: int = 25,
    existing: int = 0,
    seen_ids: set | None = None,
) -> list[Path]:
    """
    Fetch unique HD vertical clips from Pixabay.

    - Deduplicates by Pixabay video ID (shared via seen_ids set).
    - Searches up to 3 pages per keyword at 8 results/page.
    - Every clip must pass the quality gate (≥720p, ≥3 s duration, ≥200 KB).
    """
    if seen_ids is None:
        seen_ids = set()
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        log.warning("PIXABAY_API_KEY not set — cannot use Pixabay fallback.")
        return []
    saved: list[Path] = []
    queries = _mood_media_queries(keywords)
    random.shuffle(queries)
    for keyword in queries:
        if len(saved) >= target:
            break
        for page in range(1, 4):
            if len(saved) >= target:
                break
            try:
                r = requests.get(
                    "https://pixabay.com/api/videos/",
                    params={
                        "key": api_key,
                        "q": keyword,
                        "orientation": "vertical",
                        "per_page": 8,
                        "page": page,
                        "safesearch": "true",
                    },
                    timeout=25,
                )
                r.raise_for_status()
                hits = r.json().get("hits", []) or []
                if not hits:
                    break
                for hit in hits:
                    if len(saved) >= target:
                        break
                    hit_id = hit.get("id")
                    if hit_id in seen_ids:
                        continue
                    # Persistent cross-session dedup — reject IDs used in any previous build
                    if asset_tracker.is_used("pixabay", hit_id):
                        log.debug("Pixabay %s already used in a previous build — skipping.", hit_id)
                        continue
                    if not _pixabay_passes_quality(hit):
                        log.debug("Pixabay %s failed quality gate (dur=%s)", hit_id, hit.get("duration"))
                        continue
                    videos = hit.get("videos", {})
                    file_url = (
                        (videos.get("large") or {}).get("url")
                        or (videos.get("medium") or {}).get("url")
                        or (videos.get("small") or {}).get("url")
                    )
                    if not file_url:
                        continue
                    idx = existing + len(saved)
                    dest = work_dir / f"clip_{idx:02d}_pbay_{_slug(keyword)}.mp4"
                    if _download_clip(file_url, dest):
                        saved.append(dest)
                        seen_ids.add(hit_id)
                        asset_tracker.mark_used("pixabay", hit_id)
                        log.info("Pixabay: saved %s  id=%s  %d bytes", dest.name, hit_id, dest.stat().st_size)
                    else:
                        dest.unlink(missing_ok=True)
            except requests.RequestException as err:
                log.warning("Pixabay error for %r page %d: %s", keyword, page, err)
                break
    return saved


def download_clips_with_fallback(keywords: list[str], work_dir: Path, target: int = 25) -> list[Path]:
    """
    Download B-roll clips: Pexels first, then Pixabay for any shortfall.

    Guarantees:
    • All clips are unique — no video ID used twice (shared seen_ids set).
    • All clips are HD quality (≥720p, ≥3 s, ≥200 KB).
    • Hard minimum of MIN_CLIPS_REQUIRED (20) unique clips or RuntimeError.
    """
    seen_ids: set = set()

    pexels_clips: list[Path] = []
    pexels_ok = bool(os.environ.get("PEXELS_API_KEY"))
    if pexels_ok:
        log.info("Fetching up to %d unique HD clips from Pexels…", target)
        pexels_clips = download_pexels_clips(keywords, work_dir, target=target, seen_ids=seen_ids)
        log.info("Pexels returned %d quality clips.", len(pexels_clips))

    shortfall = target - len(pexels_clips)
    all_clips = list(pexels_clips)

    if shortfall > 0:
        source = "Pixabay (primary)" if not pexels_ok else f"Pixabay (filling {shortfall} clips)"
        log.info("Trying %s…", source)
        pixabay_clips = _download_pixabay_clips(
            keywords, work_dir, target=shortfall, existing=len(pexels_clips), seen_ids=seen_ids
        )
        all_clips.extend(pixabay_clips)
        log.info("Pixabay returned %d clips. Total unique: %d", len(pixabay_clips), len(all_clips))

    if len(all_clips) < MIN_CLIPS_REQUIRED:
        raise RuntimeError(
            f"Only {len(all_clips)} unique HD clips downloaded (need ≥{MIN_CLIPS_REQUIRED}). "
            "Check PEXELS_API_KEY / PIXABAY_API_KEY quota, or broaden keywords."
        )
    log.info("B-roll ready: %d unique HD clips (no repeats guaranteed).", len(all_clips))
    return all_clips


# ---------- 4. Video assembly ----------

def _resize_to_portrait(clip):
    from moviepy.video.fx import Crop, Resize
    w, h = clip.size
    target_aspect = TARGET_W / TARGET_H
    src_aspect = w / h
    if src_aspect > target_aspect:
        new_w = int(h * target_aspect)
        x1 = (w - new_w) // 2
        clip = clip.with_effects([Crop(x1=x1, y1=0, x2=x1 + new_w, y2=h)])
    else:
        new_h = int(w / target_aspect)
        y1 = (h - new_h) // 2
        clip = clip.with_effects([Crop(x1=0, y1=y1, x2=w, y2=y1 + new_h)])
    return clip.with_effects([Resize(new_size=(TARGET_W, TARGET_H))])


def _watermark_clip(duration: float):
    """Semi-transparent ghost brand watermark at bottom-right, 10% opacity."""
    from moviepy import TextClip
    try:
        txt = TextClip(
            text=WATERMARK_TEXT,
            font=CAPTION_FONT,
            font_size=36,
            color="white",
            method="label",
        )
        txt = (
            txt.with_duration(duration)
            .with_opacity(WATERMARK_OPACITY)
            .with_position((TARGET_W - txt.size[0] - 28, TARGET_H - txt.size[1] - 36))
        )
        return txt
    except Exception as err:
        log.warning("Watermark failed: %s", err)
        return None


def build_short(
    script: str,
    voiceover_path: Path,
    clip_paths: list[Path],
    work_dir: Path,
) -> Path:
    from moviepy import AudioFileClip, CompositeVideoClip, TextClip, VideoFileClip, concatenate_videoclips

    audio = AudioFileClip(str(voiceover_path))
    # Safety trim: MoviePy reads audio in small lookahead windows (~0.04 s).
    # Trim a small buffer off the end so the reader never overshoots.
    # Always use subclipped (not with_duration) — subclipped clamps time values;
    # with_duration adds a hard IOError guard that crashes on any overshoot.
    _raw_dur = audio.duration
    _safe_dur = max(_raw_dur - 0.15, _raw_dur * 0.992)
    audio = audio.subclipped(0, _safe_dur)

    # Build B-roll to cover MAX(audio length, MIN_DURATION) so the composite is
    # always at least as long as the minimum — no post-hoc video padding needed.
    total = max(audio.duration + 0.5, float(MIN_DURATION) + 1.0, float(CLIP_SWAP_SECONDS) * 3)

    # ── B-ROLL: play each clip once, no cycling — freeze last frame if exhausted
    from moviepy import ColorClip, ImageClip
    video_clips = []
    elapsed = 0.0
    idx = 0
    last_sub = None
    while elapsed < total:
        remaining = total - elapsed
        if idx < len(clip_paths):
            src_path = clip_paths[idx]
            idx += 1
            try:
                src = VideoFileClip(str(src_path), audio=False)
            except Exception as err:
                log.warning("Skipping bad clip %s: %s", src_path.name, err)
                continue
            take = min(CLIP_SWAP_SECONDS, max(0.5, src.duration - 0.1))
            start = random.uniform(0, max(0.0, src.duration - take - 0.05))
            sub = src.subclipped(start, start + take)
            sub = _resize_to_portrait(sub).without_audio()
            video_clips.append(sub)
            last_sub = sub
            elapsed += take
        else:
            # B-roll exhausted — freeze on last frame (no looping)
            try:
                if last_sub is not None:
                    freeze_frame = last_sub.get_frame(last_sub.duration - 0.02)
                    filler = ImageClip(freeze_frame).with_duration(remaining)
                else:
                    filler = ColorClip(size=(TARGET_W, TARGET_H), color=(0, 0, 0)).with_duration(remaining)
            except Exception:
                filler = ColorClip(size=(TARGET_W, TARGET_H), color=(0, 0, 0)).with_duration(remaining)
            video_clips.append(filler)
            elapsed = total
            break

    base = concatenate_videoclips(video_clips, method="chain").subclipped(0, total)

    def _clean_script_word(w: str) -> str:
        """Normalize typography without substituting one character for another."""
        return (
            w.replace("\u2019", "'")
             .replace("\u2018", "'")
             .replace("\u201c", '"')
             .replace("\u201d", '"')
             .replace("\u2026", "...")
             .replace("\u2014", " ")
             .replace("\u2013", " ")
             .strip(" .,!?;:\"#@$%^&*()[]{}")
        )

    _ensure_caption_font()
    _CAPTION_SAFE_W = int(TARGET_W * 0.80)
    _FONT_SIZE = 62
    cleaned_script = sanitize_spoken_script(script)
    caption_end = max(audio.duration - 0.12, 1.0)
    clauses = [
        clause.strip()
        for clause in re.split(r"(?<=[.!?;,:])\s+", cleaned_script)
        if clause.strip()
    ]
    segments: list[tuple[str, int, int]] = []
    for clause in clauses:
        words = [_clean_script_word(word) for word in clause.split()]
        clean_clause = " ".join(word for word in words if word)
        if not clean_clause:
            continue
        lines, fitted_size = _caption_lines(
            clean_clause, CAPTION_FONT, _FONT_SIZE, _CAPTION_SAFE_W
        )
        if lines:
            segments.append(("\n".join(lines), len(clean_clause.split()), fitted_size))

    caption_clips = []
    if segments:
        total_words = sum(word_count for _, word_count, _ in segments)
        cursor = 0.0
        y_pos    = int(TARGET_H * 0.65)
        for caption_text, word_count, fitted_size in segments:
            ln_start = cursor
            ln_end = min(
                ln_start + (caption_end * word_count / total_words),
                caption_end,
            )
            if ln_start >= audio.duration:
                break
            ln_dur = ln_end - ln_start
            cursor = ln_end
            try:
                cap = _make_caption_clip(
                    text=caption_text,
                    font_path=CAPTION_FONT,
                    font_size=fitted_size,
                    safe_width=_CAPTION_SAFE_W,
                    duration=ln_dur,
                )
                cap = (
                    cap.with_start(ln_start)
                       .with_position(("center", y_pos))
                )
                caption_clips.append(cap)
            except Exception as err:
                log.warning("Caption failed for clause %r: %s", caption_text, err)

    # Ghost watermark
    wm = _watermark_clip(total)
    overlay_clips = caption_clips + ([wm] if wm else [])

    composite = CompositeVideoClip([base, *overlay_clips], size=(TARGET_W, TARGET_H))

    # ── AUDIO / VIDEO SYNC + DURATION CLAMP ──────────────────────────────────
    # Strategy: clamp the output video to the voiceover audio length.
    # This eliminates dead air (silent stock footage playing after narration ends).
    #
    # Safety buffer: trim audio 0.12 s short so MoviePy's chunk reader never
    # overshoots the audio file end (which would raise an IOError mid-render).
    # The video is then clamped to exactly that safe length + 0.15 s grace so
    # the last caption frame doesn't hard-cut on the exact final sample.
    raw_audio_dur = audio.duration
    safe_audio_end = max(raw_audio_dur - 0.12, 1.0)
    audio = audio.subclipped(0, safe_audio_end)

    # Clamp composite to audio end + tiny grace — eliminates the dead-air tail
    video_clamp = min(safe_audio_end + 0.15, composite.duration)
    composite_clamped = composite.subclipped(0, video_clamp)

    log.info(
        "Duration clamp: raw_audio=%.3fs  safe_audio=%.3fs  video_out=%.3fs  "
        "dead_air_eliminated=%.3fs",
        raw_audio_dur, safe_audio_end, video_clamp,
        composite.duration - video_clamp,
    )

    # Hard cap at MAX_DURATION — trim both video and audio identically
    if video_clamp > float(MAX_DURATION):
        log.warning("Clamped video %.1fs still > max %ds; trimming to %ds", video_clamp, MAX_DURATION, MAX_DURATION)
        audio = audio.subclipped(0, min(audio.duration, float(MAX_DURATION) - 0.12))
        composite_clamped = composite_clamped.subclipped(0, MAX_DURATION)
    elif video_clamp < float(MIN_DURATION):
        log.warning(
            "Video %.1fs < min %ds — short voiceover (%.1fs raw); "
            "proceeding at actual length (no silent padding).",
            video_clamp, MIN_DURATION, raw_audio_dur,
        )
    else:
        log.info("Final video duration %.1fs ✓", video_clamp)

    final = composite_clamped.with_audio(audio)

    out_path = work_dir / "short.mp4"
    try:
        final.write_videofile(
            str(out_path),
            fps=60,
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=1,
            bitrate="8M",
            logger=None,
            temp_audiofile=str(work_dir / "temp_audio.m4a"),
            remove_temp=True,
            ffmpeg_params=["-vf", "noise=alls=10:allf=t"],
        )
    except Exception as render_err:
        log.error(
            "write_videofile crashed — path=%s  audio_dur=%.3fs  video_dur=%.3fs  error=%s",
            out_path, audio.duration, final.duration, render_err,
        )
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    for obj in [final, composite, base, audio]:
        try:
            obj.close()
        except Exception:
            pass

    return out_path


# ---------- 5. YouTube upload ----------

_MANDATORY_TAGS = [
    "DarkPsychology", "BehavioralPsychology", "Persuasion", "Mindset", "Shorts",
]


def _do_upload(service, video_path: Path, title: str, description: str, tags: list[str]) -> str:
    """Inner upload call — separated so the token-refresh retry can reuse it."""
    # Merge caller tags with mandatory brand/niche tags — deduplicate, preserve order
    seen: set[str] = set()
    merged: list[str] = []
    for t in list(tags) + _MANDATORY_TAGS + [SERIES_TAG, "DarkMindFilesEntry"]:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            merged.append(t)
    full_tags = merged

    # ── PRE-FLIGHT VALIDATION ──────────────────────────────────────────────────
    # Reject upload if description is empty — prevents metadata-free videos.
    if not title or not title.strip():
        raise ValueError("Upload rejected: title is empty. SEO Oracle must supply a title.")
    if not description or not description.strip():
        raise ValueError(
            "Upload rejected: description is empty. "
            "The pipeline must supply a keyword-rich description before uploading."
        )
    safe_title       = title.strip()[:100]
    safe_description = description.strip()[:4900]

    body = {
        "snippet": {
            "title": safe_title,
            "description": safe_description,
            "tags": full_tags[:30],
            "categoryId": "27",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
        },
        "localizations": {},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            log.info("Upload progress %.0f%%", status.progress() * 100)
    video_id = response.get("id", "")
    log.info("Uploaded YouTube video id=%s title=%r", video_id, title)
    return video_id


def upload_to_youtube(video_path: Path, title: str, description: str, tags: list[str]) -> str:
    """
    Upload with automatic invalid_grant / expired-token self-healing.

    On first token error: attempt silent credential refresh, rebuild the
    service, and retry the upload once. If refresh fails, record a System
    Alert and re-raise so the job is paused without killing the engine.
    """
    try:
        service = get_youtube_service()
        return _do_upload(service, video_path, title, description, tags)
    except Exception as first_err:
        if not openrouter_fallback.is_token_error(first_err):
            raise

        log.warning("YouTube token error during upload — attempting silent refresh. %s", first_err)
        refreshed = youtube_auth.try_refresh_credentials()
        if not refreshed:
            alert_msg = (
                "YouTube token is invalid/revoked and could not be refreshed. "
                "Visit /youtube/auth to reconnect your channel. Upload paused."
            )
            dashboard.record_system_alert("token_error", alert_msg, details=str(first_err))
            raise RuntimeError(alert_msg) from first_err

        try:
            from googleapiclient.discovery import build as _build
            service = _build("youtube", "v3", credentials=refreshed, cache_discovery=False)
            video_id = _do_upload(service, video_path, title, description, tags)
            log.info("Upload succeeded after token refresh.")
            return video_id
        except Exception as retry_err:
            alert_msg = (
                f"YouTube upload failed even after token refresh: {retry_err}. "
                "Upload paused — rest of engine continues."
            )
            dashboard.record_system_alert("token_error", alert_msg, details=str(retry_err))
            raise RuntimeError(alert_msg) from retry_err


# ---------- 6. Post-render quality gate ----------

_MIN_FILE_MB  = 1.0    # file must be at least 1 MB — catches empty/corrupt writes
_MIN_QA_SECS  = 8.0    # video must have at least 8 s of duration (ElevenLabs min)
_MAX_RETRY    = 1      # one automatic retry before raising


def _check_render_quality(video_path: Path) -> tuple[bool, str]:
    """
    Inspect the rendered .mp4 and return (ok, reason).

    Checks (all must pass):
      1. File exists and is ≥ _MIN_FILE_MB in size.
      2. ffprobe detects at least one audio stream.
      3. ffprobe-reported duration ≥ _MIN_QA_SECS.

    Uses ffprobe (bundled with FFmpeg) — safe subprocess call with a 30 s timeout.
    Returns (True, "OK") on pass, (False, "<reason>") on failure.
    """
    import subprocess as _sp
    import json as _json

    # Check 1 — file size
    try:
        size_mb = video_path.stat().st_size / (1024 * 1024)
    except FileNotFoundError:
        return False, f"Output file not found: {video_path}"
    if size_mb < _MIN_FILE_MB:
        return False, f"File too small: {size_mb:.2f} MB < {_MIN_FILE_MB} MB threshold"

    # Checks 2 & 3 — audio stream presence + duration via ffprobe
    try:
        probe = _sp.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return False, f"ffprobe failed (rc={probe.returncode}): {probe.stderr.strip()[:200]}"

        data = _json.loads(probe.stdout)
        streams   = data.get("streams", [])
        fmt       = data.get("format", {})
        duration  = float(fmt.get("duration", 0) or 0)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        if not has_audio:
            return False, "No audio stream detected in output file"
        if duration < _MIN_QA_SECS:
            return False, f"Duration too short: {duration:.2f}s < {_MIN_QA_SECS}s threshold"

        log.info(
            "QA gate passed: size=%.2f MB  duration=%.2fs  audio=True",
            size_mb, duration,
        )
        return True, "OK"

    except _sp.TimeoutExpired:
        return False, "ffprobe timed out after 30 s"
    except Exception as exc:
        return False, f"ffprobe error: {exc}"


# ---------- 7. Cleanup ----------

def cleanup_workdir(work_dir: Path, keep: list[Path] | None = None) -> None:
    keep_set = {p.resolve() for p in (keep or [])}
    if not work_dir.exists():
        return
    for child in work_dir.iterdir():
        try:
            if child.resolve() in keep_set:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except Exception as err:
            log.warning("cleanup error %s: %s", child, err)
    try:
        work_dir.rmdir()
    except OSError:
        pass


# ---------- Job orchestrator ----------

@dataclass
class JobStatus:
    id: str
    seed: str
    state: str = "pending"
    message: str = ""
    title: str | None = None
    title_variants: list | None = None
    video_id: str | None = None
    video_id_b: str | None = None
    final_video: str | None = None
    ab_mode: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_jobs_lock = threading.Lock()
_jobs: dict[str, JobStatus] = {}

# Strict one-at-a-time pipeline guard — prevents duplicate concurrent renders
_pipeline_mutex = threading.Semaphore(1)

# Bounded thread pool — max 1 concurrent pipeline run (semaphore enforces this above OS level)
_pipeline_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="viral")


def list_jobs() -> list[JobStatus]:
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)


def latest_job() -> JobStatus | None:
    jobs = list_jobs()
    return jobs[0] if jobs else None


def _set(job: JobStatus, **kwargs: Any) -> None:
    with _jobs_lock:
        for k, v in kwargs.items():
            setattr(job, k, v)


def _run_single(seed: str, job: JobStatus, do_upload: bool, work_dir: Path) -> tuple[str | None, dict | None]:
    """Runs one full variant. Returns (video_id, plan)."""
    _set(job, state="scripting", message="Writing viral script via SEO Oracle...")
    plan = generate_viral_script(seed)
    plan["script"] = sanitize_spoken_script(plan["script"])
    if not plan["script"]:
        raise RuntimeError("Script sanitization produced empty spoken dialogue.")
    _set(job, title=plan["title"], title_variants=plan.get("title_variants"))

    _set(job, state="voiceover", message="Recording narration (ElevenLabs → Deepgram → Fish Audio)...")
    voice_path, voice_tier = audio_engine.make_voiceover(plan["script"], work_dir)
    log.info("Voiceover selected tier: %s", voice_tier)
    _set(job, message=f"Narration recorded via {voice_tier}.")

    _set(job, state="downloading", message="Downloading 25 clips (Pexels → Pixabay fallback)...")
    clip_paths = download_clips_with_fallback(plan["keywords"], work_dir, target=25)

    _set(job, state="rendering", message=f"Stitching {len(clip_paths)} clips + captions + watermark...")
    video_path = build_short(plan["script"], voice_path, clip_paths, work_dir)

    # ── POST-RENDER QUALITY GATE ───────────────────────────────────────────────
    # Checks: file size ≥ 1 MB · audio stream present · duration ≥ 8 s
    # On failure: one automatic retry (fresh work dir) before raising.
    qa_ok, qa_reason = _check_render_quality(video_path)
    if not qa_ok:
        log.warning("QA gate FAILED on first render: %s — retrying once…", qa_reason)
        _set(job, message=f"QA failed ({qa_reason}) — retrying render…")
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass
        retry_work_dir = Path(tempfile.mkdtemp(prefix="viral_retry_", dir=str(OUTPUT_DIR)))
        try:
            video_path = build_short(plan["script"], voice_path, clip_paths, retry_work_dir)
            qa_ok2, qa_reason2 = _check_render_quality(video_path)
            if not qa_ok2:
                raise RuntimeError(
                    f"QA gate failed after retry: {qa_reason2} "
                    f"(original failure: {qa_reason})"
                )
            log.info("QA gate passed on retry ✓")
            work_dir = retry_work_dir   # point cleanup at retry dir
        except Exception:
            try:
                shutil.rmtree(retry_work_dir, ignore_errors=True)
            except Exception:
                pass
            raise
    else:
        log.info("QA gate passed on first render ✓")

    final_name = f"{int(time.time())}-{_slug(plan['title'])}.mp4"
    final_path = OUTPUT_DIR / final_name
    shutil.move(str(video_path), str(final_path))

    for c in clip_paths:
        try:
            c.unlink()
        except Exception:
            pass

    _set(job, final_video=final_name)

    video_id: str | None = None
    if do_upload:
        _set(job, state="cooldown", message="Cooling down 5 minutes before upload...")
        time.sleep(UPLOAD_DELAY_SECONDS)
        _set(job, state="uploading", message="Queuing upload — autonomous worker will post to YouTube...")
        try:
            uploader.enqueue(
                video_path=final_path,
                title=plan["title"],
                description=plan["description"],
                tags=plan["tags"],
                job_id=job.id,
            )
            log.info("Queued upload for job %s via autonomous uploader.", job.id)
        except Exception as e:
            log.warning("Uploader enqueue failed — falling back to direct upload: %s", e)
            video_id = upload_to_youtube(final_path, plan["title"], plan["description"], plan["tags"])
            try:
                dashboard.record_upload(
                    video_id=video_id,
                    title=plan["title"],
                    description=plan["description"],
                    seed=seed,
                    hook=plan.get("first_line"),
                )
            except Exception as de:
                log.warning("dashboard record failed: %s", de)
            try:
                affiliate_comments.post_affiliate_comment(video_id, title=plan["title"])
            except Exception as ae:
                log.warning("affiliate comment failed: %s", ae)

    return video_id, plan


def run_viral_pipeline(seed: str, do_upload: bool = True, ab_mode: bool = False) -> JobStatus:
    job = JobStatus(id=uuid.uuid4().hex[:8], seed=seed, state="queued", message="Starting...", ab_mode=ab_mode)
    with _jobs_lock:
        _jobs[job.id] = job

    work_dir_a = Path(tempfile.mkdtemp(prefix="viral_a_", dir=str(OUTPUT_DIR)))
    work_dir_b = Path(tempfile.mkdtemp(prefix="viral_b_", dir=str(OUTPUT_DIR))) if ab_mode else None

    try:
        video_id_a, plan_a = _run_single(seed, job, do_upload, work_dir_a)

        video_id_b: str | None = None
        if ab_mode and work_dir_b:
            _set(job, state="scripting", message="Writing Variant B script for A/B test...")
            video_id_b, plan_b = _run_single(f"{seed} (variant B)", job, do_upload, work_dir_b)
            _set(job, video_id_b=video_id_b)
            if video_id_a and video_id_b and plan_a and plan_b:
                ab_tester.register_test(
                    test_id=job.id,
                    seed=seed,
                    video_id_a=video_id_a,
                    title_a=plan_a["title"],
                    hook_a=plan_a.get("first_line", ""),
                    video_id_b=video_id_b,
                    title_b=plan_b["title"],
                    hook_b=plan_b.get("first_line", ""),
                )

        _set(job, video_id=video_id_a, state="cleanup", message="Cleaning up temp files...")
        cleanup_workdir(work_dir_a)
        if work_dir_b:
            cleanup_workdir(work_dir_b)

        _set(
            job,
            state="done",
            message="Uploaded." if do_upload else "Rendered (upload skipped — connect YouTube to enable).",
            finished_at=time.time(),
        )

        # Auto-backup codebase to GitHub after every successful render
        try:
            title_label = plan_a.get("title", "unknown") if plan_a else "unknown"
            backup_msg = f"Auto-backup after render: {title_label}"
            result = github_backup.push(backup_msg)
            if result["ok"]:
                log.info("GitHub backup: %s", result["message"])
            else:
                log.warning("GitHub backup skipped: %s", result["message"])
        except Exception as _gb_err:
            log.warning("GitHub backup non-critical error: %s", _gb_err)
    except Exception as err:
        import traceback as _tb
        tb_str = _tb.format_exc()
        log.exception("Viral pipeline crashed: %s", err)

        # ── Auto-patch: ask OpenRouter to diagnose and suggest a fix ──────────
        patch = ""
        try:
            patch = openrouter_fallback.get_patch_suggestion(tb_str)
        except Exception as patch_err:
            log.warning("Auto-patch suggestion failed: %s", patch_err)

        dashboard.record_system_alert(
            category="crash",
            message=f"Pipeline crashed [{job.id}]: {err}",
            details=tb_str,
            patch_suggestion=patch,
        )

        _set(job, state="error", message=str(err), finished_at=time.time())
        cleanup_workdir(work_dir_a)
        if work_dir_b:
            cleanup_workdir(work_dir_b)

        # ── Auto-retry once if the error is NOT a permanent token/auth issue ──
        if not openrouter_fallback.is_token_error(err):
            log.info("Auto-patch: scheduling one retry for job %s in 10 s...", job.id)
            time.sleep(10)
            retry_job = JobStatus(
                id=f"{job.id}-r",
                seed=seed,
                state="queued",
                message="Auto-retry after crash...",
                ab_mode=ab_mode,
            )
            with _jobs_lock:
                _jobs[retry_job.id] = retry_job
            # Create a fresh work dir — the original was cleaned up above
            retry_work_dir = Path(tempfile.mkdtemp(prefix="viral_retry_", dir=str(OUTPUT_DIR)))
            try:
                _run_single(seed, retry_job, do_upload, retry_work_dir)
                _set(retry_job, state="done",
                     message="Auto-retry succeeded.",
                     finished_at=time.time())
                dashboard.record_system_alert(
                    "info",
                    f"Auto-retry succeeded for job {job.id}.",
                )
            except Exception as retry_err:
                log.error("Auto-retry also failed: %s", retry_err)
                _set(retry_job, state="error", message=str(retry_err), finished_at=time.time())
            finally:
                cleanup_workdir(retry_work_dir)
    return job


def active_job_count() -> int:
    """Return number of pipeline jobs currently running (state not done/error)."""
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j.state not in ("done", "error"))


def is_pipeline_running() -> bool:
    """Return True if any pipeline job is currently active (not done/error).
    Used by the HTTP layer to enforce a strict one-at-a-time concurrency guard."""
    with _jobs_lock:
        return any(j.state not in ("done", "error") for j in _jobs.values())


def run_in_background(seed: str, do_upload: bool = True, ab_mode: bool = False) -> JobStatus:
    job = JobStatus(id=uuid.uuid4().hex[:8], seed=seed, state="queued", message="Job queued.")
    with _jobs_lock:
        _jobs[job.id] = job

    def _worker() -> None:
        # Acquire the strict one-at-a-time mutex (non-blocking — caller already
        # verified no pipeline is running via is_pipeline_running() before calling)
        acquired = _pipeline_mutex.acquire(blocking=False)
        try:
            result = run_viral_pipeline(seed, do_upload=do_upload, ab_mode=ab_mode)
        finally:
            if acquired:
                _pipeline_mutex.release()
        with _jobs_lock:
            _jobs.pop(job.id, None)
            _jobs[result.id] = result

    _pipeline_executor.submit(_worker)
    return job


SEED_POOL = [
    "the pause that quietly changes the power balance",
    "why agreeable people get maneuvered in meetings",
    "the cognitive bias that makes a bad offer feel safe",
    "the social pressure cue hidden inside a compliment",
    "how silence changes leverage in a negotiation",
    "the frame shift that makes an unfair price feel reasonable",
    "why your boundary gets tested right after you state it",
    "the reciprocity trap behind small favors",
    "how deceptive urgency bypasses careful thinking",
    "the mind game that makes you defend someone else's position",
]


def random_seed() -> str:
    return random.choice(SEED_POOL)
