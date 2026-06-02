import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

import downloader
import illustrator
import renderer
import transcriber
from models import (
    DownloadRequest, DownloadResponse,
    TranscribeRequest, TranscribeResponse,
    PlanRequest, PlanResponse,
    SearchRequest, SearchResponse,
    RenderRequest, RenderResponse,
    CleanupRequest,
)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ILLUSTRATOR")


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
        segments = illustrator.plan(words, req.duration, req.segment_seconds)
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
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if req.cleanup:
        downloader.cleanup_job(req.job_id)
    return result


@app.post("/api/cleanup")
def api_cleanup(req: CleanupRequest):
    downloader.cleanup_job(req.job_id)
    return {"ok": True}


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
