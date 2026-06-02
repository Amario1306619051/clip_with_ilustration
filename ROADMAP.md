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

## Transcription accuracy (Whisper) — "voicenya kurang akurat"

**Status:** planned (shared issue dengan clipper — `transcriber.py` identik)

**Why**

Whisper sering salah dengar, terutama **bahasa Indonesia** + **nama/istilah khusus** (nama
orang, tempat, istilah agama). Di illustrator efeknya **berlipat**: transcript salah →
`llm.queries_for_segments` dapet konteks salah → query Pexels meleset → ilustrasi nyasar.
Jadi akurasi voice itu fondasi seluruh pipeline ilustrasi, bukan cuma caption.

**Root cause** (`backend/transcriber.py`)

1. Model **hardcoded `base`** — tier terkecil, paling lemah buat non-English.
2. **Tanpa `language` hint** — `transcribe()` auto-detect; gampang meleset / code-switch di
   konten ID (apalagi yang nyelipin kutipan Arab).
3. **Tanpa `initial_prompt`** — nama diri / istilah domain (mis. "Ad-Duha", "Al-Insyirah",
   "Quraisy") gak ke-bias → sering salah eja.
4. **Gak bisa diatur via env** — ganti model = edit kode.
5. `condition_on_previous_text=True` (default) — bisa repetition/halusinasi pas musik/hening.

**Proposed**

- `WHISPER_MODEL` env (default naikin ke `small`/`medium`; `large-v3` kalau ada GPU).
- `WHISPER_LANGUAGE` env (mis. `id`) → `transcribe(language=...)`, dengan override.
- `initial_prompt` opsional buat nge-bias kosakata. illustrator **udah punya** title +
  description (dikumpulin di `/api/download`) — tinggal di-plumb ke `/api/transcribe` →
  `transcriber.transcribe(initial_prompt=...)` biar nama diri konsisten.
- Set `condition_on_previous_text=False` (atau expose) buat ngurangin loop/halusinasi.
- **(Stretch, butuh approval)** swap ke `faster-whisper` (CTranslate2): jauh lebih cepat +
  akurat + VAD bawaan. Ini **ganti tech / dep baru** → approval owner dulu.

**Implementation notes**

- `transcriber.py` **identik di clipper & illustrator** → ubah sekali, mirror ke satu lagi.
- `get_model` di-`lru_cache` by name — aman buat swap ukuran model.
- `/api/transcribe` sekarang cuma terima `job_id`; buat `initial_prompt` perlu nyimpen/teruskan
  title+description (mirip pola yang dipakai buat konteks LLM).
- Model lebih gede = lebih lambat di CPU → tradeoff latency.

**Acceptance**

- Speech ID + nama diri ke-transcribe cukup bener sampai query LLM nyambung & caption kebaca
  benar; minim kata ngawur / repetisi.

---

## (placeholder for future items)

When this project gets a GitHub repo, move these into Issues and keep this file as
the short backlog index (like clipper's ROADMAP.md + CLAUDE.md split).
