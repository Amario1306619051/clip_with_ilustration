import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autobox
import batchqueue as batch_queue
import diarize
import downloader
import illustrator
import imagesources
import render_remote
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
    QueueImportRequest, QueueJobPatch, RoomCreate,
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
            t_start=req.t_start, t_end=req.t_end, source=req.source,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"segments": segments}


@app.post("/api/search", response_model=SearchResponse)
def api_search(req: SearchRequest):
    """Re-search one segment with a user-edited query, on a chosen image source
    (Pexels / Openverse / Wikimedia / Unsplash / Pixabay)."""
    try:
        candidates = imagesources.search(req.query, source=req.source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"candidates": candidates}


def _make_source_slice(src, rs: float, re_: float):
    """Cut a small [rs, re] slice of the source (stream-copy, ~1s, ~a few MB) so the
    farm uploads that instead of the whole file — the network to the boxes is slow
    (~5 MB/s), and a full-length source is ~800 MB. Returns (slice_path, shift) where
    `shift` (= rs, keyframe-aligned actual start is handled by the same seek the full
    render used) is subtracted from every timestamp. None on failure → upload full."""
    import subprocess
    import tempfile
    dur = re_ - rs
    if dur <= 0:
        return None
    fd, path = tempfile.mkstemp(prefix="illslice_", suffix=".mp4", dir=str(TEMP_DIR))
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{rs:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-c", "copy", "-movflags", "+faststart", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or os.path.getsize(path) < 1024:
            raise RuntimeError(p.stderr or "empty slice")
    except Exception as exc:  # noqa: BLE001 — fall back to full-source upload
        logging.getLogger("illustrator").warning("source slice failed (%s) — uploading full source", exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


def _shift_time(v, by):
    return None if v is None else max(0.0, float(v) - by)


def _sliced_params(req: RenderRequest, by: float) -> dict:
    """req params with every timestamp shifted back by `by` (the slice start), and
    render_start/end cleared — the slice is 0-based, so the box renders the whole of
    it. The keyframe-seek error at the slice head matches the old full-source path."""
    p = req.model_dump(exclude={"job_id", "cleanup", "progress_id"})
    for k in p.get("box", []) or []:
        k["t"] = _shift_time(k.get("t", 0.0), by)
    for it in p.get("illustrations", []) or []:
        it["t_start"] = _shift_time(it.get("t_start"), by); it["t_end"] = _shift_time(it.get("t_end"), by)
    for w in p.get("words", []) or []:
        w["start"] = _shift_time(w.get("start"), by); w["end"] = _shift_time(w.get("end"), by)
    for r in p.get("caption_pos_ranges", []) or []:
        r["start"] = _shift_time(r.get("start"), by); r["end"] = _shift_time(r.get("end"), by)
    for o in p.get("text_overlays", []) or []:
        o["start"] = _shift_time(o.get("start"), by); o["end"] = _shift_time(o.get("end"), by)
    for s in p.get("stickers", []) or []:
        s["start"] = _shift_time(s.get("start"), by); s["end"] = _shift_time(s.get("end"), by)
    for kseg in p.get("keep_segments", []) or []:
        kseg["start"] = _shift_time(kseg.get("start"), by); kseg["end"] = _shift_time(kseg.get("end"), by)
    for fw in p.get("fullscreen_windows", []) or []:
        fw["t_start"] = _shift_time(fw.get("t_start"), by); fw["t_end"] = _shift_time(fw.get("t_end"), by)
    for sx in p.get("sfx", []) or []:
        sx["t"] = _shift_time(sx.get("t"), by); sx["t_end"] = _shift_time(sx.get("t_end"), by)
    p["render_start"] = None
    p["render_end"] = None
    return p


@app.post("/api/render", response_model=RenderResponse)
def api_render(req: RenderRequest):
    if not req.box:
        raise HTTPException(status_code=400, detail="box (top crop) required with >= 1 keyframe")
    try:
        src = downloader.get_source_path(req.job_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # RENDER ON THE GPU FARM. When the farm is configured we NEVER fall back to a
    # local render — a big source (a full-length video) rendered on the main machine
    # once crashed the user's laptop. So: if the farm is on, keep trying boxes (wait
    # when busy, skip dead ones) and, if none succeed, return 503 asking to retry —
    # do NOT render locally. Local render happens only when the farm is unconfigured.
    result = None
    if render_remote.enabled():
        fname = f"{renderer._slugify(req.title or 'clip')}_{req.job_id}.mp4"
        # The box runs its own ffmpeg + publishes progress under the job_id; relay
        # that into THIS app's progress store keyed by progress_id, so the UI's
        # /api/render-progress poll shows a live percent for remote renders too.
        cb = ucb = None
        if req.progress_id:
            cb = lambda pct: renderer.set_progress(req.progress_id, pct, "rendering")
            ucb = lambda frac: renderer.set_progress(req.progress_id, frac * 100.0, "uploading")
        # Upload only the needed SLICE of the source (the network to the boxes is
        # ~5 MB/s and a full-length source is huge). Only when a bounded range is set
        # (the per-sub-clip case). Falls back to the full source otherwise / on failure.
        slice_path = None
        if req.render_start is not None and req.render_end is not None and req.render_end > req.render_start:
            slice_path = _make_source_slice(src, req.render_start, req.render_end)
        try:
            if slice_path:
                result = render_remote.render_until(
                    job_id=req.job_id, source_path=Path(slice_path),
                    params=_sliced_params(req, req.render_start),
                    out_dir=renderer.OUTPUT_DIR, filename=fname, progress_cb=cb, upload_cb=ucb,
                    source_name=f"{req.job_id}_slice", always_upload=True)
            else:
                result = render_remote.render_until(
                    job_id=req.job_id, source_path=src,
                    params=req.model_dump(exclude={"job_id", "cleanup", "progress_id"}),
                    out_dir=renderer.OUTPUT_DIR, filename=fname, progress_cb=cb, upload_cb=ucb)
        finally:
            if slice_path:
                try:
                    os.unlink(slice_path)
                except OSError:
                    pass
        if result is None:
            renderer.clear_progress(req.progress_id)
            raise HTTPException(status_code=503,
                                detail="GPU render farm busy/unreachable — semua box sibuk atau mati. "
                                       "Coba lagi sebentar (render TIDAK dijalankan di laptop).")
    else:
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
                caption_pos=req.caption_pos,
                caption_pos_ranges=req.caption_pos_ranges,
                text_overlays=req.text_overlays,
                stickers=req.stickers,
                render_start=req.render_start,
                render_end=req.render_end,
                sfx=req.sfx,
                top_eighths=req.top_eighths,
                fullscreen_windows=req.fullscreen_windows,
                keep_segments=req.keep_segments,
                progress_id=req.progress_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    renderer.clear_progress(req.progress_id)   # done — drop the token (remote path leaves it set)
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
    # Director mode: transcribe inline (semantic context) and, if asked + available,
    # diarize for the speaker hint. Both best-effort — degrade to the plain path.
    words, turns = None, None
    if req.director:
        try:
            words = transcriber.transcribe(src)
        except Exception:  # noqa: BLE001
            words = None
        if req.diarization and diarize.enabled():
            try:
                turns = diarize.diarize_turns(src)
            except Exception:  # noqa: BLE001
                turns = None
    try:
        out = autobox.predict_track(
            src, req.prompt, req.t_start, req.t_end,
            step_seconds=req.step_seconds, padding=req.padding, smooth=req.smooth,
            lock_size=req.lock_size, head_room=req.head_room,
            # the single crop box follows the streamer — tells fullscreen layout
            # segments to use the whole frame (only matters with {layout} prompts)
            role={1: "streamer"}.get(req.box),
            use_director=req.director, words=words, turns=turns, expect=req.expect,
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
    return {"keyframes": kfs, "sampled": out["sampled"], "detected": out["detected"],
            "message": msg, "director_note": out.get("director_note", "")}


@app.post("/api/thumbnail-text", response_model=ThumbnailTextResponse)
def api_thumbnail_text(req: ThumbnailTextRequest):
    """Eye-catching thumbnail headline suggestions (text only) from the LLM. The
    frame + compositing + PNG export are all done client-side on a canvas."""
    if not thumbnail.enabled():
        raise HTTPException(status_code=400,
                            detail="text model not configured (set VLLM_BASE_URL / VLLM_MODEL in .env)")
    try:
        titles = thumbnail.generate_titles(req.context, req.n, req.language, req.tone)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"titles": titles}


@app.get("/api/capabilities")
def api_capabilities():
    """Lets the frontend hide/disable the AI auto-box (vision) and the thumbnail
    headline ideas (text model) when the endpoints aren't configured."""
    return {"vision": vision.enabled(), "thumbnail": thumbnail.enabled(),
            "diarize": diarize.enabled(), "image_sources": imagesources.available(),
            # GPU render farm: lets the UI dispatch per-sub-clip renders in parallel
            # across boxes (and show "rendering on N GPUs"). 0 boxes → local render.
            "render_remote": {"enabled": render_remote.enabled(), "boxes": render_remote.box_count()}}


@app.get("/api/render-progress")
def api_render_progress(id: str):
    """Live progress for a render token: {phase:'uploading'|'rendering', pct:0–100}
    or {} when unknown/done. Covers the source-upload phase (the slow part for a big
    clip) AND the ffmpeg phase, for both local and remote renders."""
    return renderer.get_progress(id) or {}


@app.post("/api/cleanup")
def api_cleanup(req: CleanupRequest):
    downloader.cleanup_job(req.job_id)
    return {"ok": True}


# ───────────────────────── batch queue ─────────────────────────
@app.post("/api/queue/import")
def api_queue_import(req: QueueImportRequest):
    """Upload a JSON of clips ({url: [{id,start,end,title,description,bbox_1,bbox_2}]}).
    Each clip becomes a queued job the background worker downloads + auto-boxes
    (bbox_1 = the single crop box prompt; bbox_2 is ignored in illustrator).
    `room_id` (optional) groups the new jobs under a room."""
    try:
        return batch_queue.import_text(req.content, room_id=req.room_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not parse JSON: {e}")


# ───────────────────────── rooms ─────────────────────────
@app.get("/api/rooms")
def api_rooms_list():
    """Streamer/project groups for the queue sidebar (id, name, job count)."""
    return {"rooms": batch_queue.list_rooms()}


@app.post("/api/rooms")
def api_rooms_create(req: RoomCreate):
    try:
        return batch_queue.create_room(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/rooms/{room_id}")
def api_rooms_delete(room_id: int):
    """Delete a room AND every job in it (with their downloaded videos)."""
    return batch_queue.delete_room(room_id)


@app.get("/api/queue")
def api_queue_list():
    """Sidebar summary of every job (status, kf counts) — polled by the frontend."""
    return {"jobs": batch_queue.list_jobs(), "box_eta": batch_queue.boxing_eta_seconds()}


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


@app.post("/api/queue/{key}/skip-box")
def api_queue_skip_box(key: str):
    """Pull a job out of the AI-boxing queue → ready immediately, no boxes —
    the user draws them manually instead of waiting for the boxing stage."""
    j = batch_queue.skip_boxing(key)
    if not j:
        raise HTTPException(status_code=404,
                            detail="job is not waiting for boxing (only 'downloaded' jobs can skip)")
    return {"ok": True}


@app.post("/api/queue/stop-boxing")
def api_queue_stop_boxing():
    """Stop the whole boxing run: every job still waiting to be boxed goes to
    ready (draw-manually). In-flight jobs finish; no new ones start."""
    return {"stopped": batch_queue.stop_boxing()}


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


_TEMP_MEDIA = {
    ".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".bmp": "image/bmp",
}


@app.get("/temp/{name}")
def serve_temp(name: str):
    p = TEMP_DIR / name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type=_TEMP_MEDIA.get(p.suffix.lower(), "application/octet-stream"))


@app.get("/api/img")
def api_img(url: str):
    """Same-origin proxy for a stock image so the Thumbnail canvas isn't tainted
    (a cross-origin image makes canvas.toBlob throw, breaking PNG export). The
    illustrator pulls from many hosts (Pexels, Openverse aggregates arbitrary
    sites, Wikimedia, Unsplash, Pixabay), so instead of a host whitelist we allow
    any https URL but block private/loopback/link-local targets (basic SSRF guard).
    /temp/ URLs are already same-origin and never reach here."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    import requests
    from fastapi import Response

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="only http(s) image URLs allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"cannot resolve host: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(status_code=400, detail="host not allowed")
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "clipper-illustrator/1.0"})
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    ctype = r.headers.get("content-type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise HTTPException(status_code=415, detail="not an image")
    return Response(content=r.content, media_type=ctype)


@app.post("/api/image-upload")
async def api_image_upload(request: Request, filename: str = "image"):
    """Upload the user's OWN image (raw body, like the soundboard import) — used
    as a PNG sticker or an uploaded illustration source. Saved into temp/ deduped
    by content hash; the returned /temp/ URL plugs straight into download_pick
    (which treats /temp/ URLs as already-local, keeping PNG alpha)."""
    import hashlib
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image too large (max 25MB)")
    ext = Path(filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        ext = ".png"
    name = f"up_{hashlib.sha1(data).hexdigest()[:12]}{ext}"
    (TEMP_DIR / name).write_bytes(data)
    return {"url": f"/temp/{name}", "thumb": f"/temp/{name}"}


@app.post("/api/save-output-image")
async def api_save_output_image(request: Request, filename: str = "thumbnail.png"):
    """Save a client-rendered thumbnail PNG into output/ (next to the rendered mp4)
    so 'make a thumbnail with the render' can drop one file per sub-clip. Raw body =
    the PNG bytes (like the soundboard/image-upload endpoints)."""
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image too large (max 25MB)")
    name = Path(filename).name or "thumbnail.png"     # strip any path traversal
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        name += ".png"
    (OUTPUT_DIR / name).write_bytes(data)
    return {"output_path": f"/output/{name}", "filename": name}


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
    uvicorn.run("main:app", host="127.0.0.1", port=8031, reload=False)
