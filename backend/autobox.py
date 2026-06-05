"""AI auto-box track predictor.

Samples frames across a time range, asks the vision model for the prompted
subject's bounding box on each, and returns a list of keyframes in clipper's
schema ({t,x,y,w,h,interp,fit,gap}, source pixels) that drop straight into the
Position editor — the user then drags / resizes / deletes them like manual kfs.

Frames are processed concurrently (the vision endpoint handles ~4 parallel calls
cleanly per the grounding study). Frames whose detection fails are simply skipped
— linear interpolation between the surrounding keyframes bridges the gap.
"""
import base64
import json
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import vision

log = logging.getLogger(__name__)

MAX_FRAMES = 80      # hard cap on vision calls per request (bounds cost/latency)
MAX_WORKERS = 4      # endpoint sweet spot (study: 4 clean, up to 6 ok, then queues)
SEND_MAX_W = 1280    # downscale frames sent to the model (coords are scale-free)


def _probe(path: Path) -> tuple:
    """(width, height, duration_seconds) of the source video."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    j = json.loads(r.stdout or "{}")
    s = (j.get("streams") or [{}])[0]
    dur = float((j.get("format") or {}).get("duration") or 0.0)
    return int(s.get("width", 0)), int(s.get("height", 0)), dur


def _extract_frame_b64(src: Path, t: float) -> Optional[str]:
    """Grab one frame at time t (fast-seek), downscaled to <=SEND_MAX_W wide,
    as base64 JPEG. Downscaling is safe: the model's coords are 0-1000 normalized,
    so they still convert against the SOURCE dimensions."""
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
            "-vf", f"scale='min({SEND_MAX_W},iw)':-2", "-q:v", "4", tmp,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.getsize(tmp):
            return None
        return base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("frame extract @%.2fs failed: %s", t, e)
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sample_times(t0: float, t1: float, step: float):
    """Return (times, effective_step, capped). When the range is long enough to
    exceed MAX_FRAMES, the step is widened so coverage stays within the cap —
    `capped` lets the caller tell the user their density was overridden."""
    n = int((t1 - t0) / step) + 1
    capped = False
    if n > MAX_FRAMES:
        step = (t1 - t0) / (MAX_FRAMES - 1)
        n = MAX_FRAMES
        capped = True
    times = [round(t0 + i * step, 3) for i in range(n)]
    if not times or times[-1] < t1 - 1e-3:
        times.append(round(t1, 3))
    return times, step, capped


def _pad(box: dict, frac: float, w: int, h: int) -> dict:
    """Expand a tight subject box by `frac` on each side, clamped to the frame."""
    pw, ph = box["w"] * frac, box["h"] * frac
    x = max(0.0, box["x"] - pw)
    y = max(0.0, box["y"] - ph)
    return {
        "x": x, "y": y,
        "w": min(w - x, box["w"] + 2 * pw),
        "h": min(h - y, box["h"] + 2 * ph),
    }


def _build_keyframes(seq: list, w: int, h: int) -> tuple:
    """Turn a per-sample sequence [(t, box|None), ...] (time-ordered) into clipper
    keyframes. Detected frames → real kfs (linear track). A run of UNDETECTED
    frames (the subject isn't in the shot) becomes a `gap` keyframe so the slot
    renders BLACK there instead of a stale box being held/glided across the
    absence — i.e. the box simply isn't drawn when the subject is absent.

    A LONE undetected frame in the middle (a likely model hiccup, subject still
    present) is tolerated: no gap, linear interp bridges it. Runs of >=2, or any
    absence at the very start/end of the range, become a gap. Returns
    (keyframes, detected_count)."""
    n = len(seq)
    detected = sum(1 for _, b in seq if b)
    if detected == 0:
        return [], 0

    def real(t, b):
        return {"t": round(t, 3), "x": round(b["x"], 1), "y": round(b["y"], 1),
                "w": round(b["w"], 1), "h": round(b["h"], 1),
                "interp": "linear", "fit": "cover", "gap": False}

    def gap(t, b):
        b = b or {"x": 0.0, "y": 0.0, "w": float(max(2, w)), "h": float(max(2, h))}
        return {"t": round(t, 3), "x": round(b["x"], 1), "y": round(b["y"], 1),
                "w": round(b["w"], 1), "h": round(b["h"], 1),
                "interp": "hold", "fit": "cover", "gap": True}

    kfs = []
    last_box = None
    i = 0
    while i < n:
        t, b = seq[i]
        if b:
            kfs.append(real(t, b))
            last_box = b
            i += 1
            continue
        # run of undetected samples [i, j)
        j = i
        while j < n and seq[j][1] is None:
            j += 1
        run = j - i
        boundary = (i == 0) or (j == n)
        if (run >= 2 or boundary) and (not kfs or not kfs[-1]["gap"]):
            kfs.append(gap(seq[i][0], last_box))  # subject absent → empty slot here
        i = j
    return kfs, detected


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


def _stabilize_size(dets: list, w: int, h: int, pct: float = 0.85) -> list:
    """Two-pass size stabilization: after seeing ALL detected boxes, lock ONE size
    (the `pct` percentile of widths/heights — big enough to hold the subject in
    most frames, robust to outliers) and re-center it on each frame's detected
    center. The box then pans to follow the subject but never zoom-jitters; a
    constant size also renders as a smooth expression-crop instead of stepped
    per-segment crops."""
    if not dets:
        return dets
    sw = min(float(w), _percentile(sorted(b["w"] for _, b in dets), pct))
    sh = min(float(h), _percentile(sorted(b["h"] for _, b in dets), pct))
    sw = max(2.0, sw)
    sh = max(2.0, sh)
    out = []
    for t, b in dets:
        cx = b["x"] + b["w"] / 2.0
        cy = b["y"] + b["h"] / 2.0
        x = max(0.0, min(cx - sw / 2.0, w - sw))
        y = max(0.0, min(cy - sh / 2.0, h - sh))
        out.append((t, {"x": x, "y": y, "w": sw, "h": sh}))
    return out


def _smooth(dets: list, win: int = 3) -> list:
    """Centered moving-average over the detected sequence to damp frame-to-frame
    jitter (the model boxes wobble a few px). Keeps each kf's time."""
    keys = ("x", "y", "w", "h")
    half = win // 2
    out = []
    for i in range(len(dets)):
        lo, hi = max(0, i - half), min(len(dets), i + half + 1)
        avg = {k: sum(dets[j][1][k] for j in range(lo, hi)) / (hi - lo) for k in keys}
        out.append((dets[i][0], avg))
    return out


def predict_track(
    source_path: Path,
    prompt: str,
    t_start: float = 0.0,
    t_end: Optional[float] = None,
    step_seconds: float = 1.5,
    padding: float = 0.05,
    smooth: bool = True,
    lock_size: bool = True,
) -> dict:
    """Predict a box track over [t_start, t_end]. Returns
    {keyframes, sampled, detected, width, height}. With `lock_size` (default), the
    box SIZE is locked across the whole range (a global percentile) and only its
    center pans — stable framing, no zoom jitter."""
    w, h, dur = _probe(source_path)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not probe source dimensions")

    t0 = max(0.0, float(t_start or 0.0))
    t1 = float(t_end) if t_end is not None else (dur or t0 + 10.0)
    if dur:
        t1 = min(t1, dur)
    step = max(0.2, float(step_seconds or 1.5))
    if t1 <= t0:
        t1 = min(t0 + step, dur or t0 + step)
    times, eff_step, capped = _sample_times(t0, t1, step)

    def work(t: float):
        b64 = _extract_frame_b64(source_path, t)
        if not b64:
            return (t, None)
        return (t, vision.detect_box(b64, prompt, w, h))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(work, times))
    results.sort(key=lambda r: r[0])

    # Process only the detected boxes (pad + smooth), keeping their sample times…
    det_items = [(t, b) for t, b in results if b]
    if padding and padding > 0:
        det_items = [(t, _pad(b, padding, w, h)) for t, b in det_items]
    if smooth and len(det_items) >= 3:
        det_items = _smooth(det_items)
    if lock_size:
        det_items = _stabilize_size(det_items, w, h)
    proc = {t: b for t, b in det_items}
    # …then rebuild the FULL per-sample sequence (box or None) so absent stretches
    # become `gap` keyframes (empty slot) rather than a held/glided stale box.
    seq = [(t, proc.get(t)) for t, _ in results]
    keyframes, detected = _build_keyframes(seq, w, h)

    return {
        "keyframes": keyframes,
        "sampled": len(times),
        "detected": detected,
        "width": w,
        "height": h,
        "step": round(eff_step, 2),
        "capped": capped,
    }
