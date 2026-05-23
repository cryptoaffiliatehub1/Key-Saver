"""
SEO Oracle — generates 5 title variations per video and auto-selects the
highest predicted CTR title based on viral history stored in data/hook_memory.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import google.generativeai as genai

log = logging.getLogger("seo_oracle")

HOOK_MEMORY_FILE = Path("data/hook_memory.json")
HOOK_MEMORY_FILE.parent.mkdir(exist_ok=True)

POWER_WORDS = {
    "secret", "truth", "dark", "elite", "1%", "rich", "wealth", "billionaire",
    "psychology", "manipulation", "forbidden", "hidden", "exposed", "shocking",
    "silent", "strategy", "master", "broke", "lies", "trap", "real", "never",
    "always", "control", "mind", "power", "fear", "greed", "they", "you",
    "never told", "stop", "watch", "win", "lose", "destroy", "hack", "code",
}

SEO_PROMPT = """
You are a YouTube Shorts title strategist specializing in Wealth & Dark Psychology.
Given the script seed and hook below, generate exactly 5 short YouTube titles.

Rules:
- Each title MUST be under 50 characters.
- Each title MUST use a different psychological trigger:
  1. Curiosity gap (e.g., "The secret they...")
  2. Fear of missing out (e.g., "If you don't know this...")
  3. Social proof (e.g., "The 1% trick...")
  4. Urgency (e.g., "Stop doing this NOW")
  5. Identity threat (e.g., "This is why you're still broke")
- No hashtags. No emojis. Plain text only.
- Output only a JSON array of 5 strings: ["title1", "title2", "title3", "title4", "title5"]

Seed: {seed}
Hook: {hook}
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
    """Heuristic score: power words + historical wins + length penalty."""
    score = float(_extract_power_word_score(title)) * 2.0
    length = len(title)
    if 30 <= length <= 46:
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


def generate_title_variants(seed: str, hook: str, script: str = "") -> list[str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _fallback_titles(hook)
    genai.configure(api_key=api_key)
    preferred = ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]
    for model_name in preferred:
        try:
            model = genai.GenerativeModel(
                model_name,
                generation_config={
                    "temperature": 0.92,
                    "top_p": 0.9,
                    "max_output_tokens": 400,
                    "response_mime_type": "application/json",
                },
            )
            resp = model.generate_content(SEO_PROMPT.format(seed=seed, hook=hook))
            data = json.loads(resp.text.strip())
            if isinstance(data, list) and len(data) >= 5:
                titles = [str(t).strip()[:50] for t in data[:5]]
                return titles
        except Exception as err:
            log.warning("seo title gen failed (%s): %s", model_name, err)
    return _fallback_titles(hook)


def _fallback_titles(hook: str) -> list[str]:
    short = hook[:30].rstrip()
    return [
        f"The secret: {short}"[:50],
        f"Stop ignoring this truth"[:50],
        f"The 1% do this silently"[:50],
        f"This is why you're broke"[:50],
        f"They don't want you to know"[:50],
    ]


def pick_best_title(titles: list[str]) -> tuple[str, list[dict[str, Any]]]:
    scored = [{"title": t, "score": round(_predict_ctr_score(t), 2)} for t in titles]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[0]["title"], scored
