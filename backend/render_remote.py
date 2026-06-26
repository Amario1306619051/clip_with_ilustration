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
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("illustrator.render_remote")


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
           filename: str, timeout: int = 1800) -> Optional[dict]:
    """Render on a remote box. Returns {output_path, filename, box} or None (→ local).
    The box downloads the picked illustration images itself (Pexels), so only the
    source video + the params JSON are uploaded."""
    if not _URLS:
        return None
    box = _acquire_box()
    if not box:
        return None
    fh = None
    try:
        fh = open(source_path, "rb")
        files = {"source": ("source.mp4", fh, "video/mp4")}
        data = {"job_id": job_id, "params": json.dumps(params)}
        _box_job[box] = job_id
        r = requests.post(f"{box}/render", data=data, files=files, timeout=timeout, stream=True)
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
        _box_job.pop(box, None)
        if fh:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        _release(box)
