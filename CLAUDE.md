# CLAUDE.md

Project context for Claude Code. Read fully before touching any file.

## Project: ILLUSTRATOR

Sibling of `../clipper`. Bikin video vertikal 9:16 dari YouTube, tapi layout-nya:
**slot ATAS = crop video (1 box), slot BAWAH = ilustrasi yang dipilih AI** (stock
photo dari Pexels) yang nyambung sama topik yang lagi diomongin. Ilustrasi ganti
tiap N detik. Caption word-by-word ala TikTok tetap ada.

**Language**: Codebase English. Komentar UI & dokumentasi user-facing pakai
bahasa Indonesia campur English (gaya owner). Jangan terjemahin string UI.

## Beda utama dari clipper

| | clipper | illustrator |
|---|---|---|
| Box | 2 box (atas+bawah), keduanya crop video | **1 box** (atas only), bawah = ilustrasi |
| Slot bawah | crop video kedua | gambar stock per N-detik window |
| AI | Whisper only | Whisper + **vLLM (topic→query)** + **Pexels search** |
| Storage | simpan source video | + download HANYA gambar yang kepilih, deduped |

Layout terkunci sama kaya clipper: top 720 (3/8), bottom 1200 (5/8), caption di y=720.

## Architecture

```
[Browser UI] ──HTTP──> [FastAPI] ──> yt-dlp / Whisper / ffmpeg
                           │           vLLM (query) / Pexels (search)
                           ▼
                     temp/{job_id}.mp4   output/{title}.mp4
                     temp/{job_id}_ill_{hash}.jpg  (picked images, deleted on cleanup)
```

4-step linear flow: **Source → Crop → Ilustrasi → Render**. No DB, no auth.
Job state = filesystem in `temp/` keyed by 12-char hex `job_id`.

## Flow (penting)

1. **Source** — URL + range → yt-dlp download + ffmpeg trim → `temp/{job_id}.mp4`.
2. **Crop** — gambar 1 crop box buat slot atas. Keyframe + fit cover/blur per-segment
   (mekanik persis clipper, tapi single box).
3. **Ilustrasi** — `/api/plan`:
   - `illustrator.segment_clip()` potong [0,duration] jadi window N-detik + ambil teks transcript per window.
   - `llm.queries_for_segments()` (vLLM batch) → 1 English search query per window.
     **Title+description video dikirim sebagai konteks global** biar tiap query
     nempel ke tema video, gak loncat ke kata literal per window (lihat Gotchas).
   - `illustrator.search_pexels()` → kandidat gambar (URL doang, di-cache per query).
   - UI render grid kandidat, user klik pilih 1 per segmen. Kandidat #1 auto-kepilih.
4. **Render** — `/api/render`:
   - Download HANYA gambar kepilih (`download_pick`, deduped by URL hash) ke temp/.
   - ffmpeg: top = crop chain; bottom = black base + overlay tiap gambar `enable=between(t,t0,t1)`; vstack; burn caption.

## Storage policy (requirement owner: "hemat storage")

- Kandidat = URL Pexels, di-stream langsung browser. **Server gak pernah simpan kandidat.**
- Cuma gambar yang **dipilih** yang di-download, dan baru pas render.
- Pakai Pexels `portrait` (~800×1200) bukan `original` — file kecil, udah deket AR slot.
- Dedup by URL: window yang pake gambar sama share 1 file di disk.
- Cleanup hapus `temp/{job_id}*` (source + semua gambar).

## Tech stack (jangan swap tanpa approval)

Backend: Python 3.10+ — `fastapi`+`uvicorn`, `yt-dlp`(+`yt-dlp-ejs`), `openai-whisper`,
`openai` (vLLM client), `requests` (Pexels), `pydantic` v2. ffmpeg via raw `subprocess`.
Frontend: Vanilla HTML/CSS/JS, no framework, no build step.
External: `ffmpeg`, `ffprobe`, JS runtime (node) on PATH untuk yt-dlp n-challenge.

LLM config = **var names sama persis kaya email_categorizer**: `VLLM_BASE_URL`,
`VLLM_MODEL`, `VLLM_API_KEY`. Plus `PEXELS_API_KEY`. Loaded via `config.py` (baca `.env`).

## File map

```
illustrator/
├── backend/
│   ├── main.py          FastAPI routes: download/transcribe/plan/search/render/cleanup
│   ├── downloader.py    yt-dlp + trim (copy dari clipper; env ILLUSTRATOR_COOKIES_BROWSER)
│   ├── transcriber.py   Whisper wrapper (copy dari clipper)
│   ├── llm.py           vLLM client — transcript snippet → English stock query (batch)
│   ├── illustrator.py   segment_clip / search_pexels / plan / download_pick
│   ├── renderer.py      ffmpeg: top crop + bottom illustration track + caption
│   ├── config.py        env loader — baca illustrator/.env (BASE_DIR = parent.parent)
│   └── models.py        Pydantic schemas
├── frontend/            index.html / style.css / app.js
├── temp/  output/
├── requirements.txt  .env.example  .gitignore
```

`config.py` ada di `backend/` biar `llm.py`/`illustrator.py` bisa `import config`
(backend/ yang on sys.path). `.env` sendiri di project root (`illustrator/.env`).

## API contract

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/download` | `{url,start,end,title,description}` | `{job_id,video_path,duration,width,height}` |
| POST | `/api/transcribe` | `{job_id}` | `{words:[{word,start,end}]}` |
| POST | `/api/plan` | `{job_id,words,segment_seconds,duration,title,description}` | `{segments:[{idx,t_start,t_end,text,query,candidates}]}` |
| POST | `/api/search` | `{query}` | `{candidates:[{id,thumb,full,alt,photographer}]}` |
| POST | `/api/render` | `{job_id,title,box,illustrations,words,caption_font,caption_size,cleanup,render_start,render_end}` | `{output_path,filename}` |
| POST | `/api/cleanup` | `{job_id}` | `{ok:true}` |
| GET | `/temp/{name}` / `/output/{name}` | — | mp4 |

- `box` = list keyframe `{t,x,y,w,h,interp,fit,gap}` (source px). Sama semantik clipper.
- `illustrations` = list `{t_start,t_end,url}` — `url` itu `full` dari kandidat kepilih.

## Gotchas

- **config.py di backend/**: `llm.py` & `illustrator.py` `import config`, jadi config.py
  harus satu folder sama mereka (backend/, yang on sys.path). `.env` di root; config
  baca via `BASE_DIR = parent.parent`.
- **Pexels key wajib** buat dapet kandidat. Tanpa key, `search_pexels` balikin `[]`
  (UI nampilin "ga ada hasil"), render tetap jalan tapi slot bawah hitam.
- **vLLM endpoint (PENTING)**: base URL = `.../models/qwen35` (BUKAN `.../model` — yang
  itu bisa nge-list model tapi completions-nya 504), model = `gb10-qwen35-122b-nvfp4-4node-100k`.
  Salah satu dari dua ini bikin tiap call gagal → semua query jatuh ke fallback (keyword
  mentah) → ilustrasi melenceng. `config.py` baca `.env` **sekali pas startup** → ganti
  `.env` WAJIB restart server.
- **Qwen3 = reasoning model → MATIKAN thinking**: kalau thinking ON, model emit `<think>`
  panjang (bermenit-menit) → nginx 504. `_chat` kirim `extra_body={"chat_template_kwargs":
  {"enable_thinking": False}}` → call jadi ~1-3s. `_strip_thinking` tetap ada (jaga-jaga).
  Client `timeout=90`, `max_tokens=2000`.
- **Cold-start 504**: request pertama setelah idle 504 (~60s) sambil model 122B di-load
  ke GPU, tapi request itu yang nge-warm-in. `_chat` retry `_MAX_ATTEMPTS=3` → biasanya
  attempt ke-2/3 kena model warm. Tetap fallback graceful kalau semua gagal — generate
  ulang pas warm = instan.
- **Topik diambil AUTO dari transcript penuh (fix 2026-06)**: dulu LLM cuma lihat snippet
  per-window → query literal & loncat-loncat. Sekarang `illustrator.plan` gabungin SEMUA
  kata jadi `full_transcript` (cap 8000 char) → `llm.queries_for_segments` sebagai konteks
  global; system prompt: infer topik dari transcript lalu **anchor tiap query ke topik itu**
  + imagery representatif/sopan buat topik abstrak/religi/historis. `title`/`description`
  (form Step 1) = hint opsional, BUKAN syarat — user gak perlu isi. Catatan: query tetap
  dibuat per-window, jadi window yang teksnya sendiri off-topic (mis. narator nyeletuk
  "ngopi di warung") bisa ikut literal — tweak via durasi segmen / "↻ cari ulang" di UI.
- **Whisper / GPU (fix 2026-06)**: `transcriber.py` auto-pakai **CUDA** kalau ada GPU,
  default model **`medium` di GPU / `base` di CPU**. Env: `WHISPER_MODEL` + `WHISPER_LANGUAGE`
  (dibaca dari `.env` via `_load_dotenv()` transcriber sendiri — gak butuh `config.py`, gak
  bergantung urutan import). `.env` set `WHISPER_LANGUAGE=id`. `condition_on_previous_text=False`
  (anti repeat/halusinasi). **Model di-load per call & VRAM dibebasin abis transcribe**
  (`del`+`empty_cache`) biar render NVENC dapet GPU penuh; OOM CUDA → fallback CPU. **GPU gotcha:**
  build `torch` HARUS cocok versi CUDA driver (driver di sini = 12.8 → butuh `cu12x`), kalau
  nggak `cuda.is_available()` diam-diam `False` → Whisper jalan di CPU (lambat). `requirements.txt`
  pin `torch==2.11.0+cu128` + index cu128 buat nyegah ini.
- **ffmpeg image inputs**: tiap window = 1 input `-loop 1 -t dur`. Base hitam pakai
  `d=dur` + `vstack shortest=1` biar output gak infinite (looped image itu infinite).
- **Box free-form + render-area guide** (sama konsep kaya clipper) — box bebas ukuran; `drawOverlay` nampilin guide: mode COVER nge-dim margin yang kepotong + outline sub-rect 3:2 yang ke-render (`coverKeepRect`), mode BLUR_PAD seluruh box ke-render (gak ada crop). (Sempet di-lock ke 3:2, tapi owner mau ukuran bebas + nunjuk blur udah nampilin box utuh — jadi di-revert ke free-form + guide.)
- **Bounds clamp**: commit (`mouseup`) lewat `clampToSource` + round-then-cap → box selalu di dalam frame. Defense-in-depth di backend: `renderer.py` `_clamp_kfs` (panggil `_probe_dims` di `render()`) cap box ke ukuran source — no-op buat box valid, tapi nyegah box off-frame bikin ffmpeg `crop` gagal. (Bug ini ketemu pas adversarial review.)
- **Caption selalu di y=720** (TOP_H) — layout selalu 2-slot, gak ada single-box mode.
- **Caption style = TikTok karaoke + bundled font** (sama kaya clipper): `assets/fonts/`
  punya Anton (default) + Bebas Neue, libass diarahin via `subtitles=...:fontsdir=`.
  `_build_ass` emit 1 Dialogue per word-slice, kata aktif di-highlight accent `#E8FF3A`
  + scale pop, pakai per-word timing dari `_group_words` (`words:[...]`). Outline 6 / shadow 3.
  Nambah font: drop .ttf, tambah ke `CAPTION_FONTS` + `<select>`, family name harus match.
- **Encoder/cookies env**: `ILLUSTRATOR_ENCODER`, `ILLUSTRATOR_COOKIES_BROWSER`
  (fallback ke `CLIPPER_*`).
- **crop w/h INIT-LOCKED — box resize butuh per-segment crop** (fix 2026-06, sama
  kaya clipper): filter `crop` ffmpeg evaluasi w/h **sekali di init** (cuma x/y yang
  gerak per-frame). Jadi box yang **ganti ukuran** antar keyframe (zoom) nyangkut di
  ukuran init (keyframe terakhir) — bug. `_crop_chain` deteksi `size_varies` lalu
  route ke `_crop_chain_segmented`: tiap segmen di-crop literal (ukuran konstan) +
  fit-nya, disambung via `overlay=enable=between(t,t0,t1)`. Ekspresi `_build_expr`
  cuma buat single-kf / ukuran-konstan (pan x/y mulus). Ukuran jadi **stepped** pas
  zoom (crop gak bisa per-frame w/h). Terverifikasi box resize jalan bener. **Jangan
  balikin ke single expression-crop buat box yang resize** — itu bug-nya.

## Dev workflow

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # isi VLLM_API_KEY + PEXELS_API_KEY
cd backend && python main.py   # → http://127.0.0.1:8000
```

Syntax check: `python -m py_compile backend/*.py config.py` + `node -c frontend/app.js`.

## Things NOT to do

- ❌ Jangan tambah frontend framework / build step.
- ❌ Jangan tambah DB / auth.
- ❌ Jangan simpan kandidat gambar server-side — cuma yang kepilih, pas render.
- ❌ Jangan ganti aspect ratio slot (3/8 + 5/8).
- ❌ Jangan generate gambar pakai image-gen tanpa approval — owner pilih stock (Pexels).
