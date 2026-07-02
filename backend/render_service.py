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
from models import (CaptionPosRange, FullscreenWindow, IllustrationPick,
                    KeepSegment, Keyframe, SfxPlacement, Sticker, TextOverlay, Word)

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
    # The renderer publishes ffmpeg progress under the render's progress_id (= job_id
    # here) as {phase,pct}. The dispatcher polls this and relays the pct to the UI.
    p = renderer.get_progress(job_id)
    return {"pct": (p or {}).get("pct", 0) if isinstance(p, dict) else (p or 0)}


@app.post("/cancel")
def cancel(job_id: str = Form(...)):
    if _active_job == job_id and hasattr(renderer, "terminate_active"):
        return {"killed": bool(renderer.terminate_active())}
    return {"killed": False, "note": "not the active job / not supported"}


@app.get("/has_source")
def has_source(job_id: str):
    """Does this box already have the source cached? Lets the dispatcher skip
    re-uploading the (large) source for repeat renders of the same clip — e.g.
    every sub-clip of one video shares the same job_id/source."""
    p = TEMP / f"{job_id}.mp4"
    return {"has": p.exists() and p.stat().st_size > 1_000_000}


@app.post("/render")
async def render_ep(
    job_id: str = Form(...),
    params: str = Form(...),
    source: UploadFile = File(None),
    source_name: str = Form(None),
):
    if not _busy.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="busy")
    global _active_job
    _active_job = job_id
    # `source_name` lets the dispatcher send a small per-render SLICE of the source
    # (named distinctly) so it doesn't clobber / get confused with the full-source
    # cache ({job_id}.mp4). Falls back to job_id for the whole-source path.
    src_path = TEMP / f"{(source_name or job_id)}.mp4"
    try:
        p = json.loads(params)
        if source is not None:
            with open(src_path, "wb") as f:
                while chunk := await source.read(1 << 20):
                    f.write(chunk)
        elif not src_path.exists():
            raise HTTPException(status_code=409, detail="source not cached — resend with the source file")

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
            caption_pos_ranges=[CaptionPosRange(**r) for r in p.get("caption_pos_ranges", [])] or None,
            text_overlays=[TextOverlay(**t) for t in p.get("text_overlays", [])] or None,
            stickers=[Sticker(**s) for s in p.get("stickers", [])] or None,
            render_start=p.get("render_start"),
            render_end=p.get("render_end"),
            sfx=[SfxPlacement(**s) for s in p.get("sfx", [])] or None,
            top_eighths=float(p.get("top_eighths") or 3.0),
            fullscreen_windows=[FullscreenWindow(**f) for f in p.get("fullscreen_windows", [])] or None,
            keep_segments=[KeepSegment(**k) for k in p.get("keep_segments", [])] or None,
            progress_id=job_id,   # publish ffmpeg progress under job_id → /progress polls it
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
            # KEEP the source cached (src_path) so repeat renders of the same clip
            # (e.g. other sub-clips) skip the big re-upload. Only drop the per-render
            # scratch (ASS + downloaded illustration images).
            (TEMP / f"{job_id}.ass").unlink(missing_ok=True)
            for img in TEMP.glob(f"{job_id}_ill_*"):
                img.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        _active_job = None
        _busy.release()
