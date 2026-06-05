# Roadmap / Notes — ILLUSTRATOR

Planned future work. Issue-style so it can be copy-pasted into GitHub Issues once
this project gets its own repo (it isn't a git repo yet — see CLAUDE.md).

---

## Thumbnail generator (cover image for the clip) — enhancement

**Status:** planned

**Why**

Same need as clipper: after rendering, you need a **thumbnail / cover image** to
post the short (TikTok/Shorts/Reels). Today only the video comes out; the cover is
made in a separate tool.

**Proposed**

A **"Generate Thumbnail"** button that produces a cover IMAGE for the clip:
- **Frame pick** — scrub + grab a frame, or auto-suggest (brightest / face frame,
  or reuse the top crop box keyframe times the user already set).
- **Bold title text** overlay — reuse the bundled **Anton / Bebas Neue** + the
  fat-stroke caption style (separate from the burned karaoke captions), with
  position + accent-color options.
- **Aspect/size** — primarily **1080×1920 (9:16)** to match the output, plus
  optional 1280×720 (16:9) / 1080×1080 (square). Export **PNG/JPG** to `output/`.
- **Compose with the illustration** (illustrator-specific) — the thumbnail can mix
  the top video frame + the picked bottom illustration, or just one. Could offer a
  few **candidate thumbnails** (different frames / illustrations / text) to pick
  from, reusing the existing illustration-picker UI pattern.
- **Stretch** — subject cutout (person segmentation) over an accent/illustration bg.

**Implementation notes**

- New route `POST /api/thumbnail` `{job_id, frame_time, title, font, size, accent,
  illustration_url?}` → ffmpeg single-frame render (`-ss frame_time -frames:v 1`,
  ASS/drawtext text via the bundled fonts + `to_ass_color()`, compose top crop +
  bottom illustration like the main render, scale/pad to chosen size) →
  `output/{slug}_thumb.png`. **ffmpeg-only, no new heavy deps.**
- Reuse `assets/fonts/`, `renderer.to_ass_color()`, the crop/compose machinery in
  `renderer.py`, and `illustrator.download_pick()` for the chosen illustration.

**Acceptance**

- One click → a ready-to-post 9:16 thumbnail image with bold title text (and
  optionally the chosen illustration), saved next to the rendered video.

---

## Transcription accuracy (Whisper) — "the voice isn't accurate enough"

**Status:** ✅ mostly DONE 2026-06 (shared with clipper — `transcriber.py` is identical).
Done: GPU auto (torch fixed to `cu128` so the 4050 gets used), default model **medium** on GPU,
`WHISPER_LANGUAGE=id`, `condition_on_previous_text=False`, VRAM freed after transcribing + CPU
fallback on OOM. **Remaining (stretch):** `large-v3` (needs >6GB VRAM / faster-whisper int8) for
maximum accuracy, and `initial_prompt` from the title/topic (the param already exists, just needs
to be plumbed into `/api/transcribe`).

**Why**

Whisper often mishears, especially **Indonesian** + **specific names/terms** (people's
names, places, religious terms). In illustrator the effect is **compounded**: a wrong transcript →
`llm.queries_for_segments` gets the wrong context → the Pexels query misses → the illustration goes astray.
So voice accuracy is the foundation of the entire illustration pipeline, not just the captions.

**Root cause** (`backend/transcriber.py`)

1. Model **hardcoded to `base`** — the smallest tier, weakest for non-English.
2. **No `language` hint** — `transcribe()` auto-detects; easily misses / code-switches on
   ID content (especially when Arabic quotes are mixed in).
3. **No `initial_prompt`** — proper names / domain terms (e.g. "Ad-Duha", "Al-Insyirah",
   "Quraisy") aren't biased → often misspelled.
4. **Not configurable via env** — changing the model = editing the code.
5. `condition_on_previous_text=True` (default) — can cause repetition/hallucination during music/silence.

**Proposed**

- `WHISPER_MODEL` env (raise the default to `small`/`medium`; `large-v3` if a GPU is available).
- `WHISPER_LANGUAGE` env (e.g. `id`) → `transcribe(language=...)`, with override.
- Optional `initial_prompt` to bias the vocabulary. illustrator **already has** the title +
  description (collected in `/api/download`) — it just needs to be plumbed into `/api/transcribe` →
  `transcriber.transcribe(initial_prompt=...)` to keep proper names consistent.
- Set `condition_on_previous_text=False` (or expose it) to reduce loops/hallucination.
- **(Stretch, needs approval)** swap to `faster-whisper` (CTranslate2): much faster +
  more accurate + built-in VAD. This is a **tech change / new dependency** → owner approval first.

**Implementation notes**

- `transcriber.py` is **identical in clipper & illustrator** → change it once, mirror to the other.
- `get_model` is `lru_cache`d by name — safe for swapping the model size.
- `/api/transcribe` currently only takes `job_id`; for `initial_prompt` it needs to store/forward
  the title+description (similar to the pattern used for the LLM context).
- A bigger model = slower on CPU → a latency tradeoff.

**Acceptance**

- ID speech + proper names are transcribed accurately enough that the LLM query connects & the captions read
  correctly; minimal garbage words / repetition.

---

## AI auto-box (vision-LM crop assist)

**Status:** ✅ DONE 2026-06 (ported from clipper). In the Crop step: type what the top crop
should follow (e.g. "the speaker"), drag a single pair of range handles, hit **Generate** → the
Qwen-VL endpoint (`vision.py`, `VISION_*` env) is asked for the subject's box on frames sampled
across the range (`autobox.py`, ThreadPool×4), returning a keyframe track that drops into the top
crop box (`state.box`), fully editable. Coords 0-1000 normalized → `px=v/1000*W,H`; absent subject
→ no box (gap). Verified end-to-end (8/8 frames on a real clip). `/api/capabilities` gates the UI
when no vision model is configured. **Stretch:** auto-pick the illustration subject too, or feed the
auto-box crop into the thumbnail generator.

---

## (placeholder for future items)

When this project gets a GitHub repo, move these into Issues and keep this file as
the short backlog index (like clipper's ROADMAP.md + CLAUDE.md split).
