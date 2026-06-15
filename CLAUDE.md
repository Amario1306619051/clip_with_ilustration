# CLAUDE.md

Project context for Claude Code. Read fully before touching any file.

## Project: ILLUSTRATOR

Sibling of `../clipper`. Builds 9:16 vertical videos from YouTube, but with this
layout: **TOP slot = cropped video (1 box), BOTTOM slot = AI-picked illustration**
(stock photo from Pexels) that matches the topic being discussed. The illustration
changes every N seconds. TikTok-style word-by-word captions are still present.

**Language**: English everywhere. The codebase, code comments, documentation, and
UI strings are all in English.

## Key differences from clipper

| | clipper | illustrator |
|---|---|---|
| Box | 2 boxes (top+bottom), both crop video | **1 box** (top only), bottom = illustration |
| Bottom slot | second video crop | stock image per N-second window |
| AI | Whisper only | Whisper + **vLLM (topic→query)** + **Pexels search** |
| Storage | stores source video | + downloads ONLY the picked images, deduped |

Layout is locked the same as clipper: top 720 (3/8), bottom 1200 (5/8), caption at y=720.

## Architecture

```
[Browser UI] ──HTTP──> [FastAPI] ──> yt-dlp / Whisper / ffmpeg
                           │           vLLM (query) / Pexels (search)
                           ▼
                     temp/{job_id}.mp4   output/{title}.mp4
                     temp/{job_id}_ill_{hash}.jpg  (picked images, deleted on cleanup)
```

6-step linear flow: **Source → Crop → Illustration → Render → Thumbnail → Sound**, plus a **batch-queue sidebar**. The **Sound** step (`soundboard.py` + the `sfx-*` frontend block) is identical to clipper's "Soundboard / SFX" (see clipper CLAUDE.md): a persistent SFX library (own SQLite db + audio files in `soundboard/`, raw-body upload, no multipart) + per-clip placements (one-shot / range+loop, each with a volume) carried in `RenderRequest.sfx` and mixed by the renderer's `_audio_inputs_and_graph` — SFX inputs come after the source + image inputs (`first_sfx_index = 1 + len(img_inputs)`). No auth — and stateless except the batch queue, which persists to a local **SQLite** DB `queue/queue.db` (the one deliberate on-disk state; owner asked for resumable batch progress, in a real DB not a JSON file).

**Batch queue** (`batchqueue.py` + the queue-* frontend block) is clipper's batch queue with two illustrator-specific differences (see clipper CLAUDE.md "Batch queue" for the full spec):
- `NUM_BOXES = 1` — auto-boxes the single top crop from `bbox_1` (`bbox_2` is parsed but unused).
- **No render phase** — illustrator does NOT set `RENDER_IN_QUEUE`, so the worker only does download + predict; there are no `/api/queue/{key}/render` routes. Render stays **manual** because it needs the interactive Illustration step (pick stock photos). The JSON's optional **`segment_seconds`** (aliases `seg_seconds`/`jeda`) is stored per job and **pre-fills the Illustration step's duration** on open, so the user just picks images then renders via Step 3-4.

**Editing features shared with clipper (2026-06; see clipper CLAUDE.md for the full spec):** (1) **Cut lanes / draggable segment bars** in the Crop step (`#cuts-1`, `renderCutLane`) — every crop segment is a draggable bar (body=move, edge=resize, double-click gap=restore) + a "Delete crop here" button; backed by `gap` keyframes (note: `drawPreview` was fixed to render a gap kf as black). (2) **Global undo/redo** (`hist` module, ↶ ↷ + Ctrl+Z/Y) over `state.box` + `state.sfx`, reset on job switch. (3) **Rooms** — identical backend (`rooms` table + `jobs.room_id`, `list_rooms`/`create_room`/`delete_room`, `import_text(room_id=…)`) + a single-box sidebar room bar.

Upload a JSON `{url:[{id,start,end,title,description,bbox_1,segment_seconds}]}` → worker downloads + auto-boxes each clip, persists to the SQLite DB `queue/queue.db` (jobs + a relational keyframes table, no JSON blob); open a ready job to fine-tune the crop (auto-saved), do illustrations + render manually, delete when done. **⚠️ The module is `batchqueue.py`, not `queue.py`** — a `queue.py` on `sys.path` shadows the stdlib `queue` urllib3/yt-dlp need and crashes boot. Also `sqlite3`'s `with conn:` only manages the transaction, not the handle — use the `_db()` context manager (commits + closes).
Job state = filesystem in `temp/` keyed by 12-char hex `job_id`.

## Flow (important)

1. **Source** — URL + range → yt-dlp download + ffmpeg trim → `temp/{job_id}.mp4`.
2. **Crop** — draw 1 crop box for the top slot. Keyframe + per-segment cover/blur fit
   (same mechanics as clipper, but a single box).
3. **Illustration** — `/api/plan`:
   - `illustrator.segment_clip()` splits [0,duration] into N-second windows + pulls the transcript text per window.
   - `llm.queries_for_segments()` (vLLM batch) → 1 English search query per window.
     **The video's title+description are sent as global context** so each query
     sticks to the video's theme rather than jumping to literal per-window words (see Gotchas).
   - `illustrator.search_pexels()` → image candidates (URLs only, cached per query).
   - The UI renders a candidate grid; the user clicks to pick 1 per segment. Candidate #1 is auto-selected.
4. **Render** — `/api/render`:
   - Download ONLY the picked images (`download_pick`, deduped by URL hash) into temp/.
   - ffmpeg: top = crop chain; bottom = black base + overlay of each image `enable=between(t,t0,t1)`; vstack; burn caption.

## Storage policy (owner requirement: "save storage")

- Candidates = Pexels URLs, streamed directly to the browser. **The server never stores candidates.**
- Only the **picked** images are downloaded, and only at render time.
- Use Pexels `portrait` (~800×1200) instead of `original` — small files, already close to the slot AR.
- Dedup by URL: windows that use the same image share 1 file on disk.
- Cleanup deletes `temp/{job_id}*` (source + all images).

## Tech stack (don't swap without approval)

Backend: Python 3.10+ — `fastapi`+`uvicorn`, `yt-dlp`(+`yt-dlp-ejs`), `openai-whisper`,
`openai` (vLLM client), `requests` (Pexels), `pydantic` v2. ffmpeg via raw `subprocess`.
Frontend: Vanilla HTML/CSS/JS, no framework, no build step.
External: `ffmpeg`, `ffprobe`, JS runtime (node) on PATH for the yt-dlp n-challenge.

LLM config = **variable names exactly the same as email_categorizer**: `VLLM_BASE_URL`,
`VLLM_MODEL`, `VLLM_API_KEY`. Plus `PEXELS_API_KEY`. Loaded via `config.py` (reads `.env`).

## File map

```
illustrator/
├── backend/
│   ├── main.py          FastAPI routes: download/transcribe/plan/search/render/cleanup
│   ├── downloader.py    yt-dlp + trim (copied from clipper; env ILLUSTRATOR_COOKIES_BROWSER)
│   ├── transcriber.py   Whisper wrapper (copied from clipper)
│   ├── llm.py           vLLM client — transcript snippet → English stock query (batch)
│   ├── illustrator.py   segment_clip / search_pexels / plan / download_pick
│   ├── renderer.py      ffmpeg: top crop + bottom illustration track + caption
│   ├── vision.py        Vision-LM client: prompt → bbox (AI auto-box for top crop)
│   ├── autobox.py       Track predictor: sample frames over a range → keyframes
│   ├── thumbnail.py     Text-LLM client: context → eye-catching headline ideas; self-loads .env
│   ├── batchqueue.py    Persistent batch queue + background worker (JSON import → download + auto-box). NUM_BOXES=1
│   ├── soundboard.py    Persistent SFX library (SQLite + audio files); renderer mixes placements into the audio
│   ├── config.py        env loader — reads illustrator/.env (BASE_DIR = parent.parent)
│   └── models.py        Pydantic schemas
├── frontend/            index.html / style.css / app.js
├── temp/  output/
├── requirements.txt  .env.example  .gitignore
```

`config.py` lives in `backend/` so `llm.py`/`illustrator.py` can `import config`
(backend/ is on sys.path). `.env` itself sits in the project root (`illustrator/.env`).

## API contract

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/download` | `{url,start,end,title,description}` | `{job_id,video_path,duration,width,height}` |
| POST | `/api/transcribe` | `{job_id}` | `{words:[{word,start,end}]}` |
| POST | `/api/plan` | `{job_id,words,segment_seconds,duration,title,description}` | `{segments:[{idx,t_start,t_end,text,query,candidates}]}` |
| POST | `/api/search` | `{query}` | `{candidates:[{id,thumb,full,alt,photographer}]}` |
| POST | `/api/render` | `{job_id,title,box,illustrations,words,caption_font,caption_size,cleanup,render_start,render_end}` | `{output_path,filename}` |
| POST | `/api/autobox` | `{job_id,prompt,t_start,t_end,box,step_seconds}` | `{keyframes:[Keyframe],sampled,detected,message}` |
| POST | `/api/thumbnail-text` | `{context,n,language}` | `{titles:[str]}` (eye-catching headline ideas) |
| POST/GET/DELETE | `/api/queue` `/api/queue/import` `/api/queue/{key}` `/api/queue/{key}/save` `/api/queue/{key}/retry` | batch queue | identical to clipper; `bbox_1` = the single crop prompt (`bbox_2` parsed but unused) |
| GET/POST/DELETE | `/api/soundboard` `/api/soundboard/{id}` `/api/soundboard/{id}/audio` | SFX library | identical to clipper; import = raw body + `?name=&filename=`. `RenderRequest.sfx` mixes placements into the audio |
| GET | `/api/capabilities` | — | `{vision: bool, thumbnail: bool}` (auto-box / headline ideas available) |
| POST | `/api/cleanup` | `{job_id}` | `{ok:true}` |
| GET | `/temp/{name}` / `/output/{name}` | — | mp4 |

- `box` = list of keyframes `{t,x,y,w,h,interp,fit,gap}` (source px). Same semantics as clipper.
- `illustrations` = list of `{t_start,t_end,url}` — `url` is the `full` URL of the picked candidate.

## Gotchas

- **config.py in backend/**: `llm.py` & `illustrator.py` `import config`, so config.py
  must be in the same folder as them (backend/, which is on sys.path). `.env` is in the root;
  config reads it via `BASE_DIR = parent.parent`.
- **Pexels key is required** to get candidates. Without a key, `search_pexels` returns `[]`
  (the UI shows "no results"), render still runs but the bottom slot is black.
- **vLLM endpoint (IMPORTANT)**: base URL = `.../models/qwen35` (NOT `.../model` — that one
  can list models but its completions 504), model = `gb10-qwen35-122b-nvfp4-4node-100k`.
  Either of these being wrong makes every call fail → all queries fall back to the fallback
  (raw keywords) → illustrations go off-topic. `config.py` reads `.env` **once at startup** → changing
  `.env` REQUIRES a server restart.
- **Qwen3 = reasoning model → TURN OFF thinking**: with thinking ON, the model emits a long
  `<think>` (minutes long) → nginx 504. `_chat` sends `extra_body={"chat_template_kwargs":
  {"enable_thinking": False}}` → calls become ~1-3s. `_strip_thinking` is still there (just in case).
  Client `timeout=90`, `max_tokens=2000`.
- **Cold-start 504**: the first request after idle 504s (~60s) while the 122B model loads
  onto the GPU, but that request is what warms it up. `_chat` retries `_MAX_ATTEMPTS=3` → usually
  the 2nd/3rd attempt hits the warm model. Still fails gracefully if everything fails — regenerating
  once warm is instant.
- **Topic is pulled AUTOMATICALLY from the full transcript (fix 2026-06)**: previously the LLM only saw
  the per-window snippet → literal, jumpy queries. Now `illustrator.plan` joins ALL words into a
  `full_transcript` (capped at 8000 chars) → passed to `llm.queries_for_segments` as global context;
  system prompt: infer the topic from the transcript, then **anchor each query to that topic**
  + representative/respectful imagery for abstract/religious/historical topics. `title`/`description`
  (Step 1 form) = optional hints, NOT requirements — the user doesn't need to fill them in. Note: queries
  are still generated per-window, so a window whose own text is off-topic (e.g. the narrator quipping
  "having coffee at a stall") can still go literal — tweak via segment duration / "↻ search again" in the UI.
- **Whisper / GPU (fix 2026-06)**: `transcriber.py` automatically uses **CUDA** when a GPU is present,
  default model **`medium` on GPU / `base` on CPU**. Env: `WHISPER_MODEL` + `WHISPER_LANGUAGE`
  (read from `.env` via the transcriber's own `_load_dotenv()` — doesn't need `config.py`, doesn't
  depend on import order). `.env` sets `WHISPER_LANGUAGE=id`. `condition_on_previous_text=False`
  (anti repeat/hallucination). **The model is loaded per call & VRAM is freed after transcribing**
  (`del`+`empty_cache`) so the render's NVENC gets the full GPU; CUDA OOM → falls back to CPU. **GPU gotcha:**
  the `torch` build MUST match the CUDA driver version (driver here = 12.8 → needs `cu12x`); otherwise
  `cuda.is_available()` silently returns `False` → Whisper runs on CPU (slow). `requirements.txt`
  pins `torch==2.11.0+cu128` + the cu128 index to prevent this.
- **ffmpeg image inputs**: each window = 1 input `-loop 1 -t dur`. The black base uses
  `d=dur` + `vstack shortest=1` so the output isn't infinite (a looped image is infinite).
- **Free-form box + render-area guide** (same concept as clipper) — the box is any size; `drawOverlay` shows a guide: COVER mode dims the cropped-off margins + outlines the rendered 3:2 sub-rect (`coverKeepRect`), BLUR_PAD mode renders the whole box (no crop). (It was briefly locked to 3:2, but the owner wanted free sizing + pointed out that blur already shows the whole box — so it was reverted to free-form + guide.)
- **AI auto-box (vision-LM)** — `vision.py` + `autobox.py` (identical to clipper's; see clipper CLAUDE.md "AI auto-box" for the full spec). In the Crop step: type what the crop should follow, drag a single pair of range handles, **Generate** → `/api/autobox` samples frames over the range, asks the Qwen-VL endpoint (`VISION_*`, shared with browser_agent) for the subject's box on each, and drops a keyframe track into the **top crop box** (`state.box`) — editable afterwards. Coords are **0-1000 normalized → `px=v/1000*W,H`**; regex parse (output non-deterministic); largest-area box = subject; **absent subject → no box** (a run of misses becomes a `gap`/black keyframe). **Stable size (default `lock_size=True`)**: a two-pass step locks one box size for the whole range (percentile) and only pans the center → no zoom jitter; toggle off in the UI for adaptive size. Optional — `/api/capabilities` returns `{vision:false}` and the UI disables it when `VISION_*` is unset.
- **Windowed shot-director + panning + diarization (2026-06)** — `vision.py` / `autobox.py` / `diarize.py` (identical to clipper's; see clipper CLAUDE.md "Windowed shot-director + panning + diarization" for the FULL spec). Three additive toggle-gated layers: **Phase 1** dynamic-segment → smooth size-locked PANNING track (`moving=True`, no longer a black gap) — survives to the renderer via guards in debounce/hold-override/the three split-geometry snaps + `_crop_chain_segmented`'s same-w/h RUN grouping; persisted in `keyframes.moving` (+ALTER), green TRACKED chip. **Phase 2** `vision.director()` + `autobox.run_director()` window the clip (frames + Whisper transcript + speaker hint → a JSON `{layout,box1_present,box1_side,box1_desc,subject_moving,confidence}` verdict reconciled into segments) — toggle `director`, transcript write-through cached in `jobs.transcript`. **Phase 3** `diarize.py` (pyannote 4.x, lazy, optional, `HF_TOKEN`-gated, `_preload_cuda_libs` for torchcodec) supplies the dominant-speaker hint. **Illustrator specifics:** single box → `role='streamer'`, `NUM_BOXES=1`, `RENDER_IN_QUEUE=False` (transcript cache benefits clipper's queue render, not illustrator's manual render); `box1_desc` applies to the single box; UI has the **Director**/**Diarize** checkboxes + capability gating. All default OFF; with everything off boxing is byte-for-byte the old behaviour.
- **Thumbnail generator (Step 5)** — `thumbnail.py` + the thumb-* frontend block (identical to clipper's; see clipper CLAUDE.md "Thumbnail generator" for the full spec). A standalone 9:16 cover maker, **almost entirely client-side**: pick a frame on its own `#thumb-video` scrubber, the text LLM (`VLLM_*`, the same Qwen3 used for stock queries — NOT the vision endpoint) proposes eye-catching headlines via `/api/thumbnail-text`, editable or type your own, then export a 1080×1920 PNG with `canvas.toBlob` (nothing written server-side). Headlines are in the **content's language** (Indonesian content → Indonesian headlines) — the English-everywhere rule is about code/UI, not generated output. `thumbnail.py` self-loads `.env` and defaults `VLLM_*` to the internal endpoint; `enable_thinking=False` + cold-start retries. `/api/capabilities` returns `{thumbnail:false}` (disabling only the **Generate ideas** button; manual typing + export still work) when `VLLM_*` is cleared.
- **Bounds clamp**: commit (`mouseup`) goes through `clampToSource` + round-then-cap → the box is always inside the frame. Defense-in-depth on the backend: `renderer.py` `_clamp_kfs` (called by `_probe_dims` in `render()`) caps the box to the source size — a no-op for valid boxes, but it prevents an off-frame box from making ffmpeg `crop` fail. (This bug was found during adversarial review.)
- **Caption is always at y=720** (TOP_H) — the layout is always 2-slot, there is no single-box mode.
- **Caption style = TikTok karaoke + bundled font** (same as clipper): `assets/fonts/`
  has Anton (default) + Bebas Neue, libass is pointed at it via `subtitles=...:fontsdir=`.
  `_build_ass` emits 1 Dialogue per word-slice, the active word is highlighted with the accent `#E8FF3A`
  + a scale pop, using per-word timing from `_group_words` (`words:[...]`). Outline 6 / shadow 3.
  To add a font: drop in the .ttf, add it to `CAPTION_FONTS` + the `<select>`, the family name must match.
- **Encoder/cookies env**: `ILLUSTRATOR_ENCODER`, `ILLUSTRATOR_COOKIES_BROWSER`
  (fall back to `CLIPPER_*`).
- **crop w/h INIT-LOCKED — box resize needs per-segment crop** (fix 2026-06, same
  as clipper): the ffmpeg `crop` filter evaluates w/h **once at init** (only x/y move
  per-frame). So a box that **changes size** between keyframes (zoom) gets stuck at the
  init size (the last keyframe) — a bug. `_crop_chain` detects `size_varies` and then
  routes to `_crop_chain_segmented`: each segment is cropped literally (constant size) +
  fitted, then stitched together via `overlay=enable=between(t,t0,t1)`. The `_build_expr`
  expression is only for single-kf / constant-size (smooth x/y pan). Sizing becomes **stepped**
  on zoom (crop can't do per-frame w/h). Verified that box resize works correctly. **Don't
  revert to a single expression-crop for a box that resizes** — that's the bug.

## Dev workflow

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in VLLM_API_KEY + PEXELS_API_KEY
cd backend && python main.py   # → http://127.0.0.1:8000
```

Syntax check: `python -m py_compile backend/*.py config.py` + `node -c frontend/app.js`.

## Things NOT to do

- ❌ Don't add a frontend framework / build step.
- ❌ Don't add a DB / auth.
- ❌ Don't store image candidates server-side — only the picked ones, at render time.
- ❌ Don't change the slot aspect ratios (3/8 + 5/8).
- ❌ Don't generate images with image-gen without approval — the owner picks stock (Pexels).
