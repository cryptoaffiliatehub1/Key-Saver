"""
SEO Oracle — generates 5 title variants plus an LLM-optimized description
and 3-4 niche hashtags. Auto-selects the highest predicted CTR title.
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Any

import google.generativeai as genai

log = logging.getLogger("seo_oracle")

HOOK_MEMORY_FILE = Path("data/hook_memory.json")
HOOK_MEMORY_FILE.parent.mkdir(exist_ok=True)

POWER_WORDS = {
    "secret", "truth", "dark", "elite", "psychology", "persuasion", "bias",
    "psychology", "manipulation", "forbidden", "hidden", "exposed", "shocking",
    "silent", "strategy", "master", "broke", "lies", "trap", "real", "never",
    "always", "control", "mind", "power", "fear", "greed", "they", "you",
    "never told", "stop", "watch", "win", "lose", "destroy", "hack", "code",
}

SEO_PROMPT = """
You are a YouTube Shorts title strategist specializing in dark psychology,
behavioral persuasion, and social influence.
Given the actual opening hook extracted from the finished script below, generate
exactly 5 short YouTube titles that name or clearly reframe the hook's concrete
behavioral subject. Never substitute a generic wealth, crypto, or motivation title.

Rules:
- Each title MUST be under 60 characters (including any emoji).
- Each title MUST use a different psychological trigger:
  1. Curiosity gap     (e.g., "The Silent Test Hidden In Every Request 👁️")
  2. Fear of missing   (e.g., "Miss This Cue And You Lose The Frame")
  3. Social proof      (e.g., "Why Skilled Negotiators Pause Here 🤫")
  4. Urgency           (e.g., "Spot This Pressure Tactic Before You Agree ⚠️")
  5. Identity threat   (e.g., "The Mind Game That Makes You Doubt Yourself")
- You MAY use 1 relevant emoji per title to boost CTR — use it sparingly.
- No hashtags. Sentence case or title case only.
- Produce clean standalone titles. Never add prefixes such as "The secret:",
  "The truth:", "Here's why:", or "Why you should:".
- Output only a JSON array of 5 strings: ["title1", "title2", "title3", "title4", "title5"]

Seed context: {seed}
Actual script hook: {hook}
""".strip()

METADATA_PROMPT = """
You are an SEO specialist for YouTube Shorts in the dark psychology,
behavioral persuasion, and social influence niche.
Given the script below, generate high-retention video metadata.

Rules:
- description: Exactly 2-3 sentences. Packed with high-volume semantic search terms from the
  dark psychology / persuasion / behavioral science niche. Compelling, human tone. No hashtags,
  em-dashes, section labels, or repetitive punctuation. Use standard human paragraphs.
- hashtags: You MUST include ALL of these mandatory brand tags:
    #DarkPsychology #BehavioralPsychology #Persuasion #Mindset #Shorts
  Then add 2-3 topic-specific niche tags from:
    #CognitiveBias #SocialInfluence #MindGames #NegotiationSkills #BoundarySetting
    #ManipulationAwareness #BehavioralTraps #CriticalThinking

Script:
{script}

Return ONLY valid JSON with these exact keys:
{{
  "description": "...",
  "hashtags": ["#DarkPsychology", "#BehavioralPsychology", "#Persuasion", "#Mindset", "#Shorts", "#NicheTag1", "#NicheTag2"]
}}
""".strip()


def _load_memory() -> list[dict[str, Any]]:
    if not HOOK_MEMORY_FILE.exists() or HOOK_MEMORY_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(HOOK_MEMORY_FILE.read_text())
    except json.JSONDecodeError:
        return []


def record_winner(title: str, views: int, seed: str) -> None:
    memory = _load_memory()
    memory.append({"title": title, "views": views, "seed": seed})
    HOOK_MEMORY_FILE.write_text(json.dumps(memory[-200:], indent=2))


def _extract_power_word_score(title: str) -> int:
    lower = title.lower()
    return sum(1 for w in POWER_WORDS if w in lower)


def _predict_ctr_score(title: str) -> float:
    """Heuristic score: power words + historical wins + length sweet-spot."""
    score = float(_extract_power_word_score(title)) * 2.0
    length = len(title)
    if 30 <= length <= 55:
        score += 3.0
    elif length < 30:
        score += 1.5
    title_lower = title.lower()
    memory = _load_memory()
    for rec in memory[-50:]:
        winner_lower = rec.get("title", "").lower()
        words_match = sum(1 for w in winner_lower.split() if w in title_lower)
        if words_match >= 3:
            views = rec.get("views", 0)
            score += min(views / 5000.0, 3.0)
    return score


def _gemini_generate(prompt: str, max_tokens: int = 400) -> dict | list:
    """Call Gemini with JSON output mode, trying models in order."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    preferred = ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = "unknown"
    for model_name in preferred:
        try:
            model = genai.GenerativeModel(
                model_name,
                generation_config={
                    "temperature": 0.92,
                    "top_p": 0.9,
                    "max_output_tokens": max_tokens,
                    "response_mime_type": "application/json",
                },
            )
            resp = model.generate_content(prompt)
            return json.loads(resp.text.strip())
        except Exception as err:
            last_err = str(err)
            log.warning("Gemini %s failed: %s", model_name, err)
    raise RuntimeError(f"All Gemini models failed: {last_err}")


# ─── Title generation ──────────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    """Normalize titles for case/punctuation-insensitive duplicate checks."""
    return " ".join(re.findall(r"[a-z0-9]+", str(title).lower()))


def _hook_subject(hook: str) -> str:
    """Keep the title subject tied to the actual hook while leaving room for a suffix."""
    cleaned = re.sub(r"[^A-Za-z0-9'!?.,\s-]", "", str(hook))
    cleaned = " ".join(cleaned.split()).strip(" .,!?:;-")
    return textwrap.shorten(cleaned, width=34, placeholder="").strip(" .,!?:;-")


def _fallback_titles(hook: str) -> list[str]:
    """Build non-generic titles from the script hook when the model is unavailable."""
    subject = _hook_subject(hook) or "This influence tactic"
    return [
        f"{subject}: The Hidden Influence Pattern",
        f"{subject}: Spot The Mind Game",
        f"{subject}: Who Controls The Frame?",
        f"{subject}: The Persuasion Trap",
        f"{subject}: Break The Influence Loop",
    ]


def _unique_title_variants(
    candidates: list[str], hook: str, used_titles: list[str] | None = None
) -> list[str]:
    """Return five clean, word-safe titles absent from prior and current output."""
    reserved = {_title_key(title) for title in (used_titles or []) if _title_key(title)}
    selected: list[str] = []
    selected_keys: set[str] = set()
    for candidate in candidates + _fallback_titles(hook):
        cleaned = clean_title(candidate)
        key = _title_key(cleaned)
        if not key or key in reserved or key in selected_keys:
            continue
        selected.append(cleaned)
        selected_keys.add(key)
        if len(selected) == 5:
            break
    return selected


def generate_title_variants(
    seed: str,
    hook: str,
    script: str = "",
    used_titles: list[str] | None = None,
) -> list[str]:
    actual_hook = " ".join(str(hook or script).split())[:240]
    try:
        data = _gemini_generate(SEO_PROMPT.format(seed=seed, hook=actual_hook))
        if isinstance(data, list) and len(data) >= 5:
            titles = _unique_title_variants(
                [str(t) for t in data[:5]], actual_hook, used_titles
            )
            if len(titles) == 5:
                return titles
    except Exception as err:
        log.warning("seo title variants failed: %s", err)
    titles = _unique_title_variants([], actual_hook, used_titles)
    if len(titles) < 5:
        # This is only reachable when every normal hook variant is already used.
        # Keep the title hook-derived rather than falling back to a stock slogan.
        titles = _unique_title_variants(
            [f"{_hook_subject(actual_hook)} {suffix}" for suffix in (
                "Today", "Before You Agree", "In Plain Sight", "Under Pressure", "At Work"
            )],
            actual_hook,
            used_titles,
        )
    return titles


def clean_title(title: str) -> str:
    """Remove clickbait prefixes and truncate only at word boundaries."""
    cleaned = re.sub(
        r"^\s*(?:the\s+)?(?:secret|truth|answer|lesson|breakdown)\s*:\s*",
        "",
        str(title),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^\s*(?:here(?:'s| is)\s+why|why\s+you\s+should)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return textwrap.shorten(" ".join(cleaned.split()), width=60, placeholder="")


def pick_best_title(titles: list[str]) -> tuple[str, list[dict[str, Any]]]:
    scored = [{"title": t, "score": round(_predict_ctr_score(t), 2)} for t in titles]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[0]["title"], scored


# ─── Description + Hashtag generation ─────────────────────────────────────────

def generate_seo_metadata(script: str, seed: str) -> tuple[str, list[str]]:
    """
    LLM-powered metadata generation.

    Returns:
        description  — 2-3 sentence, dark-psych-keyword-stuffed summary
        hashtags     — list of 3-4 niche hashtags (each starts with #)
    """
    try:
        data = _gemini_generate(
            METADATA_PROMPT.format(script=script[:1800]),
            max_tokens=350,
        )
        if isinstance(data, dict):
            desc = str(data.get("description", "")).strip()
            raw_tags = data.get("hashtags") or []
            tags = [str(t).strip() for t in raw_tags if str(t).strip().startswith("#")]
            if desc and len(tags) >= 2:
                # Enforce mandatory brand tags regardless of what LLM returned
                seen_tags: set[str] = {t.lower() for t in tags}
                for must in _MANDATORY_HASHTAGS:
                    if must.lower() not in seen_tags:
                        tags.append(must)
                        seen_tags.add(must.lower())
                log.info("SEO metadata generated: %d-char desc, %d hashtags", len(desc), len(tags))
                return sanitize_description(desc), tags[:12]
    except Exception as err:
        log.warning("seo metadata generation failed: %s", err)

    return sanitize_description(_fallback_description(seed)), _fallback_hashtags()


def _fallback_description(seed: str) -> str:
    return (
        f"The influence pattern hidden inside {seed.strip()}. "
        "Learn to recognize dark psychology, persuasion tactics, and behavioral traps "
        "before they steer your decision. "
        "Use clear boundaries and critical thinking to keep control of the conversation."
    )


def sanitize_description(description: str) -> str:
    """Turn model punctuation into ordinary readable paragraph punctuation."""
    text = str(description).replace("\u2014", ", ").replace("\u2013", ", ")
    text = re.sub(r",\s*,+", ", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_MANDATORY_HASHTAGS = [
    "#DarkPsychology", "#BehavioralPsychology", "#Persuasion",
    "#Mindset", "#Shorts",
]


def _fallback_hashtags() -> list[str]:
    return _MANDATORY_HASHTAGS + ["#CognitiveBias", "#SocialInfluence"]
