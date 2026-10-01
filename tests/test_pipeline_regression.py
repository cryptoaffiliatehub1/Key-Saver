"""Fast regression coverage for the refactored content pipeline.

The test-only PIPELINE_TESTING flag prevents audio_engine's import-time credit
poll from touching provider APIs. All provider boundaries exercised below are
explicitly mocked.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["PIPELINE_TESTING"] = "1"

from PIL import Image, ImageDraw, ImageFont

import audio_engine
import seo_oracle
import topic_memory
import youtube_auth
from seo_oracle import clean_title
from viral_engine import (
    CAPTION_FONT,
    _caption_lines,
    _mood_media_queries,
    sanitize_spoken_script,
)
from viral_engine import _generate_concept_matrix


class PipelineRegressionTests(unittest.TestCase):
    def test_sanitize_spoken_script_removes_messy_structural_tags(self) -> None:
        source = (
            "[Hook] Welcome. Twist: Here is the trick. "
            "[Body] It works because [pause 0.4] incentives compound. "
            "CTA: Follow for the next breakdown."
        )
        cleaned = sanitize_spoken_script(source)

        self.assertNotRegex(cleaned, r"(?i)\[hook\]|\[body\]|\[pause")
        self.assertNotRegex(cleaned, r"(?i)\b(?:hook|body|twist|cta)\s*:")
        self.assertNotIn("—", cleaned)
        self.assertEqual(
            cleaned,
            "Welcome. Here is the trick. It works because incentives compound. Follow for the next breakdown.",
        )

    def test_caption_lines_measure_pixels_and_wrap_without_splitting_words(self) -> None:
        safe_width = 864
        source = (
            "This caption contains enough words to require clean multi-line wrapping "
            "while preserving every complete word and keeping each rendered line safe."
        )
        lines, fitted_size = _caption_lines(source, CAPTION_FONT, 62, safe_width)

        probe = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(probe)
        font = ImageFont.truetype(CAPTION_FONT, fitted_size)
        rendered_widths = [
            draw.textbbox((0, 0), line, font=font)[2] for line in lines
        ]
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(width <= safe_width for width in rendered_widths))
        self.assertEqual(" ".join(lines).split(), source.split())

        oversized_word = "W" * 45
        reduced_lines, reduced_size = _caption_lines(
            oversized_word, CAPTION_FONT, 62, safe_width
        )
        self.assertLess(reduced_size, 62)
        reduced_font = ImageFont.truetype(CAPTION_FONT, reduced_size)
        self.assertLessEqual(
            draw.textbbox((0, 0), reduced_lines[0], font=reduced_font)[2],
            safe_width,
        )

    def test_topic_json_hooks_are_thread_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "used_topics.json"
            ledger.write_text("[]\n", encoding="utf-8")
            with patch.object(topic_memory, "_USED_TOPICS_PATH", ledger):
                def append(index: int) -> None:
                    topic_memory.append_used_topic(
                        "regression-seed",
                        {
                            "topic": f"Topic {index}",
                            "angle": "A distinct angle",
                            "subtopic": "Opportunity Cost",
                        },
                    )

                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(append, range(24)))

                entries = topic_memory.recent_used_topics(100)
                self.assertEqual(len(entries), 24)
                self.assertEqual(
                    {entry["topic"] for entry in entries},
                    {f"Topic {index}" for index in range(24)},
                )
                self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), entries)

    def test_tts_provider_boundary_is_mocked_and_receives_clean_text(self) -> None:
        response = Mock()
        response.content = b"audio" * 300
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.mp3"
            with patch.object(audio_engine.requests, "post", return_value=response) as post:
                audio_engine._elevenlabs(
                    "Build systems — not wishes. Measure the downside before scaling.",
                    destination,
                    "test-key",
                )
                self.assertTrue(destination.exists())

        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("—", payload["text"])
        self.assertIn("Build systems...", payload["text"])
        self.assertIn("\n", payload["text"])
        voice_settings = payload["voice_settings"]
        self.assertEqual(post.call_args.args[0], f"https://api.elevenlabs.io/v1/text-to-speech/{audio_engine.ELEVENLABS_VOICE_ID}")
        self.assertEqual(audio_engine.ELEVENLABS_VOICE_ID, "pNInz6obpgmA5QC9632W")
        self.assertGreaterEqual(voice_settings["stability"], 0.35)
        self.assertLessEqual(voice_settings["stability"], 0.45)
        self.assertGreaterEqual(voice_settings["similarity_boost"], 0.75)
        self.assertLessEqual(voice_settings["similarity_boost"], 0.85)
        self.assertGreaterEqual(voice_settings["style"], 0.15)
        self.assertLessEqual(voice_settings["style"], 0.25)

    def test_tts_cleanup_removes_fillers_orphan_punctuation_and_long_pauses(self) -> None:
        cleaned = audio_engine._clean(
            "Uh,  listen... ... um!  The frame!!!!  ;  really matters????"
        )
        self.assertNotRegex(cleaned, r"(?i)\b(?:uh|um)\b")
        self.assertNotRegex(cleaned, r" {2,}")
        self.assertNotRegex(cleaned, r"(?<!\w)[,;:!?]")
        self.assertNotRegex(cleaned, r"\.{4,}|!{2,}|\?{2,}")
        self.assertIn("listen", cleaned.lower())
        self.assertIn("frame", cleaned.lower())

    def test_youtube_service_boundary_is_mocked(self) -> None:
        credentials = object()
        service = object()
        with patch.object(youtube_auth, "load_credentials", return_value=credentials), \
             patch.object(youtube_auth, "build", return_value=service) as build:
            self.assertIs(youtube_auth.get_youtube_service(), service)

        build.assert_called_once_with(
            "youtube", "v3", credentials=credentials, cache_discovery=False
        )

    def test_title_cleanup_is_word_safe_and_prefix_free(self) -> None:
        title = clean_title(
            "The secret: Build better systems for capital and protect your upside"
        )
        self.assertNotIn("The secret:", title)
        self.assertLessEqual(len(title), 60)
        self.assertFalse(title.endswith("…"))
        self.assertFalse(title.endswith(" "))

    def test_title_fallback_is_hook_derived_and_deduplicates_prior_titles(self) -> None:
        hook = "The pause that quietly changes the power balance"
        with patch.object(
            seo_oracle, "_gemini_generate", side_effect=RuntimeError("offline")
        ):
            first_batch = seo_oracle.generate_title_variants(
                "test seed", hook, "The full script begins here."
            )
            second_batch = seo_oracle.generate_title_variants(
                "test seed", hook, "The full script begins here.", first_batch
            )
        self.assertEqual(len(first_batch), 5)
        self.assertEqual(len(second_batch), 5)
        self.assertEqual(len({_title.lower() for _title in first_batch}), 5)
        self.assertTrue(
            {_title.lower() for _title in first_batch}.isdisjoint(
                {_title.lower() for _title in second_batch}
            )
        )
        self.assertTrue(all("pause" in title.lower() for title in first_batch))
        self.assertNotIn("stop ignoring this truth", " ".join(first_batch).lower())
        self.assertTrue(all(len(title) <= 60 for title in first_batch + second_batch))

    def test_titles_persist_in_json_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "used_topics.json"
            database = Path(temp_dir) / "topic_memory.db"
            ledger.write_text("[]\n", encoding="utf-8")
            concept = {
                "topic": "Reciprocity pressure in meetings",
                "angle": "Small favors can create false obligation",
                "subtopic": "Dark Psychology in Negotiations",
            }
            with patch.object(topic_memory, "_USED_TOPICS_PATH", ledger), \
                 patch.object(topic_memory, "_DB_PATH", database):
                topic_memory.append_used_topic("hook seed", concept)
                topic_memory.record_entry("hook seed", concept, "The Reciprocity Trap")
                entries = topic_memory.recent_used_topics(10)
            self.assertEqual(entries[-1]["title"], "The Reciprocity Trap")
            self.assertEqual(entries[-1]["niche"], topic_memory.CURRENT_NICHE)

    def test_media_queries_are_dark_psychology_specific(self) -> None:
        queries = _mood_media_queries(["negotiation pause", "boundary setting"])
        self.assertEqual(len(queries), 2)
        self.assertTrue(
            all(
                phrase in query
                for query in queries
                for phrase in ("cinematic", "dark", "psychology", "influence")
            )
        )
        self.assertNotEqual(queries[0], "negotiation pause")

    def test_concept_matrix_accepts_wrapped_provider_arrays(self) -> None:
        concepts = [
            {
                "topic": f"Specific topic {index}",
                "angle": "A concrete angle",
                "mechanism": "A measurable mechanism",
            }
            for index in range(5)
        ]
        with patch(
            "viral_engine.openrouter_fallback.call_with_fallback",
            return_value={"items": concepts},
        ):
            result = _generate_concept_matrix(
                "test seed", "[]", "Opportunity Cost"
            )
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["topic"], "Specific topic 0")


if __name__ == "__main__":
    unittest.main()