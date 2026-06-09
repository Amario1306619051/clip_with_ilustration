"""Soundboard: a persistent library of imported sound-effect files.

Audio files live in `soundboard/` on disk; their metadata (name, duration,
default volume) is in a local SQLite database `soundboard/soundboard.db`. So the
library survives a restart and can be listed / previewed / deleted. Placing a
sound onto a clip (one-shot at a timestamp, or a layer over a range, each with a
volume) is part of the render request — `renderer.py` mixes them into the audio.

No server, no extra dependency: `sqlite3` ships with Python, and uploads are
read as the raw request body (no `python-multipart` needed). Same SQLite
plumbing style as `batchqueue.py`.
"""
import logging
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SB_DIR = BASE_DIR / "soundboard"
SB_DIR.mkdir(exist_ok=True)
DB_FILE = SB_DIR / "soundboard.db"

# Accepted audio extensions → MIME type for serving back to the browser.
_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".opus": "audio/opus", ".webm": "audio/webm",
}

_lock = threading.RLock()


# ───────────────────────── database plumbing ─────────────────────────
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _lock, _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS sounds (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            name TEXT,
            ext TEXT,
            duration REAL,
            volume REAL,
            created REAL
        )''')


_init_db()


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True)
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:  # noqa: BLE001 — duration is informational only
        return 0.0


def _row_to_sound(r) -> dict:
    return {"id": r["id"], "name": r["name"], "ext": r["ext"],
            "duration": r["duration"], "volume": r["volume"]}


# ───────────────────────── public API ─────────────────────────
def list_sounds() -> list[dict]:
    with _lock, _db() as conn:
        return [_row_to_sound(r) for r in conn.execute(
            "SELECT * FROM sounds ORDER BY seq")]


def get(sid: str) -> Optional[dict]:
    with _lock, _db() as conn:
        r = conn.execute("SELECT * FROM sounds WHERE id=?", (sid,)).fetchone()
        return _row_to_sound(r) if r else None


def path_for(sid: str) -> Optional[Path]:
    s = get(sid)
    if not s:
        return None
    p = SB_DIR / f"{sid}{s['ext']}"
    return p if p.exists() else None


def media_type(sid: str) -> str:
    s = get(sid)
    return _MIME.get((s or {}).get("ext", ""), "application/octet-stream")


def add_sound(name: str, filename: str, data: bytes) -> dict:
    """Save an uploaded audio file to the library. ext is taken from filename
    (must be a known audio type). Returns the stored sound record."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in (filename or "") else ""
    if ext not in _MIME:
        raise ValueError(
            f"unsupported audio type '{ext or filename}'. Allowed: {', '.join(sorted(_MIME))}")
    if not data:
        raise ValueError("empty file")
    sid = uuid.uuid4().hex[:12]
    dest = SB_DIR / f"{sid}{ext}"
    dest.write_bytes(data)
    dur = _probe_duration(dest)
    disp = (name or "").strip() or Path(filename).stem or "sound"
    with _lock, _db() as conn:
        conn.execute(
            "INSERT INTO sounds (id, name, ext, duration, volume, created) "
            "VALUES (?,?,?,?,?,?)",
            (sid, disp, ext, dur, 1.0, time.time()))
    return {"id": sid, "name": disp, "ext": ext, "duration": dur, "volume": 1.0}


def update_sound(sid: str, patch: dict) -> Optional[dict]:
    """Rename / change default volume."""
    allowed = {k: patch[k] for k in ("name", "volume") if k in patch}
    if not allowed:
        return get(sid)
    with _lock, _db() as conn:
        if not conn.execute("SELECT 1 FROM sounds WHERE id=?", (sid,)).fetchone():
            return None
        sets = ", ".join(f"{k}=?" for k in allowed)
        conn.execute(f"UPDATE sounds SET {sets} WHERE id=?", (*allowed.values(), sid))
    return get(sid)


def delete_sound(sid: str) -> bool:
    with _lock, _db() as conn:
        r = conn.execute("SELECT ext FROM sounds WHERE id=?", (sid,)).fetchone()
        if not r:
            return False
        conn.execute("DELETE FROM sounds WHERE id=?", (sid,))
        ext = r["ext"]
    f = SB_DIR / f"{sid}{ext}"
    try:
        f.unlink()
    except OSError:
        pass
    return True
