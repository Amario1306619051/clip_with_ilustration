"""Dispatch an ILLUSTRATOR render to a remote GPU box (they run render_service.py).

Pool of box URLs from `ILLUSTRATOR_RENDER_REMOTE_URLS` (env or project-root .env,
comma-sep, e.g. "http://HOST1:8871,http://HOST2:8871,…"). NOTE: this is a DIFFERENT
service/port from clipper's render farm — the box must run the ILLUSTRATOR
render_service (it imports the illustrator renderer, which downloads the picked
Pexels images itself, so the box needs internet + PEXELS_API_KEY).

`render()` uploads the source + params, streams the finished mp4 into the local
output/, and returns the result dict — or None so the caller falls back to a LOCAL
render (box busy / down / errored / not configured). Disabled (→ always local) when
the env var is unset, so there's zero regression until it's deployed."""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("illustrator.render_remote")


class _UploadFile:
    """A read-through file wrapper that reports upload progress: requests reads it
    in chunks while POSTing the multipart body, so each read() tells us how much of
    the source has been sent → `cb(fraction 0..1)`. `len()` gives Content-Length."""
    def __init__(self, path, cb):
        self._f = open(path, "rb")
        self._total = max(1, os.path.getsize(path))
        self._sent = 0
        self._cb = cb
        self._last = -1.0

    def read(self, size=-1):
        chunk = self._f.read(size)
        self._sent += len(chunk)
        if self._cb:
            frac = min(1.0, self._sent / self._total)
            if frac - self._last >= 0.01 or frac >= 1.0:   # throttle to ~1% steps
                self._last = frac
                try:
                    self._cb(frac)
                except Exception:  # noqa: BLE001
                    pass
        return chunk

    def __len__(self):
        return self._total

    def close(self):
        try:
            self._f.close()
        except Exception:  # noqa: BLE001
            pass


def _load_urls() -> list[str]:
    raw = os.getenv("ILLUSTRATOR_RENDER_REMOTE_URLS", "")
    if not raw:
        envf = Path(__file__).resolve().parent.parent / ".env"
        if envf.exists():
            for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("ILLUSTRATOR_RENDER_REMOTE_URLS="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


_URLS = _load_urls()
_box_locks = {u: threading.Lock() for u in _URLS}
_box_host = {}                  # url → friendly hostname, filled on health checks
_box_job = {}                   # url → job_id currently rendering there (for cancel)


def enabled() -> bool:
    return bool(_URLS)


def box_count() -> int:
    return len(_URLS)


def _acquire_box() -> Optional[str]:
    """Grab the first free + healthy box (one in-flight job each). None if all busy/down."""
    for u in _URLS:
        if _box_locks[u].acquire(blocking=False):
            try:
                h = requests.get(f"{u}/health", timeout=4).json()
                if h.get("ok") and not h.get("busy"):
                    _box_host[u] = h.get("host") or u
                    return u
            except Exception:  # noqa: BLE001 — box down/unreachable
                pass
            _box_locks[u].release()
    return None


def render_until(job_id: str, source_path: Path, params: dict, out_dir: Path,
                 filename: str, progress_cb=None, upload_cb=None, source_name: Optional[str] = None,
                 always_upload: bool = False, max_seconds: int = 600) -> Optional[dict]:
    """Render on SOME GPU box, retrying: wait when all boxes are busy, move to another
    box if one dies mid-render. Returns None only if no box succeeds within
    max_seconds. The caller must NOT fall back to a local render on None — a big
    source can overwhelm the main machine (it once crashed the laptop). Better to
    surface 'farm busy, try again' than to render locally."""
    if not _URLS:
        return None
    start = time.monotonic()
    attempt = 0
    while time.monotonic() - start < max_seconds:
        attempt += 1
        try:
            res = render(job_id, source_path, params, out_dir, filename,
                         progress_cb=progress_cb, upload_cb=upload_cb,
                         source_name=source_name, always_upload=always_upload)
        except Exception as e:  # noqa: BLE001 — never let a dispatch error escape
            log.warning("render_until attempt %d error: %s", attempt, e)
            res = None
        if res:
            return res
        # No free box, or the chosen box died → brief wait, then retry (another box
        # may free up / the dead one is skipped by the next health check).
        if progress_cb:
            try:
                progress_cb(0)   # reset the UI to "rendering…/waiting for a GPU"
            except Exception:  # noqa: BLE001
                pass
        time.sleep(4)
    log.warning("render_until gave up after %ds (%d attempts) — no box succeeded", max_seconds, attempt)
    return None


def _release(u: str) -> None:
    try:
        _box_locks[u].release()
    except Exception:  # noqa: BLE001
        pass


def cancel(job_id: str) -> bool:
    """If a box is currently rendering `job_id`, tell it to kill its ffmpeg."""
    for u, jid in list(_box_job.items()):
        if jid == job_id:
            try:
                requests.post(f"{u}/cancel", data={"job_id": job_id}, timeout=6)
            except Exception as e:  # noqa: BLE001
                log.warning("cancel on %s failed: %s", u, e)
            return True
    return False


def render(job_id: str, source_path: Path, params: dict, out_dir: Path,
           filename: str, progress_cb=None, upload_cb=None, source_name: Optional[str] = None,
           always_upload: bool = False, timeout: int = 900) -> Optional[dict]:
    """Render on a remote box. Returns {output_path, filename, box} or None (→ local).
    The box downloads the picked illustration images itself (Pexels), so only the
    source video + the params JSON are uploaded. A daemon thread polls the box's
    /progress: it feeds the percent to progress_cb AND acts as a liveness check —
    if the box stops responding mid-render (crash / overload), it closes the HTTP
    session so the blocked POST aborts fast and the caller falls back to a LOCAL
    render, instead of hanging for the whole `timeout`."""
    if not _URLS:
        return None
    box = _acquire_box()
    if not box:
        return None
    stop_poll = threading.Event()
    sess = requests.Session()

    def _poll():
        fails = 0
        while not stop_poll.wait(1.5):
            try:
                pr = requests.get(f"{box}/progress", params={"job_id": job_id}, timeout=4).json()
                if progress_cb:
                    progress_cb(float(pr.get("pct", 0)))
                fails = 0
            except Exception:  # noqa: BLE001 — box unreachable / overloaded
                fails += 1
                if fails >= 6:   # ~9s unresponsive mid-render → box is dead, abort
                    log.warning("box %s went unresponsive mid-render — aborting → local fallback", box)
                    try:
                        sess.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
    threading.Thread(target=_poll, daemon=True, name=f"poll-{job_id}").start()
    fh = None
    try:
        skey = source_name or job_id
        # Skip re-uploading the source if the box already has it cached (keyed by
        # skey). `always_upload` is set for per-render SLICES — they're tiny (~a few
        # MB) so caching isn't worth it, and it guarantees the box renders THIS
        # slice, never a stale one.
        have = False
        if not always_upload:
            try:
                have = bool(requests.get(f"{box}/has_source", params={"job_id": skey},
                                         timeout=5).json().get("has"))
            except Exception:  # noqa: BLE001
                have = False
        data = {"job_id": job_id, "params": json.dumps(params), "source_name": skey}
        files = None
        if not have:
            fh = _UploadFile(source_path, upload_cb)   # reports upload % as it sends
            files = {"source": ("source.mp4", fh, "video/mp4")}
        _box_job[box] = job_id
        # (connect, read) timeout: read is the max silence before the box's response
        # (the box only replies after ffmpeg finishes, so it must exceed render time);
        # a truly dead box is caught faster by the liveness poll closing `sess`.
        r = sess.post(f"{box}/render", data=data, files=files, timeout=(10, timeout), stream=True)
        if r.status_code == 503:          # box got busy between health-check and post
            return None
        r.raise_for_status()
        out = out_dir / filename
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
        if out.stat().st_size < 1024:     # sanity: a real mp4 is never this small
            out.unlink(missing_ok=True)
            return None
        host = _box_host.get(box, box)
        log.info("remote render done on %s → %s", host, filename)
        return {"output_path": f"/output/{filename}", "filename": filename, "box": host}
    except Exception as e:  # noqa: BLE001 — any failure → caller falls back to local
        log.warning("remote render on %s failed (%s) — falling back to local", box, e)
        return None
    finally:
        stop_poll.set()
        _box_job.pop(box, None)
        if fh:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
        _release(box)
