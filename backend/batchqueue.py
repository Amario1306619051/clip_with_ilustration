"""Persistent batch queue + background worker (SQLite-backed).

Lets the user upload a JSON of clips and walk away: a single background worker
downloads each clip and predicts its crop boxes (from the per-box text prompts)
one job at a time, persisting progress to a local SQLite database
(`queue/queue.db`) so it survives a server restart. The user then opens each job
from the sidebar, fine-tunes the boxes (auto-saved back to the job), and deletes
it when done.

Storage is a real (file-based) database, NOT a JSON file:
  - `jobs`      — one row per queued clip (scalar fields).
  - `keyframes` — one row per crop-box keyframe (fully relational, no JSON blob),
                  FK to jobs(key) ON DELETE CASCADE.
No server, no extra dependency — `sqlite3` ships with Python. All access is
serialized through a module RLock and short-lived connections, so the worker
thread and API requests never collide.

Import format — keyed by video URL, tolerant of Python-dict single quotes:

  { "<video_url>": [
      {"id":.., "start":.., "end":.., "title":.., "description":..,
       "bbox_1":.., "bbox_2":..},
      ...
  ], ... }

`bbox_1` / `bbox_2` are TEXT PROMPTS for the vision auto-box (e.g.
"the live streamer ..."). Box 2 is clipper-only; illustrator ignores it.

NOTE: this is the ONE place the project keeps state on disk on purpose — the
owner asked for resumable batch progress. The rest of the app stays stateless.
"""
import ast
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import autobox
import downloader
import renderer
import transcriber
import vision
from models import Keyframe, Word

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "queue"
QUEUE_DIR.mkdir(exist_ok=True)
DB_FILE = QUEUE_DIR / "queue.db"

# illustrator = 1 (single top crop box); clipper sets this to 2 in its own copy.
# bbox_2 in the JSON is parsed but unused here.
NUM_BOXES = 1

# Whether the worker also runs the heavy transcribe + render phase (on demand,
# after the user has edited the boxes). clipper = True; illustrator keeps render
# manual (it needs the interactive Illustration step) and sets this False.
RENDER_IN_QUEUE = False
CAPTION_FONT = "Anton"   # batch render caption defaults (match the UI default)
CAPTION_SIZE = 64

# Stage concurrency (per the device). Download is I/O-bound → parallelize;
# render is heavy (Whisper + NVENC) but the owner wants two at a time. Boxing
# at 2: each job's detection pool is 4 concurrent vision calls (the endpoint's
# clean zone), but most of a boxing job is SEQUENTIAL probing — two jobs
# interleave their calls well below the 2×4 worst case in practice. Each stage
# is its own pool of worker threads that atomically CLAIM jobs from its input
# status, so no two grab the same clip.
DOWNLOAD_WORKERS = 2
BOXING_WORKERS = 2
RENDER_WORKERS = 2

# Statuses (download and boxing are now SEPARATE stages so they parallelize):
#   pending → downloading → downloaded → predicting → ready   (auto, on import)
#   ready → render_queued → rendering → done                  (on demand, after editing)
#   → error at any step
_TERMINAL = {"ready", "done", "error"}

_lock = threading.RLock()       # serializes all DB access within the process
_worker_started = False
_wake = threading.Event()       # set to nudge the worker when new work arrives

# jobs table = these scalar columns (box keyframes live in the keyframes table).
_JOB_COLS = [
    "key", "id", "url", "start", "end", "title", "description",
    "prompt1", "prompt2", "context", "auto_context", "segment_seconds", "padding",
    "step_seconds",
    "status", "message", "job_id", "video_path",
    "width", "height", "duration", "output_path", "filename",
]


# ───────────────────────── database plumbing ─────────────────────────
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")   # so ON DELETE CASCADE works
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db():
    """Short-lived connection; commits on success, always closes. (sqlite3's own
    `with conn` only manages the transaction — it does NOT close the handle.)"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _lock, _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            id TEXT, url TEXT, start TEXT, "end" TEXT, title TEXT, description TEXT,
            prompt1 TEXT, prompt2 TEXT, context TEXT, auto_context TEXT,
            segment_seconds REAL, padding REAL, step_seconds REAL,
            status TEXT, message TEXT, job_id TEXT, video_path TEXT,
            width INTEGER, height INTEGER, duration REAL,
            output_path TEXT, filename TEXT
        )''')
        # Migrations for DBs created before these columns existed.
        for ddl in ("ALTER TABLE jobs ADD COLUMN padding REAL",
                    "ALTER TABLE jobs ADD COLUMN context TEXT",
                    "ALTER TABLE jobs ADD COLUMN auto_context TEXT",
                    "ALTER TABLE jobs ADD COLUMN step_seconds REAL"):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already there
        conn.execute('''CREATE TABLE IF NOT EXISTS keyframes (
            job_key TEXT NOT NULL,
            box INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            t REAL, x REAL, y REAL, w REAL, h REAL,
            interp TEXT, fit TEXT, gap INTEGER,
            PRIMARY KEY (job_key, box, idx),
            FOREIGN KEY (job_key) REFERENCES jobs(key) ON DELETE CASCADE
        )''')


_init_db()


# ───────────────────────── row ↔ job mapping ─────────────────────────
def _read_boxes(conn, key: str) -> dict:
    boxes: dict = {1: [], 2: []}
    rows = conn.execute(
        "SELECT box, t, x, y, w, h, interp, fit, gap FROM keyframes "
        "WHERE job_key=? ORDER BY box, idx", (key,))
    for r in rows:
        boxes.setdefault(r["box"], []).append({
            "t": r["t"], "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
            "interp": r["interp"] or "hold", "fit": r["fit"] or "cover",
            "gap": bool(r["gap"]),
        })
    return boxes


def _write_box(conn, key: str, box: int, kfs) -> None:
    conn.execute("DELETE FROM keyframes WHERE job_key=? AND box=?", (key, box))
    for idx, kf in enumerate(kfs or []):
        conn.execute(
            "INSERT INTO keyframes (job_key, box, idx, t, x, y, w, h, interp, fit, gap) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, box, idx, kf.get("t", 0.0), kf.get("x", 0.0), kf.get("y", 0.0),
             kf.get("w", 0.0), kf.get("h", 0.0), kf.get("interp", "hold"),
             kf.get("fit", "cover"), 1 if kf.get("gap") else 0))


def _row_to_job(conn, row) -> dict:
    job = {c: row[c] for c in _JOB_COLS}
    boxes = _read_boxes(conn, row["key"])
    job["box1"] = boxes.get(1) or None
    job["box2"] = boxes.get(2) or None
    return job


def _insert_job(conn, job: dict) -> None:
    cols = ", ".join(f'"{c}"' for c in _JOB_COLS)
    ph = ", ".join("?" for _ in _JOB_COLS)
    conn.execute(f'INSERT INTO jobs ({cols}) VALUES ({ph})',
                 tuple(job.get(c) for c in _JOB_COLS))
    _write_box(conn, job["key"], 1, job.get("box1"))
    _write_box(conn, job["key"], 2, job.get("box2"))


def _update(key: str, **fields) -> Optional[dict]:
    with _lock, _db() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
            return None
        scal = {k: v for k, v in fields.items() if k in _JOB_COLS and k != "key"}
        if scal:
            sets = ", ".join(f'"{k}"=?' for k in scal)
            conn.execute(f'UPDATE jobs SET {sets} WHERE key=?', (*scal.values(), key))
        if "box1" in fields:
            _write_box(conn, key, 1, fields["box1"])
        if "box2" in fields:
            _write_box(conn, key, 2, fields["box2"])
        row = conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return _row_to_job(conn, row)


def _find(key: str) -> Optional[dict]:
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return _row_to_job(conn, row) if row else None


# ───────────────────────── import / parse ─────────────────────────
def _parse(content: str):
    content = (content or "").strip()
    if not content:
        raise ValueError("empty input")
    try:
        return json.loads(content)
    except Exception:
        # Tolerate the Python-dict style the user pastes (single quotes, None…).
        return ast.literal_eval(content)


def _to_hhmmss(v) -> Optional[str]:
    """Accept "HH:MM:SS" strings or plain seconds (int/float) → "HH:MM:SS"."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        s = int(round(v))
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return str(v).strip() or None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clip_to_job(url: str, clip: dict, default_context: str = "") -> dict:
    cid = str(clip.get("id") or uuid.uuid4().hex[:8])
    return {
        "key": uuid.uuid4().hex[:12],   # internal, URL-safe id used by the API
        "id": cid,                      # the user's clip id (shown in the sidebar)
        "url": url,
        "start": _to_hhmmss(clip.get("start")) or "00:00:00",
        "end": _to_hhmmss(clip.get("end")),
        "title": str(clip.get("title") or cid or "clip"),
        "description": str(clip.get("description") or ""),
        "prompt1": str(clip.get("bbox_1") or clip.get("bbox1") or ""),
        "prompt2": str(clip.get("bbox_2") or clip.get("bbox2") or ""),
        # Shared "system" context prepended to BOTH box prompts at predict time —
        # describe the layout once (per-clip "context", or the file-level
        # "_context" default) and keep bbox_1/bbox_2 as short instructions.
        "context": str(clip.get("context") or clip.get("system") or default_context or ""),
        # Filled by the worker after download: the model "studies" a frame first
        # and writes its own observation (streamer appearance, content type) —
        # merged as [observation] + [context] + [bbox prompt].
        "auto_context": None,
        # Optional per-clip illustration segment length (illustrator only) — pre-fills
        # the Illustration step so the user just picks. Ignored by clipper.
        "segment_seconds": _to_float(clip.get("segment_seconds")
                                     or clip.get("seg_seconds") or clip.get("jeda")),
        # Optional per-clip auto-box padding (fraction per side). 0 = tight box
        # hugging the subject; None → the autobox default (0.05).
        "padding": _to_float(clip.get("padding") if clip.get("padding") is not None
                             else clip.get("pad")),
        # Optional per-clip temporal precision for switches/motion (seconds).
        # 0.2 / 0.8 / 1.0 are the sensible choices; None → 0.2. Detection stays
        # on a ~1s coarse grid and only subdivides where something changes.
        "step_seconds": _to_float(clip.get("step") if clip.get("step") is not None
                                  else clip.get("precision")),
        "status": "pending",
        "message": "queued",
        "job_id": None,                 # temp/{job_id}.mp4 once downloaded
        "video_path": None,
        "width": 0, "height": 0, "duration": 0.0,
        "box1": None, "box2": None,     # predicted/edited keyframe lists
        "output_path": None, "filename": None,   # set once rendered
    }


def import_text(content: str) -> dict:
    """Parse the JSON and insert new jobs (skipping ids already queued). Returns
    {added, skipped, total}. Wakes the worker so processing starts immediately."""
    data = _parse(content)
    if not isinstance(data, dict):
        raise ValueError("top level must be an object keyed by video URL")
    # File-level shared context: a top-level "_context" key (underscore = clearly
    # not a URL) becomes the default `context` for every clip in the file.
    default_context = str(data.pop("_context", "") or "")

    added, skipped = 0, 0
    with _lock, _db() as conn:
        existing_ids = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        for url, clips in data.items():
            if not isinstance(clips, list):
                clips = [clips]
            for clip in clips:
                if not isinstance(clip, dict):
                    continue
                job = _clip_to_job(str(url), clip, default_context)
                if job["id"] in existing_ids:
                    skipped += 1
                    continue
                existing_ids.add(job["id"])
                _insert_job(conn, job)
                added += 1
        total = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    _wake.set()
    return {"added": added, "skipped": skipped, "total": total}


# ───────────────────────── queries / mutations ─────────────────────────
def list_jobs() -> list[dict]:
    """Light summary for the sidebar (no heavy keyframe arrays — just counts)."""
    with _lock, _db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY seq").fetchall()
        counts: dict = {}
        for r in conn.execute(
                "SELECT job_key, box, COUNT(*) AS c FROM keyframes GROUP BY job_key, box"):
            counts.setdefault(r["job_key"], {})[r["box"]] = r["c"]
        out = []
        for r in rows:
            c = counts.get(r["key"], {})
            out.append({
                "key": r["key"], "id": r["id"], "title": r["title"],
                "status": r["status"], "message": r["message"] or "",
                "kf1": c.get(1, 0), "kf2": c.get(2, 0),
                "ready": bool(r["job_id"]),
                "output_path": r["output_path"], "filename": r["filename"],
            })
        return out


def get_job(key: str) -> Optional[dict]:
    return _find(key)


def save_job(key: str, patch: dict) -> Optional[dict]:
    """Persist edits from the editor (title + keyframes). Only known fields."""
    allowed = {k: patch[k] for k in
               ("title", "box1", "box2", "description", "context", "prompt1", "prompt2")
               if k in patch}
    if not allowed:
        return _find(key)
    return _update(key, **allowed)


def retry_job(key: str) -> Optional[dict]:
    """Re-queue an errored job at the right phase: if it already downloaded +
    has boxes, the failure was the render → re-render; otherwise re-download/predict."""
    with _lock, _db() as conn:
        row = conn.execute("SELECT job_id FROM jobs WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        has_box = conn.execute(
            "SELECT 1 FROM keyframes WHERE job_key=? LIMIT 1", (key,)).fetchone() is not None
        job_id = row["job_id"]
    if RENDER_IN_QUEUE and job_id and has_box:
        j = _update(key, status="render_queued", message="re-queued render")
    else:
        j = _update(key, status="pending", message="re-queued")
    _wake.set()
    return j


def skip_boxing(key: str) -> Optional[dict]:
    """Pull a job OUT of the boxing queue: a `downloaded` job (waiting for the
    AI boxing stage) goes straight to `ready` with no boxes, so the user can
    open it immediately and draw the boxes manually instead of waiting. Done
    under the module lock, so the boxing pool can't claim it concurrently
    (its claim also runs under the lock). Returns the job, or None when the
    job isn't currently waiting to be boxed."""
    with _lock, _db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE key=?", (key,)).fetchone()
        if not row or row["status"] != "downloaded":
            return None
        conn.execute(
            "UPDATE jobs SET status='ready', "
            "message='boxing skipped — draw the boxes manually' WHERE key=?", (key,))
    return _find(key)


def queue_render(key: str) -> Optional[dict]:
    """Mark a downloaded job for the (background) render phase. Boxes used are
    whatever is currently saved (the frontend auto-saves edits before calling)."""
    if not RENDER_IN_QUEUE:
        return None
    j = _find(key)
    if not j or not j.get("job_id"):
        return None
    r = _update(key, status="render_queued", message="render queued")
    _wake.set()
    return r


def queue_render_all_ready() -> int:
    """Queue every edited-and-ready job for render. Returns how many were queued."""
    if not RENDER_IN_QUEUE:
        return 0
    with _lock, _db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='render_queued', message='render queued' "
            "WHERE status='ready' AND job_id IS NOT NULL")
        n = cur.rowcount
    if n:
        _wake.set()
    return n


def delete_job(key: str, cleanup: bool = True) -> bool:
    with _lock, _db() as conn:
        row = conn.execute("SELECT job_id FROM jobs WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        job_id = row["job_id"]
        # keyframes go too via ON DELETE CASCADE (foreign_keys pragma is on)
        conn.execute("DELETE FROM jobs WHERE key=?", (key,))
    if cleanup and job_id:
        try:
            downloader.cleanup_job(job_id)
        except Exception:  # noqa: BLE001 — best-effort temp cleanup
            pass
    return True


# ───────────────────────── background worker ─────────────────────────
_OBSERVE_QUESTION = (
    "Look at this video frame from a reaction stream. In ONE short sentence, "
    "describe the live streamer in the webcam panel (gender, skin tone, clothing, "
    "accessories). Then in ONE short sentence, describe what kind of content the "
    "other panel shows. Be factual and concise. Do NOT mention left/right/top/"
    "bottom positions. Answer with the two sentences only."
)


def _observe_clip(src, dur: float) -> Optional[str]:
    """Let the model study the clip before boxing: describe a mid-clip frame
    (streamer appearance + content type). Falls back to an early frame. Returns
    the observation text or None — boxing proceeds without it on failure."""
    for t in (max(0.5, dur / 2.0), 1.0):
        b64 = autobox._extract_frame_b64(src, t)
        if not b64:
            continue
        obs = vision.describe(b64, _OBSERVE_QUESTION)
        if obs:
            # keep it one line and bounded — it's a prompt prefix, not an essay
            return " ".join(obs.split())[:400]
    return None


def _norm_step(step) -> float:
    """Per-clip temporal precision (s); 0.4 default, clamped to [0.2, 3]."""
    return 0.4 if step is None else min(3.0, max(0.2, float(step)))


def _predict_box(src, prompt: str, dur: float, padding=None, role=None,
                 step=None, segments=None) -> Optional[list]:
    """Auto-box keyframes for one prompt over the whole clip, or None when the
    prompt is empty / the vision endpoint is off / nothing is detected. `role`
    ("streamer"/"content") tells fullscreen layout segments what this box is:
    the box whose subject fills the screen → whole frame, the other → gap.
    `step` = temporal precision in seconds (default 0.2); `segments` = the
    clip's shared layout timeline so both boxes agree on it."""
    if not (prompt or "").strip() or not vision.enabled():
        return None
    pad = 0.05 if padding is None else max(0.0, float(padding))
    out = autobox.predict_track(src, prompt, 0.0, dur, padding=pad, lock_size=True,
                                role=role, step_seconds=_norm_step(step),
                                segments=segments)
    return out.get("keyframes") or None


def _download_one(job: dict) -> None:
    """Stage 1 (download pool): yt-dlp the clip, then hand off to the boxing
    stage via the `downloaded` status. Status is already `downloading` (set
    atomically by the claim). A clip downloaded on a previous run skips straight
    through."""
    key = job["key"]
    if not job.get("job_id"):
        try:
            res = downloader.download(job["url"], job.get("start") or "00:00:00",
                                      job.get("end"), job.get("title") or "clip")
        except Exception as e:  # noqa: BLE001
            log.warning("queue download failed (%s): %s", job["id"], e)
            _update(key, status="error", message=f"download failed: {e}")
            return
        _update(key, job_id=res["job_id"], video_path=res["video_path"],
                width=res["width"], height=res["height"], duration=res["duration"])
    _update(key, status="downloaded", message="downloaded — waiting to box…")


def _predict_boxes(job: dict) -> None:
    """Stage 2 (boxing pool, single worker): auto-box the downloaded clip. Status
    is already `predicting` (set atomically by the claim)."""
    key = job["key"]
    try:
        src = downloader.get_source_path(job["job_id"])
        dur = job.get("duration") or 0.0
        pad = job.get("padding")   # per-clip from the JSON; None → autobox default
        ctx = (job.get("context") or "").strip()

        # The model studies the video FIRST: one observation of a mid-clip frame
        # (streamer appearance + content type, deliberately WITHOUT sides — the
        # geometric {side} probe is more reliable for that). Generated once per
        # job, persisted, and prepended to both box prompts.
        obs = (job.get("auto_context") or "").strip()
        if not obs and vision.enabled():
            obs = _observe_clip(src, dur) or ""
            if obs:
                _update(key, auto_context=obs)

        def merged(p: str) -> str:
            # [model observation] + [shared context] + [per-box instruction].
            p = (p or "").strip()
            if not p:
                return p
            return " ".join(s for s in (obs, ctx, p) if s)

        notes = []
        stp = job.get("step_seconds")
        p1m, p2m = merged(job.get("prompt1", "")), merged(job.get("prompt2", ""))
        # ONE shared layout timeline per clip — both boxes must agree on what
        # each stretch is (independent probing can label the same stretch
        # fullscreen-webcam for box1 but fullscreen-content for box2 → both
        # boxes real at once, the reel shows the same frame twice)
        segs = None
        w_, h_ = job.get("width") or 0, job.get("height") or 0
        if (w_ and h_ and vision.enabled()
                and any(t in (p1m + p2m) for t in ("{side}", "{other_side}", "{layout}"))):
            segs = autobox.detect_layout_segments(src, 0.0, dur, w_, h_,
                                                  min_gap=_norm_step(stp))
            if segs:
                notes.append("layout: " + " · ".join(
                    f"{s['layout']}{'(' + s['side'] + ')' if s['side'] else ''} "
                    f"{s['t0']:.1f}–{s['t1']:.1f}s" for s in segs))
        # box1 = the streamer (AREA 1), box2 = the content (AREA 2) — the same
        # convention the {side}/{layout} placeholders and the geometric side
        # probe are built on
        box1 = _predict_box(src, p1m, dur, padding=pad,
                            role="streamer", step=stp, segments=segs)
        if job.get("prompt1") and not box1:
            notes.append("box1: nothing detected")
        w_, h_ = job.get("width") or 0, job.get("height") or 0
        if box1 and w_ and h_:
            box1 = autobox.debounce_track(box1, w=w_, h=h_)  # drop sub-second box flickers
        box2 = None
        if NUM_BOXES >= 2:
            box2 = _predict_box(src, p2m, dur, padding=pad,
                                role="content", step=stp, segments=segs)
            if job.get("prompt2") and not box2:
                notes.append("box2: nothing detected")
            if box1 and box2:
                # both boxes gap at once = a fullscreen blip the probes couldn't
                # see → one box must carry the full frame there (else the reel
                # goes black); a classification probe decides which
                w_, h_ = job.get("width") or 0, job.get("height") or 0
                if w_ and h_:
                    box1, box2 = autobox.merge_double_gaps(
                        box1, box2, w_, h_,
                        classify=lambda t: autobox.classify_fullscreen_owner(src, t, w_, h_))
                    # the two split panels often overlap a little on the seam
                    # (streamer box too wide / content box too far in) → snap
                    # both edges to a shared divider so the crops don't double up
                    box1, box2 = autobox.resolve_split_overlap(box1, box2, w_, h_)
                    # …and an UNDER-sized content box can't be fixed by a snap:
                    # in a true split the content area is the geometric
                    # complement of the streamer panel (seam → frame edge)
                    box2 = autobox.expand_content_to_seam(box1, box2, w_, h_)
    except Exception as e:  # noqa: BLE001 — download already succeeded; boxes are best-effort
        log.warning("queue predict failed (%s): %s", job["id"], e)
        _update(key, status="ready", box1=None, box2=None,
                message=f"downloaded — box predict failed: {e} (draw manually)")
        return

    msg = "ready — open to fine-tune"
    if notes:
        msg += " · " + ", ".join(notes)
    _update(key, status="ready", box1=box1, box2=box2, message=msg)


def _render_one(job: dict) -> None:
    """Heavy phase (clipper): transcribe + render with the saved/edited boxes →
    a finished mp4 in output/. Runs in the SAME single worker thread, so only one
    render ever runs at a time (CPU/GPU-safe)."""
    key = job["key"]
    if not job.get("job_id"):
        _update(key, status="error", message="not downloaded yet — can't render")
        return
    b1 = job.get("box1") or []
    b2 = job.get("box2") or []
    if not b1 and not b2:
        _update(key, status="error", message="no boxes to render — open and draw/predict first")
        return
    try:
        src = downloader.get_source_path(job["job_id"])
    except Exception as e:  # noqa: BLE001
        _update(key, status="error", message=f"source missing: {e}")
        return

    _update(key, status="rendering", message="transcribing (Whisper)…")
    try:
        words = [Word(**w) for w in transcriber.transcribe(src)]
    except Exception as e:  # noqa: BLE001
        log.warning("queue transcribe failed (%s): %s", job["id"], e)
        _update(key, status="error", message=f"transcribe failed: {e}")
        return

    _update(key, status="rendering", message="rendering + caption…")
    try:
        kf1 = [Keyframe(**k) for k in b1] or None
        kf2 = [Keyframe(**k) for k in b2] or None
        out = renderer.render(
            job_id=job["job_id"], source_path=src, title=job.get("title") or "clip",
            box1=kf1, box2=kf2, words=words,
            caption_font=CAPTION_FONT, caption_size=CAPTION_SIZE,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("queue render failed (%s): %s", job["id"], e)
        _update(key, status="error", message=f"render failed: {e}")
        return

    _update(key, status="done", output_path=out["output_path"], filename=out["filename"],
            message=f"done — {out['filename']}")


def _claim_next(from_status: str, to_status: str, message: str):
    """Atomically claim the oldest job in `from_status`: flip it to `to_status`
    and return it, or None when the stage's inbox is empty. All DB access is
    serialized by `_lock`, so two pool threads can never grab the same row."""
    with _lock, _db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY seq LIMIT 1",
            (from_status,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status=?, message=? WHERE key=?",
                     (to_status, message, row["key"]))
        job = _row_to_job(conn, row)
        job["status"] = to_status
        return job


def _reset_interrupted() -> None:
    """A job left mid-flight by a restart resumes from the start of its phase.
    Mid-`predicting` jobs already have the clip on disk → back to `downloaded`
    (straight to the boxing inbox), not all the way to `pending`."""
    with _lock, _db() as conn:
        conn.execute(
            "UPDATE jobs SET status='pending', message='re-queued after restart' "
            "WHERE status='downloading' OR (status='predicting' AND job_id IS NULL)")
        conn.execute(
            "UPDATE jobs SET status='downloaded', message='re-queued boxing after restart' "
            "WHERE status='predicting' AND job_id IS NOT NULL")
        conn.execute(
            "UPDATE jobs SET status='render_queued', message='re-queued render after restart' "
            "WHERE status='rendering'")


def _migrate_json_if_any() -> None:
    """One-time: if an old queue/queue.json exists and the DB has no jobs yet,
    import it so nobody loses mid-batch progress from the pre-SQLite version."""
    old = QUEUE_DIR / "queue.json"
    if not old.exists():
        return
    with _lock, _db() as conn:
        if conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]:
            return
        try:
            jobs = json.loads(old.read_text(encoding="utf-8")) or []
        except Exception as e:  # noqa: BLE001
            log.warning("could not migrate queue.json: %s", e)
            return
        for job in jobs:
            job.setdefault("key", uuid.uuid4().hex[:12])
            try:
                _insert_job(conn, job)
            except Exception as e:  # noqa: BLE001 — skip a bad row, keep the rest
                log.warning("skip migrating one job: %s", e)
    try:
        old.rename(old.with_suffix(".json.migrated"))
    except OSError:
        pass


def _stage_loop(name: str, from_status: str, to_status: str,
                claim_msg: str, work) -> None:
    """One pool thread: claim jobs from this stage's inbox and run `work` on
    each. Sleeps on `_wake` (or a 5s poll) when the inbox is empty. The wake
    Event is shared by every pool — a spurious wake just re-checks and sleeps."""
    while True:
        job = _claim_next(from_status, to_status, claim_msg)
        if job is None:
            # The Event is shared by every pool thread. Let all waiters see a
            # set() before anyone clears it (the 50ms grace), then clear so the
            # next wait blocks again; the 5s poll bounds any missed wake.
            _wake.wait(timeout=5)
            time.sleep(0.05)
            _wake.clear()
            continue
        try:
            work(job)
        except Exception as e:  # noqa: BLE001 — never let a pool thread die
            log.exception("queue %s worker crashed on %s: %s", name, job.get("id"), e)
            _update(job["key"], status="error", message=f"{name} error: {e}")
        _wake.set()   # hand-off: nudge downstream stages immediately


def start_worker() -> None:
    """Idempotent — call once at app startup. Spawns the per-stage pools:
    DOWNLOAD_WORKERS yt-dlp threads, BOXING_WORKERS vision threads, RENDER_WORKERS transcribe+render threads."""
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
        _init_db()
        _migrate_json_if_any()
        _reset_interrupted()
    stages = (
        [("download", "pending", "downloading", "downloading clip…",
          _download_one)] * DOWNLOAD_WORKERS
        + [("boxing", "downloaded", "predicting", "predicting boxes (AI)…",
            _predict_boxes)] * BOXING_WORKERS
        + ([("render", "render_queued", "rendering", "transcribing (Whisper)…",
             _render_one)] * RENDER_WORKERS if RENDER_IN_QUEUE else [])
    )
    for i, (name, frm, to, msg, work) in enumerate(stages):
        threading.Thread(target=_stage_loop, args=(name, frm, to, msg, work),
                         daemon=True, name=f"queue-{name}-{i}").start()
    with _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    log.info("queue pools started (download×%d boxing×%d render×%d, %d job(s) in db)",
             DOWNLOAD_WORKERS, BOXING_WORKERS,
             RENDER_WORKERS if RENDER_IN_QUEUE else 0, n)
