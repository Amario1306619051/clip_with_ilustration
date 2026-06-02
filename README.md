# ILLUSTRATOR

Bikin video vertikal 9:16 (TikTok/Shorts/Reels) dari YouTube — **slot atas = potongan
video, slot bawah = ilustrasi (stock photo) yang dipilihin AI biar nyambung sama yang
lagi diomongin**. Ganti tiap N detik. Caption word-by-word otomatis.

Adik dari `../clipper`. Bedanya: clipper 2 box video, ini 1 box video + 1 track ilustrasi.

## Flow

1. **Source** — paste URL YouTube + range waktu → download & trim.
2. **Crop** — gambar 1 box buat slot atas. Bisa keyframe (pan) + pilih fit cover/blur per-segment.
3. **Ilustrasi** — set durasi per gambar (N detik) → "Generate". AI (Whisper + LLM) baca
   transcript tiap window, nyariin gambar nyambung di Pexels, kasih beberapa kandidat.
   Klik buat milih satu per segmen (kandidat pertama auto-kepilih).
4. **Render** — compose + burn caption → 1 file mp4 di `output/`.

## Hemat storage

Kandidat gambar cuma URL — di-load langsung dari Pexels di browser, **gak disimpen di server**.
Yang di-download cuma gambar yang kamu pilih, pas render, ukuran kecil (`portrait` ~800×1200),
dan di-dedup (window yang pake gambar sama share 1 file). Semua kehapus pas "Done · clean up".

---

# Cara jalanin (lengkap, step-by-step)

## 0. Prasyarat — install dulu di komputer

Tool ini butuh 3 program di luar Python. Cek dulu udah ada belum:

```bash
ffmpeg -version
ffprobe -version
node -v
python3 --version    # butuh 3.10+
```

Kalau ada yang `command not found`, install dulu:

**Linux (Debian/Ubuntu)**
```bash
sudo apt update
sudo apt install -y ffmpeg nodejs python3 python3-venv
```

**macOS (pakai Homebrew)**
```bash
brew install ffmpeg node python
```

**Windows**
- ffmpeg: download dari https://www.gyan.dev/ffmpeg/builds/ → ekstrak → tambahin folder `bin/` ke PATH.
- node: download dari https://nodejs.org → install (next-next-finish).
- python: download dari https://python.org → install, centang **"Add Python to PATH"**.

> `node` dibutuhin yt-dlp buat mecahin "n-challenge"-nya YouTube. Tanpa node, download bisa cuma dapet gambar storyboard.

## 1. Bikin virtual env + install dependency Python

Dari dalam folder `illustrator/`:

```bash
cd illustrator

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt
```

> Pertama kali bakal agak lama — `openai-whisper` narik PyTorch yang gede.

## 2. Dapetin Pexels API key (gratis)

Ini buat nyari gambar ilustrasi.

1. Buka https://www.pexels.com/api/ → klik **"Get Started"**.
2. **Login / daftar** akun Pexels (bisa pakai Google/email).
3. Isi form singkat:
   - *What are you building?* → isi bebas, misal **"Personal video tool"**.
   - *Description* → misal **"App bikin video pendek + ilustrasi otomatis"**.
   - Centang agree to terms.
4. Klik **Generate API Key** → key langsung muncul di dashboard (https://www.pexels.com/api/new/).
   Bentuknya string panjang, contoh `563492ad6f917000010000019xxxxxxxxxxxxx`.
5. Copy key-nya (nanti ditempel di step 4).

> Gratis: **200 request/jam**, **20.000/bulan** — lebih dari cukup. Wajib kasih attribution
> ke Pexels kalau video-nya dipublish (nama fotografer udah ke-track otomatis).

## 3. Siapin akses LLM (vLLM internal)

Pakai endpoint vLLM internal yang sama kaya `email_categorizer` — buat nerjemahin
topik tiap segmen jadi keyword pencarian gambar. Yang kamu butuh cuma **`VLLM_API_KEY`**-nya
(base URL + model udah ada default-nya). Ambil dari `email_categorizer/.env` punya kamu.

## 4. Isi file `.env`

```bash
cp .env.example .env
```

Buka `.env`, isi 2 baris ini (sisanya boleh dibiarin default):

```ini
VLLM_API_KEY=<key vLLM dari email_categorizer>
PEXELS_API_KEY=<key Pexels dari step 2>
```

## 5. Jalanin servernya

```bash
cd backend
python main.py
```

Kalau sukses bakal muncul `Uvicorn running on http://127.0.0.1:8000`.
Buka **http://127.0.0.1:8000** di browser.

> Hentiin server: `Ctrl + C`. Tiap mau jalanin lagi, aktifin venv dulu
> (`source venv/bin/activate`), terus `cd backend && python main.py`.

## 6. Cara pakai di browser (4 langkah)

1. **Source** — paste URL YouTube, isi Start/End (kosongin End = sampe abis), klik **Download & Trim**.
2. **Crop** — klik pill **"Crop box"** (atau pencet `1`) buat ngaktifin canvas → drag di video
   buat bikin box (slot atas). Mau pan? scrub ke waktu lain, drag lagi = keyframe baru.
   Tiap segment row bisa di-toggle **HOLD↔PAN** dan **COVER↔BLUR**. Klik **Transcribe & Continue**.
3. **Ilustrasi** — set **durasi per ilustrasi** (detik), klik **Generate ilustrasi**.
   Tiap segmen muncul barisan kandidat gambar → klik buat milih (kandidat #1 auto-kepilih).
   Keyword kurang pas? edit di kotak query → **cari ulang**. Klik **Continue**.
4. **Render** — atur caption font/size (atau render range kalau mau dipotong lagi) →
   **Render Final (with Caption)**. Hasilnya muncul + tombol download. Klik
   **Done · clean up source** kalau udah, biar temp kehapus.

Output ada di `illustrator/output/*.mp4`.

---

## Troubleshooting

| Masalah | Solusi |
|---|---|
| Download gagal / "Sign in to confirm you're not a bot" | Login YouTube di Chrome/Firefox sekali, atau set `ILLUSTRATOR_COOKIES_BROWSER=firefox`, atau export `cookies.txt` (Netscape) ke `illustrator/cookies.txt`. |
| Slot bawah hitam pas render | `PEXELS_API_KEY` belum diisi / salah. Render tetap jalan, cuma tanpa ilustrasi. |
| Kandidat gambar kosong di Step 3 | Cek key Pexels, atau edit keyword-nya terus "cari ulang". |
| Transcribe lama banget | Normal pas pertama — Whisper download + load model. Berikutnya cepet. |
| Caption Indonesia kurang akurat | Ganti model di `backend/transcriber.py`: `get_model("base")` → `"small"`/`"medium"`. |
| Render lambat | Kalau ada GPU NVIDIA, NVENC kepake otomatis. Paksa CPU: `ILLUSTRATOR_ENCODER=libx264`. |
| Port 8000 kepake | Edit port di baris terakhir `backend/main.py`. |

## Catatan teknis

- Tanpa `PEXELS_API_KEY`, slot bawah bakal kosong (hitam) — render tetap jalan.
- LLM Qwen kadang lemot/`<think>` — ada fallback keyword, jadi gak pernah macet total.
- Whisper default model `base` (mediocre buat Indonesia) — ganti di `backend/transcriber.py`.
- GPU NVENC kepake otomatis kalo ada; paksa CPU: `ILLUSTRATOR_ENCODER=libx264`.
- Detail arsitektur lengkap: lihat [CLAUDE.md](CLAUDE.md).
