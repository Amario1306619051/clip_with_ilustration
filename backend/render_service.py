"""Remote ILLUSTRATOR render worker — runs on each GPU box.

Receives a render job (the source video + a JSON of render params), reconstructs
it locally and runs the SAME `renderer.render()` the main illustrator uses, then
returns the finished mp4. The box downloads the picked Pexels illustrations itself
(via `illustrator.download_pick` inside the renderer), so it needs internet +
PEXELS_API_KEY in its .env. One render at a time per box.

⚠️ This is the ILLUSTRATOR farm worker — a SEPARATE service from clipper's
render_service. Deploy it to the boxes on its own port (e.g. 8871) and point the
main illustrator's `ILLUSTRATOR_RENDER_REMOTE_URLS` at it.

Run:  uvicorn render_service:app --host 0.0.0.0 --port 8871
"""
import asyncio
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import renderer
from models import (FullscreenWindow, IllustrationPick, KeepSegment, Keyframe,
                    SfxPlacement, Word)

app = FastAPI()
_busy = threading.Lock()        # serialize renders on this box (one at a time)
_active_job = None
TEMP = renderer.OUTPUT_DIR.parent / "temp"   # same temp dir download_pick writes into
TEMP.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    return {"ok": True, "busy": _busy.locked(), "host": os.uname().nodename}


@app.get("/progress")
def progress(job_id: str):
    # The illustrator renderer has no progress callback — report 0 (the dispatcher
    # treats this as "no progress info").
    return {"pct": 0}


@app.post("/cancel")
def cancel(job_id: str = Form(...)):
    if _active_job == job_id and hasattr(renderer, "terminate_active"):
        return {"killed": bool(renderer.terminate_active())}
    return {"killed": False, "note": "not the active job / not supported"}


@app.post("/render")
async def render_ep(
    job_id: str = Form(...),
    params: str = Form(...),
    source: UploadFile = File(...),
):
    if not _busy.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="busy")
    global _active_job
    _active_job = job_id
    src_path = TEMP / f"{job_id}.mp4"
    try:
        p = json.loads(params)
        with open(src_path, "wb") as f:
            while chunk := await source.read(1 << 20):
                f.write(chunk)

        out = await asyncio.to_thread(
            renderer.render,
            job_id=job_id,
            source_path=src_path,
            title=p.get("title") or "clip",
            box=[Keyframe(**k) for k in p.get("box", [])],
            illustrations=[IllustrationPick(**c) for c in p.get("illustrations", [])],
            words=[Word(**w) for w in p.get("words", [])],
            caption_font=p.get("caption_font") or "Anton",
            caption_size=int(p.get("caption_size") or 64),
            caption_pos=p.get("caption_pos") or "middle",
            render_start=p.get("render_start"),
            render_end=p.get("render_end"),
            sfx=[SfxPlacement(**s) for s in p.get("sfx", [])] or None,
            top_eighths=float(p.get("top_eighths") or 3.0),
            fullscreen_windows=[FullscreenWindow(**f) for f in p.get("fullscreen_windows", [])] or None,
            keep_segments=[KeepSegment(**k) for k in p.get("keep_segments", [])] or None,
        )
        out_file = renderer.OUTPUT_DIR / out["filename"]
        if not out_file.exists():
            raise HTTPException(status_code=500, detail="render produced no file")
        return FileResponse(str(out_file), media_type="video/mp4", filename=out["filename"])
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"render failed: {e}")
    finally:
        try:
            src_path.unlink(missing_ok=True)
            (TEMP / f"{job_id}.ass").unlink(missing_ok=True)
            for img in TEMP.glob(f"{job_id}_ill_*"):
                img.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        _active_job = None
        _busy.release()
