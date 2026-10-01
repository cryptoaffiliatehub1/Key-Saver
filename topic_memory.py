"""
Persistent topic memory for the viral content pipeline.

The ledger is intentionally small and local: SQLite gives us durable
cross-process storage without introducing another service or dependency.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any

log = logging.getLogger("topic_memory")

_DB_PATH = Path("data/topic_memory.db")
_USED_TOPICS_PATH = Path("data/used_topics.json")
CURRENT_NICHE = "dark_psychology"
_DB_PATH.parent.mkdir(exist_ok=True)
if not _USED_TOPICS_PATH.exists():
    _USED_TOPICS_PATH.write_text("[]\n", encoding="utf-8")
_lock = threading.RLock()

ACTIONABLE_SUBTOPICS = (
    "Dark Psychology in Negotiations",
    "Cognitive Biases & Mind Games",
    "Asymmetric Influence Techniques",
    "Perception Manipulation & Value Framing",
    "Spotting Deceptive Leverage",
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(str(_DB_PATH), timeout=30, check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            seed TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL,
            angle TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            niche TEXT NOT NULL DEFAULT 'legacy'
        )
        """
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(generated_topics)").fetchall()
    }
    if "niche" not in columns:
        connection.execute(
            "ALTER TABLE generated_topics ADD COLUMN niche TEXT NOT NULL DEFAULT 'legacy'"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_generated_topics_created_at "
        "ON generated_topics(created_at DESC)"
    )
    connection.commit()
    return connection


def recent_entries(
    limit: int = 50, niche: str | None = CURRENT_NICHE
) -> list[dict[str, Any]]:
    """Return the newest generated topic records for prompt context."""
    safe_limit = max(1, min(int(limit), 50))
    with _lock:
        connection = _connection()
        try:
            niche_filter = "WHERE niche = ?" if niche else ""
            params: tuple[Any, ...] = (niche, safe_limit) if niche else (safe_limit,)
            rows = connection.execute(
                f"""
                SELECT seed, topic, angle, title, created_at
                FROM generated_topics
                {niche_filter}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [
                {
                    "seed": row[0],
                    "topic": row[1],
                    "angle": row[2],
                    "title": row[3],
                    "created_at": row[4],
                }
                for row in rows
            ]
        finally:
            connection.close()


def entries_json(limit: int = 50, niche: str | None = CURRENT_NICHE) -> str:
    """Return recent history as compact JSON suitable for a model prompt."""
    return json.dumps(
        recent_entries(limit, niche=niche),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def recent_used_topics(
    limit: int = 50, niche: str | None = None
) -> list[dict[str, Any]]:
    """Read the required human-auditable JSON topic history."""
    safe_limit = max(1, int(limit))
    with _lock:
        if not _USED_TOPICS_PATH.exists():
            return []
        try:
            data = json.loads(_USED_TOPICS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Unable to read used topic history; treating it as empty.")
            return []
        if not isinstance(data, list):
            return []
        records = [item for item in data if isinstance(item, dict)]
        if niche:
            records = [item for item in records if item.get("niche") == niche]
        return records[-safe_limit:]


def recent_used_titles(limit: int = 100_000) -> list[str]:
    """Return normalized-independent title strings for cross-run deduplication."""
    titles: list[str] = []
    for item in recent_used_topics(limit):
        title = str(item.get("title") or "").strip()
        if title:
            titles.append(title)
    return titles


def next_subtopic() -> str:
    """Choose the least-used actionable sub-topic, rotating on ties."""
    history = recent_used_topics(100_000, niche=CURRENT_NICHE)
    counts = {name: 0 for name in ACTIONABLE_SUBTOPICS}
    for item in history:
        name = str(item.get("subtopic") or "")
        if name in counts:
            counts[name] += 1
    return min(
        ACTIONABLE_SUBTOPICS,
        key=lambda name: (counts[name], ACTIONABLE_SUBTOPICS.index(name)),
    )


def append_used_topic(seed: str, concept: dict[str, Any]) -> None:
    """Immediately append the selected concept to the required JSON log."""
    topic = str(concept.get("topic") or concept.get("concept") or "").strip()
    if not topic:
        raise ValueError("Cannot persist a selected topic without a topic name.")
    entry = {
        "topic": topic,
        "angle": str(concept.get("angle") or "").strip(),
        "subtopic": str(concept.get("subtopic") or "").strip(),
        "seed": str(seed).strip(),
        "title": "",
        "niche": CURRENT_NICHE,
        "selected_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        history = recent_used_topics(100_000)
        history.append(entry)
        _USED_TOPICS_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def record_entry(seed: str, concept: dict[str, Any], title: str) -> None:
    """Persist the selected concept and the title ultimately generated for it."""
    topic = str(concept.get("topic") or concept.get("concept") or "").strip()
    angle = str(concept.get("angle") or concept.get("mechanism") or "").strip()
    if not topic:
        log.warning("Topic memory skipped a record with no topic.")
        return
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        connection = _connection()
        try:
            connection.execute(
                """
                INSERT INTO generated_topics (created_at, seed, topic, angle, title, niche)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    str(seed).strip(),
                    topic,
                    angle,
                    str(title).strip(),
                    CURRENT_NICHE,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        history = recent_used_topics(100_000)
        title_value = str(title).strip()
        seed_value = str(seed).strip()
        updated = False
        for item in reversed(history):
            if (
                str(item.get("seed") or "").strip() == seed_value
                and str(item.get("topic") or "").strip() == topic
            ):
                item["title"] = title_value
                item["niche"] = CURRENT_NICHE
                updated = True
                break
        if not updated:
            history.append(
                {
                    "topic": topic,
                    "angle": angle,
                    "subtopic": str(concept.get("subtopic") or "").strip(),
                    "seed": seed_value,
                    "title": title_value,
                    "niche": CURRENT_NICHE,
                    "selected_at": now,
                }
            )
        _USED_TOPICS_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.lower())
        if token not in {"the", "and", "for", "with", "from", "that", "this"}
    }


def novelty_score(concept: dict[str, Any], history: list[dict[str, Any]]) -> float:
    """
    Score a concept from 0 to 1, where 1 means no meaningful overlap.

    Both token overlap and sequence similarity are used so reordered phrases
    and near-copy concepts are penalized.
    """
    if not history:
        return 1.0
    candidate = " ".join(
        str(concept.get(key) or "")
        for key in ("topic", "angle", "mechanism", "audience")
    ).strip()
    candidate_tokens = _tokens(candidate)
    highest_similarity = 0.0
    for previous in history:
        prior = " ".join(
            str(previous.get(key) or "")
            for key in ("topic", "angle", "title")
        ).strip()
        prior_tokens = _tokens(prior)
        token_similarity = (
            len(candidate_tokens & prior_tokens) / len(candidate_tokens | prior_tokens)
            if candidate_tokens and prior_tokens
            else 0.0
        )
        sequence_similarity = SequenceMatcher(
            None, candidate.lower(), prior.lower()
        ).ratio()
        highest_similarity = max(
            highest_similarity,
            (token_similarity * 0.65) + (sequence_similarity * 0.35),
        )
    return round(max(0.0, 1.0 - highest_similarity), 4)


def select_most_novel(
    concepts: list[dict[str, Any]], history: list[dict[str, Any]]
) -> tuple[dict[str, Any], float]:
    """Pick the highest-novelty concept, preserving model order on ties."""
    if not concepts:
        raise ValueError("Concept matrix was empty.")
    ranked = [(novelty_score(concept, history), concept) for concept in concepts]
    score, selected = max(ranked, key=lambda item: item[0])
    return selected, score