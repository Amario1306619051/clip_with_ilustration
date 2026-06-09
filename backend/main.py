import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autobox
import batchqueue as batch_queue
import downloader
import illustrator
import renderer
import soundboard
import thumbnail
import transcriber
import vision
from models import (
    DownloadRequest, DownloadResponse,
    TranscribeRequest, TranscribeResponse,
    PlanRequest, PlanResponse,
    SearchRequest, SearchResponse,
    RenderRequest, RenderResponse,
    CleanupRequest,
    AutoBoxRequest, AutoBoxResponse,
    ThumbnailTextRequest, ThumbnailTextResponse,
    QueueImportRequest, QueueJobPatch,
    SoundPatch,
)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ILLUSTRATOR")

# Start the batch-queue worker: downloads + auto-boxes queued clips one at a time
# in the background, resuming from the SQLite db queue/queue.db across restarts.
batch_queue.start_worker()


@app.post("/api/download", response_model=DownloadResponse)
def api_download(req: DownloadRequest):
    try:
        return downloader.download(req.url, req.start, req.end, req.title)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/transcribe", response_model=TranscribeResponse)
def api_transcribe(req: TranscribeRequest):
    try:
        src = downloader.get_source_path(req.job_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        words = transcriber.transcribe(src)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"words": words}


@app.post("/api/plan", response_model=PlanResponse)
def api_plan(req: PlanRequest):
    """Segment the clip into N-second windows, derive a topic query per window
    (vLLM), and fetch candidate stock images per window (URLs only)."""
    try:
        words = [w.model_dump() for w in req.words]
        segments = illustrator.plan(
            words, req.duration, req.segment_seconds,
            title=req.title, description=req.description,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"segments": segments}


@app.post("/api/search", response_model=SearchResponse)
def api_search(req: SearchRequest):
    """Re-search one segment with a user-edited query."""
    try:
        candidates = illustrator.search_pexels(req.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"candidates": candidates}


@app.post("/api/render", response_model=RenderResponse)
def api_render(req: RenderRequest):
    if not req.box:
        raise HTTPException(status_code=400, detail="box (top crop) required with >= 1 keyframe")
    try:
        src = downloader.get_source_path(req.job_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        result = renderer.render(
            job_id=req.job_id,
            source_path=src,
            title=req.title,
            box=req.box,
            illustrations=req.illustrations,
            words=req.words,
            caption_font=req.caption_font,
            caption_size=req.caption_size,
            render_start=req.render_start,
            render_end=req.render_end,
            sfx=req.sfx,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if req.cleanup:
        downloader.cleanup_job(req.job_id)
    return result


@app.post("/api/autobox", response_model=AutoBoxResponse)
def api_autobox(req: AutoBoxRequest):
    """AI auto-box: sample frames over [t_start,t_end] and ask the vision model for
    the prompted subject's bounding box on each → a keyframe track for the top crop
    box that the user can then edit."""
    if not vision.enabled():
        raise HTTPException(status_code=400,
                            detail="vision model not configured (set VISION_BASE_URL / VISION_MODEL in .env)")
    if not (req.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt required")
    try:
        src = downloader.get_source_path(req.job_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        out = autobox.predict_track(
            src, req.prompt, req.t_start, req.t_end,
            step_seconds=req.step_seconds, padding=req.padding, smooth=req.smooth,
            lock_size=req.lock_size,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    kfs = out["keyframes"]
    cap_note = (f" (range long — sampled every {out['step']}s, capped at {out['sampled']} frames)"
                if out.get("capped") else "")
    msg = (f"Detected '{req.prompt.strip()}' in {out['detected']}/{out['sampled']} frames"
           f" (every {out['step']}s).{cap_note}"
           if kfs else
           f"No '{req.prompt.strip()}' found in {out['sampled']} frames — try a different prompt or range.")
    return {"keyframes": kfs, "sampled": out["sampled"], "detected": out["detected"], "message": msg}


@app.post("/api/thumbnail-text", response_model=ThumbnailTextResponse)
def api_thumbnail_text(req: ThumbnailTextRequest):
    """Eye-catching thumbnail headline suggestions (text only) from the LLM. The
    frame + compositing + PNG export are all done client-side on a canvas."""
    if not thumbnail.enabled():
        raise HTTPException(status_code=400,
                            detail="text model not configured (set VLLM_BASE_URL / VLLM_MODEL in .env)")
    try:
        titles = thumbnail.generate_titles(req.context, req.n, req.language)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"titles": titles}


@app.get("/api/capabilities")
def api_capabilities():
    """Lets the frontend hide/disable the AI auto-box (vision) and the thumbnail
    headline ideas (text model) when the endpoints aren't configured."""
    return {"vision": vision.enabled(), "thumbnail": thumbnail.enabled()}


@app.post("/api/cleanup")
def api_cleanup(req: CleanupRequest):
    downloader.cleanup_job(req.job_id)
    return {"ok": True}


# ───────────────────────── batch queue ─────────────────────────
@app.post("/api/queue/import")
def api_queue_import(req: QueueImportRequest):
    """Upload a JSON of clips ({url: [{id,start,end,title,description,bbox_1,bbox_2}]}).
    Each clip becomes a queued job the background worker downloads + auto-boxes
    (bbox_1 = the single crop box prompt; bbox_2 is ignored in illustrator)."""
    try:
        return batch_queue.import_text(req.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not parse JSON: {e}")


@app.get("/api/queue")
def api_queue_list():
    """Sidebar summary of every job (status, kf counts) — polled by the frontend."""
    return {"jobs": batch_queue.list_jobs()}


@app.get("/api/queue/{key}")
def api_queue_get(key: str):
    """Full job (incl. predicted/edited keyframes) to load into the editor."""
    j = batch_queue.get_job(key)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@app.post("/api/queue/{key}/save")
def api_queue_save(key: str, patch: QueueJobPatch):
    """Auto-save edits (title + keyframes) back to the job so progress survives."""
    j = batch_queue.save_job(key, patch.model_dump(exclude_none=True))
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@app.post("/api/queue/{key}/retry")
def api_queue_retry(key: str):
    j = batch_queue.retry_job(key)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@app.delete("/api/queue/{key}")
def api_queue_delete(key: str):
    batch_queue.delete_job(key)
    return {"ok": True}


# ───────────────────────── soundboard ─────────────────────────
@app.get("/api/soundboard")
def api_sb_list():
    """The persistent SFX library (id, name, duration, default volume)."""
    return {"sounds": soundboard.list_sounds()}


@app.post("/api/soundboard")
async def api_sb_add(request: Request, name: str = "", filename: str = ""):
    """Import an audio file. The file bytes are the raw request body (no
    multipart dependency); `name`/`filename` come from the query string."""
    data = await request.body()
    try:
        return soundboard.add_sound(name, filename, data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/soundboard/{sid}")
def api_sb_update(sid: str, patch: SoundPatch):
    s = soundboard.update_sound(sid, patch.model_dump(exclude_none=True))
    if not s:
        raise HTTPException(status_code=404, detail="sound not found")
    return s


@app.delete("/api/soundboard/{sid}")
def api_sb_delete(sid: str):
    soundboard.delete_sound(sid)
    return {"ok": True}


@app.get("/api/soundboard/{sid}/audio")
def api_sb_audio(sid: str):
    """Serve the audio file — used for in-browser preview playback."""
    p = soundboard.path_for(sid)
    if not p:
        raise HTTPException(status_code=404, detail="sound not found")
    return FileResponse(p, media_type=soundboard.media_type(sid))


@app.get("/temp/{name}")
def serve_temp(name: str):
    p = TEMP_DIR / name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="video/mp4")


@app.get("/output/{name}")
def serve_output(name: str):
    p = OUTPUT_DIR / name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="video/mp4", filename=name)


# Static mount MUST be last — it's a catch-all.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
