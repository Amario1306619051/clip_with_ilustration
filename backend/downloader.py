import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# Optional cookies file in project root (Netscape format export).
COOKIES_FILE = TEMP_DIR.parent / "cookies.txt"


def _detect_browser_for_cookies() -> Optional[str]:
    """Return browser name yt-dlp can extract cookies from.
    Override with env var CLIPPER_COOKIES_BROWSER=chrome|firefox|brave|edge|chromium.
    """
    env = os.environ.get("ILLUSTRATOR_COOKIES_BROWSER") or os.environ.get("CLIPPER_COOKIES_BROWSER")
    if env:
        return env.strip().lower()
    home = Path.home()
    for name, path in [
        ("firefox",  home / ".mozilla/firefox"),
        ("chrome",   home / ".config/google-chrome"),
        ("chromium", home / ".config/chromium"),
        ("brave",    home / ".config/BraveSoftware/Brave-Browser"),
        ("edge",     home / ".config/microsoft-edge"),
    ]:
        if path.exists():
            return name
    return None


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def _ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    stream = data.get("streams", [{}])[0]
    fmt = data.get("format", {})
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0)
    return {"width": width, "height": height, "duration": duration}


def download(url: str, start: str, end: Optional[str], title: str) -> dict:
    """Download YouTube clip and trim to [start, end] window.
    Returns dict with job_id, video_path, duration, width, height.
    """
    job_id = _new_job_id()
    raw_path = TEMP_DIR / f"{job_id}_raw.mp4"
    final_path = TEMP_DIR / f"{job_id}.mp4"

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(raw_path),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    # Enable a JS runtime for YouTube's n-challenge — without this, yt-dlp
    # gets only the storyboard images back. Prefer node, fall back to deno/bun
    # if installed. yt-dlp also needs `yt-dlp-ejs` (in requirements.txt).
    runtimes = {}
    for rt in ("node", "deno", "bun"):
        if shutil.which(rt):
            runtimes[rt] = {}
    if runtimes:
        ydl_opts["js_runtimes"] = runtimes
    # Prefer explicit cookies file if user dropped one in project root.
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)
    else:
        browser = _detect_browser_for_cookies()
        if browser:
            ydl_opts["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in to confirm" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
            cookies_source = (
                f"cookies.txt at {COOKIES_FILE}" if COOKIES_FILE.exists()
                else f"cookies from browser '{ydl_opts.get('cookiesfrombrowser', ('none',))[0]}'"
            )
            raise RuntimeError(
                f"YouTube anti-bot. Tried {cookies_source}. "
                f"Fix options:\n"
                f"  1. Log in to YouTube in Chrome/Firefox once, then re-try.\n"
                f"  2. Set ILLUSTRATOR_COOKIES_BROWSER=firefox (or chrome/brave/edge) before starting the server.\n"
                f"  3. Export cookies.txt (Netscape format, via a browser extension) to {COOKIES_FILE}\n"
                f"Original error: {msg}"
            )
        raise

    if not raw_path.exists():
        # yt-dlp may have written a different extension despite outtmpl
        candidates = list(TEMP_DIR.glob(f"{job_id}_raw.*"))
        if not candidates:
            raise RuntimeError("yt-dlp failed to produce a file")
        raw_path = candidates[0]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", start]
    if end:
        cmd += ["-to", end]
    cmd += ["-i", str(raw_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            str(final_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed: {proc.stderr}")

    try:
        raw_path.unlink()
    except OSError:
        pass

    info = _ffprobe(final_path)
    return {
        "job_id": job_id,
        "video_path": f"/temp/{final_path.name}",
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
    }


def get_source_path(job_id: str) -> Path:
    p = TEMP_DIR / f"{job_id}.mp4"
    if not p.exists():
        raise FileNotFoundError(f"source for job {job_id} not found")
    return p


def cleanup_job(job_id: str) -> None:
    for p in TEMP_DIR.glob(f"{job_id}*"):
        try:
            p.unlink()
        except OSError:
            pass
