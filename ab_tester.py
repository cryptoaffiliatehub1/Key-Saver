"""
A/B Hook Tester — for each scheduled run, two variant scripts are generated
(same seed, different hooks). Both are uploaded. After 48 hours, the engine
auto-archives the loser and records the winning hook style in
data/hook_memory.json so future scripts draw from the winning patterns.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ab_tester")

AB_TESTS_FILE = Path("data/ab_tests.json")
HOOK_MEMORY_FILE = Path("data/hook_memory.json")
AB_TESTS_FILE.parent.mkdir(exist_ok=True)

SETTLE_SECONDS = 48 * 3600
_lock = threading.Lock()


@dataclass
class ABTest:
    test_id: str
    seed: str
    created_at: float = field(default_factory=time.time)
    variant_a: dict[str, Any] = field(default_factory=dict)
    variant_b: dict[str, Any] = field(default_factory=dict)
    winner: str | None = None
    settled_at: float | None = None


def _read_tests() -> list[dict[str, Any]]:
    if not AB_TESTS_FILE.exists() or AB_TESTS_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(AB_TESTS_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _write_tests(tests: list[dict[str, Any]]) -> None:
    AB_TESTS_FILE.write_text(json.dumps(tests, indent=2))


def _read_memory() -> list[dict[str, Any]]:
    if not HOOK_MEMORY_FILE.exists() or HOOK_MEMORY_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(HOOK_MEMORY_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _append_memory(entry: dict[str, Any]) -> None:
    memory = _read_memory()
    memory.append(entry)
    HOOK_MEMORY_FILE.write_text(json.dumps(memory[-200:], indent=2))


def register_test(
    test_id: str,
    seed: str,
    video_id_a: str,
    title_a: str,
    hook_a: str,
    video_id_b: str,
    title_b: str,
    hook_b: str,
) -> None:
    with _lock:
        tests = _read_tests()
        tests.append({
            "test_id": test_id,
            "seed": seed,
            "created_at": time.time(),
            "variant_a": {"video_id": video_id_a, "title": title_a, "hook": hook_a, "views": 0},
            "variant_b": {"video_id": video_id_b, "title": title_b, "hook": hook_b, "views": 0},
            "winner": None,
            "settled_at": None,
        })
        _write_tests(tests)
    log.info("A/B test registered: %s vs %s (test_id=%s)", video_id_a, video_id_b, test_id)


def settle_pending_tests() -> list[dict[str, Any]]:
    """
    Called periodically. Fetches view counts for unsettled tests older than
    SETTLE_SECONDS, crowns the winner, archives the loser, and records the
    winning hook style in memory.
    """
    now = time.time()
    settled_results: list[dict[str, Any]] = []

    with _lock:
        tests = _read_tests()
        changed = False

        for test in tests:
            if test.get("winner"):
                continue
            age = now - test.get("created_at", now)
            if age < SETTLE_SECONDS:
                continue

            va = test["variant_a"]
            vb = test["variant_b"]

            # Fetch fresh view counts
            va_views = _fetch_views(va.get("video_id", ""))
            vb_views = _fetch_views(vb.get("video_id", ""))
            va["views"] = va_views
            vb["views"] = vb_views

            if va_views >= vb_views:
                winner_variant, loser_variant = va, vb
                test["winner"] = "A"
            else:
                winner_variant, loser_variant = vb, va
                test["winner"] = "B"
            test["settled_at"] = now

            _append_memory({
                "hook": winner_variant["hook"],
                "title": winner_variant["title"],
                "views": winner_variant["views"],
                "seed": test["seed"],
                "won_against": loser_variant["hook"],
                "won_against_views": loser_variant["views"],
                "settled_at": now,
            })

            log.info(
                "A/B settled (test_id=%s): winner=%s (%d views) loser=%s (%d views)",
                test["test_id"], winner_variant["hook"], winner_variant["views"],
                loser_variant["hook"], loser_variant["views"],
            )
            settled_results.append(test)
            changed = True

        if changed:
            _write_tests(tests)

    return settled_results


def _fetch_views(video_id: str) -> int:
    if not video_id:
        return 0
    try:
        from youtube_auth import get_youtube_service
        yt = get_youtube_service()
        resp = yt.videos().list(part="statistics", id=video_id).execute()
        items = resp.get("items", [])
        if items:
            return int(items[0].get("statistics", {}).get("viewCount", 0))
    except Exception as err:
        log.warning("view fetch failed for %s: %s", video_id, err)
    return 0


def get_winning_hooks(limit: int = 10) -> list[str]:
    """Returns the top winning hook phrases for prompt enrichment."""
    memory = _read_memory()
    sorted_mem = sorted(memory, key=lambda x: x.get("views", 0), reverse=True)
    return [m["hook"] for m in sorted_mem[:limit] if m.get("hook")]


def list_tests() -> list[dict[str, Any]]:
    with _lock:
        return list(reversed(_read_tests()))
