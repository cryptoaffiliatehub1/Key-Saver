"""
OpenRouter fallback — called automatically when Gemini returns a 429 rate-limit
or quota-exceeded error. Falls back to GPT-4o or Claude 3.5 via OpenRouter.
Requires OPENROUTER_API_KEY to be set as a secret.

Also provides get_patch_suggestion() for the auto-patch crash recovery loop.
"""
from __future__ import annotations

import json
import logging
import os
import re

import requests

log = logging.getLogger("openrouter_fallback")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
PREFERRED_MODELS = [
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemma-2-9b-it:free",
    "mistralai/mistral-7b-instruct:free",
    "openai/gpt-4o",
]
PATCH_MODELS = [
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.1-8b-instruct:free",
    "openai/gpt-4o",
]


def is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "quota", "rate limit", "resource_exhausted", "too many requests"))


def is_token_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("invalid_grant", "token has been expired", "token_expired",
                                   "token has been revoked", "invalid grant", "unauthorized"))


def _headers() -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Add it as a secret to enable the fallback.")
    return {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://replit.com",
        "X-Title": "Wealth Vault Viral Engine",
        "Content-Type": "application/json",
    }


def call_openrouter(prompt: str, temperature: float = 0.92, max_tokens: int = 1400,
                    models: list[str] | None = None) -> str:
    headers = _headers()
    last_error: Exception | None = None
    for model in (models or PREFERRED_MODELS):
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            resp = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            log.info("OpenRouter fallback succeeded via %s", model)
            return content
        except Exception as err:
            log.warning("OpenRouter %s failed: %s", model, err)
            last_error = err

    raise RuntimeError(f"All OpenRouter models failed. Last error: {last_error}")


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def call_with_fallback(prompt: str, primary_call_fn, temperature: float = 0.92, max_tokens: int = 1400) -> dict:
    """
    Tries primary_call_fn() first. If it raises any error (rate-limit, quota,
    or model-not-found), falls back to OpenRouter immediately without stopping
    the job. Returns a parsed dict.
    """
    try:
        return primary_call_fn()
    except Exception as exc:
        if is_rate_limit_error(exc):
            log.warning("Gemini 429 — pivoting to OpenRouter bridge. Error: %s", exc)
        else:
            log.warning("Gemini failed (%s) — pivoting to OpenRouter bridge.", type(exc).__name__)
        raw = call_openrouter(prompt, temperature=temperature, max_tokens=max_tokens)
        return _extract_json(raw)


def get_patch_suggestion(traceback_str: str, code_context: str = "") -> str:
    """
    Send a Python traceback (+ optional code snippet) to OpenRouter and return
    the suggested corrected code block as a raw string.

    Used by the auto-patch crash recovery loop in viral_engine.py.
    Returns an empty string if OPENROUTER_API_KEY is not set or all models fail.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        return ""

    context_block = f"\n\nRelevant code context:\n```python\n{code_context}\n```" if code_context else ""
    prompt = (
        "You are a senior Python engineer debugging a production crash.\n"
        "Fix this Python error and provide ONLY the corrected code block — "
        "no explanation, no markdown, no commentary.\n\n"
        f"Traceback:\n```\n{traceback_str}\n```"
        f"{context_block}"
    )

    headers = _headers()
    for model in PATCH_MODELS:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 800,
            }
            resp = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=45)
            resp.raise_for_status()
            suggestion = resp.json()["choices"][0]["message"]["content"]
            log.info("Auto-patch suggestion received from %s (%d chars)", model, len(suggestion))
            return suggestion.strip()
        except Exception as err:
            log.warning("Patch suggestion via %s failed: %s", model, err)

    return ""
