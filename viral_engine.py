"""
Viral Engine for YouTube Shorts — Wealth & Dark Psychology niche.

Pipeline:
1. Gemini (or OpenRouter fallback on 429) writes a hook+loop script.
2. ElevenLabs Sage Mentor voice (or gTTS fallback) narrates it.
3. Pexels supplies 6-8 vertical clips swapping every 3 seconds.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import google.generativeai as genai
import requests
from googleapiclient.http import MediaFileUpload

import ab_tester
import affiliate_comments
import audio_engine
import dashboard
import openrouter_fallback
import retention_engine
import seo_oracle
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
CAPTION_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

def _find_caption_font():
    for f in CAPTION_FONT_CANDIDATES:
        if _os.path.exists(f):
            return f
    return CAPTION_FONT_CANDIDATES[-1]

CAPTION_FONT = _find_caption_font()
CLIP_SWAP_SECONDS = 3.0
TARGET_W, TARGET_H = 1080, 1920
UPLOAD_DELAY_SECONDS = 5 * 60
SERIES_TAG = "WealthVault"
WATERMARK_TEXT = "Crypto Affiliate Hub"
WATERMARK_OPACITY = 0.10

HOOK_ARCHETYPES = [
    "The 1% secret...",
    "Why you are being manipulated...",
    "The dark truth about...",
    "They don't want you to know...",
    "The richest people on Earth do this...",
    "This is why you stay broke...",
    "The silent weapon of the elite...",
]

SCRIPT_MIN_WORDS = 150
SCRIPT_TARGET_WORDS = "150-190"
MIN_DURATION = 60
MAX_DURATION = 75

VIRAL_PROMPT = """
You are a YouTube Shorts strategist for the Wealth & Dark Psychology niche.
Write a 60-75 second narrator script that obeys EVERY rule:

1. Open with one of these hook archetypes (pick the strongest for the seed):
{hook_archetypes}

2. Use short punchy sentences. Build escalating tension across at least 5 distinct beats.
3. THE INFINITE LOOP RULE: the FINAL sentence must flow naturally so that
   re-reading the FIRST sentence right after it feels like the next beat.
4. DURATION REQUIREMENT: {target_words} spoken words minimum. No emojis. No stage directions.
   This is non-negotiable — a 60-75 second Short requires a full {target_words}-word narration.
5. After every major wealth principle, leave a natural [pause] beat (at least 4 pauses total).
6. Build through these phases: Hook → Tension → Revelation → Deep Insight → Twist → Loop.

Winning hook patterns from past top performers:
{winning_hooks}

Retention-optimised hooks (highest watch-time % on this channel — prioritise these patterns):
{retention_hooks}

Retention intelligence: {retention_insight}
Target: beat {target_retention}% average view duration on this video.

Trending keywords to weave in naturally:
{trending_kw}

Topic seed: {seed}

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


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:48] or "viral-short"


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


# ---------- 1. Script generation ----------

def _gemini_generate(prompt: str) -> dict:
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
                    "temperature": 0.95,
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


def generate_viral_script(seed: str) -> dict[str, Any]:
    winning  = ab_tester.get_winning_hooks(5)
    trending = trend_hunter.get_trending_seed_enrichment()
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
        seed=_luxury_prompt_guard(seed),
    )

    def _primary() -> dict:
        return _gemini_generate(prompt)

    data: dict | None = None
    script = ""
    for attempt in range(3):
        if attempt == 0:
            data = openrouter_fallback.call_with_fallback(prompt, _primary, temperature=0.95, max_tokens=2000)
        else:
            expand_prompt = prompt + (
                f"\n\nWARNING: Your previous script was too short ({len(script.split())} words). "
                f"You MUST write at least {SCRIPT_MIN_WORDS} words. Expand every beat significantly. "
                "Add more concrete examples, deeper psychological insight, and additional [pause] beats."
            )
            def _expand_primary() -> dict:
                return _gemini_generate(expand_prompt)
            data = openrouter_fallback.call_with_fallback(expand_prompt, _expand_primary, temperature=0.92, max_tokens=2000)

        script = str(data.get("script", "")).strip()
        word_count = len(script.split())
        if word_count >= SCRIPT_MIN_WORDS:
            log.info("Script generated: %d words (attempt %d)", word_count, attempt + 1)
            break
        log.warning("Script too short (%d words < %d min), regenerating (attempt %d/3)...", word_count, SCRIPT_MIN_WORDS, attempt + 1)

    keywords = data.get("keywords", []) or []
    description = str(data.get("description", "")).strip()
    tags = data.get("tags", []) or []
    if not script or len(keywords) < 6:
        raise RuntimeError("Gemini returned an incomplete script.")

    if "#Shorts" not in description:
        description = f"{description}\n\n#Shorts #Wealth #Psychology"

    first_line = str(data.get("first_line") or script.split(".")[0]).strip()
    last_line = str(data.get("last_line") or script.split(".")[-2]).strip()

    # SEO Oracle: generate 5 title variants and auto-pick best
    title_variants = seo_oracle.generate_title_variants(seed, first_line, script)
    best_title, scored_titles = seo_oracle.pick_best_title(title_variants)

    return {
        "script": script,
        "first_line": first_line,
        "last_line": last_line,
        "keywords": [str(k).strip() for k in keywords[:8]],
        "title": best_title,
        "title_variants": scored_titles,
        "description": description,
        "tags": [str(t).strip().lstrip("#") for t in tags][:12] or [
            "wealth", "psychology", "shorts", "mindset", "darkpsychology"
        ],
    }


def _luxury_prompt_guard(seed: str) -> str:
    prompt = seed.strip()
    if re.search(r"\b(sexy|casual)\b", prompt, re.IGNORECASE):
        return re.sub(r"\b(sexy|casual)\b", "8K cinematic billionaire luxury", prompt, flags=re.IGNORECASE)
    return prompt


# ---------- 2. Voiceover (delegated to audio_engine) ----------
# Four-tier fallback: ElevenLabs → Deepgram → Fish Audio → gTTS


# ---------- 3. Pexels download ----------

def _pexels_headers() -> dict[str, str]:
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        raise RuntimeError("PEXELS_API_KEY is not set.")
    return {"Authorization": api_key}


def _pick_video_file(video: dict) -> str | None:
    files = video.get("video_files", []) or []
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or files
    pool.sort(key=lambda f: abs((f.get("height") or 0) - 1280))
    for f in pool:
        link = f.get("link")
        if link:
            return link
    return None


def download_pexels_clips(keywords: list[str], work_dir: Path, target: int = 8) -> list[Path]:
    headers = _pexels_headers()
    saved: list[Path] = []
    queries = list(keywords)
    random.shuffle(queries)
    for keyword in queries:
        if len(saved) >= target:
            break
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": keyword, "orientation": "portrait", "per_page": 3},
                timeout=25,
            )
            r.raise_for_status()
            for video in r.json().get("videos", []) or []:
                if len(saved) >= target:
                    break
                file_url = _pick_video_file(video)
                if not file_url:
                    continue
                dest = work_dir / f"clip_{len(saved):02d}_{_slug(keyword)}.mp4"
                with requests.get(file_url, stream=True, timeout=120) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            if chunk:
                                fh.write(chunk)
                if dest.stat().st_size > 50_000:
                    saved.append(dest)
                    log.info("Downloaded %s (%s bytes)", dest.name, dest.stat().st_size)
        except requests.RequestException as err:
            log.warning("Pexels error for %r: %s", keyword, err)
    if len(saved) < 3:
        raise RuntimeError(f"Only {len(saved)} clips downloaded; need at least 3.")
    return saved


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
    # Safety trim: moviepy reads audio in small lookahead windows (~0.04 s).
    # If the clip is exactly N seconds long, the last window overshoots by a
    # few milliseconds and raises "Accessing time t=N.01… with duration=N".
    # Trimming 0.08 s off the end prevents this without any audible difference.
    _raw_dur = audio.duration
    _safe_dur = max(_raw_dur - 0.08, _raw_dur * 0.995)  # whichever is less aggressive
    audio = audio.subclipped(0, _safe_dur)
    total = max(audio.duration + 0.3, CLIP_SWAP_SECONDS * 3)

    video_clips = []
    elapsed = 0.0
    idx = 0
    while elapsed < total:
        src_path = clip_paths[idx % len(clip_paths)]
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
        elapsed += take

    base = concatenate_videoclips(video_clips, method="chain").subclipped(0, total)

    # Word-by-word centred captions
    words = [w for w in re.findall(r"\S+", re.sub(r"\[pause[^]]*\]", "", script))]
    caption_clips = []
    if words:
        per_word = audio.duration / len(words)
        for i, word in enumerate(words):
            # Cap each caption so the last word never extends past the safe audio end
            w_start = i * per_word
            w_end = min(w_start + per_word, audio.duration)
            if w_start >= audio.duration:
                break
            try:
                txt = TextClip(
                    text=word.upper(),
                    font=CAPTION_FONT,
                    font_size=110,
                    color="white",
                    stroke_color="black",
                    stroke_width=3,
                    method="caption",
                    size=(int(TARGET_W * 0.85), None),
                    text_align="center",
                )
                txt = (
                    txt.with_start(w_start)
                    .with_duration(w_end - w_start)
                    .with_position(("center", int(1920 * 0.45)))
                )
                caption_clips.append(txt)
            except Exception as err:
                log.warning("Caption failed for %r: %s", word, err)

    # Ghost watermark
    wm = _watermark_clip(total)
    overlay_clips = caption_clips + ([wm] if wm else [])

    composite = CompositeVideoClip([base, *overlay_clips], size=(TARGET_W, TARGET_H))
    final = composite.with_audio(audio).with_duration(audio.duration)

    # ── ENFORCE 60-75s DURATION LOCK ──
    final_duration = final.duration
    if final_duration < MIN_DURATION:
        log.warning("Video duration %.1fs < min %ds; padding to %ds", final_duration, MIN_DURATION, MIN_DURATION)
        final = final.with_duration(MIN_DURATION)
    elif final_duration > MAX_DURATION:
        log.warning("Video duration %.1fs > max %ds; clipping to %ds", final_duration, MAX_DURATION, MAX_DURATION)
        final = final.subclipped(0, MAX_DURATION)
    else:
        log.info("Video duration %.1fs locked within 60-75s range", final_duration)

    out_path = work_dir / "short.mp4"
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
    )

    for obj in [final, composite, base, audio]:
        try:
            obj.close()
        except Exception:
            pass

    return out_path


# ---------- 5. YouTube upload ----------

def _do_upload(service, video_path: Path, title: str, description: str, tags: list[str]) -> str:
    """Inner upload call — separated so the token-refresh retry can reuse it."""
    full_tags = list(tags) + [SERIES_TAG, "WealthVaultEntry"]
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
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


# ---------- 6. Cleanup ----------

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

# Bounded thread pool — max 2 concurrent pipeline runs so we never exhaust OS threads
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
    _set(job, title=plan["title"], title_variants=plan.get("title_variants"))

    _set(job, state="voiceover", message="Recording narration (ElevenLabs → Deepgram → Fish Audio → gTTS)...")
    voice_path = audio_engine.make_voiceover(plan["script"], work_dir)

    _set(job, state="downloading", message="Downloading 6-8 Pexels clips...")
    clip_paths = download_pexels_clips(plan["keywords"], work_dir, target=8)

    _set(job, state="rendering", message=f"Stitching {len(clip_paths)} clips + captions + watermark...")
    video_path = build_short(plan["script"], voice_path, clip_paths, work_dir)

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


def run_in_background(seed: str, do_upload: bool = True, ab_mode: bool = False) -> JobStatus:
    job = JobStatus(id=uuid.uuid4().hex[:8], seed=seed, state="queued", message="Job queued.")
    with _jobs_lock:
        _jobs[job.id] = job

    def _worker() -> None:
        result = run_viral_pipeline(seed, do_upload=do_upload, ab_mode=ab_mode)
        with _jobs_lock:
            _jobs.pop(job.id, None)
            _jobs[result.id] = result

    _pipeline_executor.submit(_worker)
    return job


SEED_POOL = [
    "the dark psychology trick the rich use to control conversations",
    "why broke people stay broke according to behavioral economics",
    "the 1% secret to building generational wealth most ignore",
    "manipulation tactics billionaires use without you noticing",
    "how the elite weaponize silence to get whatever they want",
    "the cold truth about money that schools refuse to teach",
    "why your friends secretly want you to fail (and how to spot it)",
    "the dark side of compound interest no one tells you",
    "the wealth gap is not an accident — here is the blueprint",
    "ancient elite money rituals that still work in 2026",
]


def random_seed() -> str:
    return random.choice(SEED_POOL)
