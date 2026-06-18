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
# Plausibility gate for SUBJECT tracking (not the layout/side probes, which need
# huge boxes): drop a detection that fills >92% of the frame or is an extreme
# strip (aspect > 6) — a mis-detection for a person/subject prompt. → None → the
# normal miss/bridge/gap machinery handles it.
_GATE_MAX_AREA = 0.92
_GATE_MAX_ASPECT = 6.0


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


def _render_final_crop_b64(src: Path, t: float, box: dict, slot_w: int, slot_h: int) -> Optional[str]:
    """Render the FINAL cover-cropped SLOT image at time t (full-res, base64 JPEG)
    exactly as the renderer would: crop the source box, scale-cover to the slot,
    center-crop. Used by verify_framing to judge the REAL on-screen framing (NOT a
    downscaled probe — head-cut must be read on real pixels)."""
    cx, cy = int(max(0, round(box["x"]))), int(max(0, round(box["y"])))
    cw, ch = int(max(2, round(box["w"]))), int(max(2, round(box["h"])))
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        vf = (f"crop={cw}:{ch}:{cx}:{cy},"
              f"scale={slot_w}:{slot_h}:force_original_aspect_ratio=increase,"
              f"crop={slot_w}:{slot_h}")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
               "-vf", vf, "-q:v", "4", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.getsize(tmp):
            return None
        return base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("final-crop render @%.2fs failed: %s", t, e)
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _box_at(kfs: list, t: float) -> Optional[dict]:
    """The {x,y,w,h} a box occupies at time t (hold/linear interp), or None when the
    segment containing t is a gap/empty. Used to composite the review preview."""
    if not kfs:
        return None
    ks = sorted(kfs, key=lambda k: k.get("t", 0.0))

    def xywh(k):
        if k.get("gap"):
            return None
        d = {q: float(k[q]) for q in ("x", "y", "w", "h")}
        d["fit"] = k.get("fit", "cover")
        return d

    if t <= ks[0].get("t", 0.0):
        return xywh(ks[0])
    for a, b in zip(ks, ks[1:]):
        if a["t"] <= t < b["t"]:
            if a.get("gap"):
                return None
            if a.get("interp") == "linear" and not b.get("gap") and b["t"] > a["t"]:
                f = (t - a["t"]) / (b["t"] - a["t"])
                d = {q: float(a[q]) + (float(b[q]) - float(a[q])) * f
                     for q in ("x", "y", "w", "h")}
                d["fit"] = a.get("fit", "cover")
                return d
            return xywh(a)
    return xywh(ks[-1])


def _composite_preview_b64(src: Path, t: float, b1: Optional[dict],
                           b2: Optional[dict]) -> Optional[str]:
    """Build the 9:16 CROP PREVIEW at time t exactly as the renderer composes it:
    both boxes present → box1 cover-cropped into the top 1080x720 slot stacked over
    box2 in the bottom 1080x1200; one box only → it fills the whole 1080x1920
    (single-box full mode); neither → black. Returns base64 JPEG (what the director
    review sees) or None on failure. Cover-fit is used deliberately — it reveals any
    clipping, which is exactly what the director must judge (blur vs cover etc.)."""
    from PIL import Image
    import io

    def slot(box, sw, sh):
        if not box:
            return Image.new("RGB", (sw, sh), (0, 0, 0))
        bb = (_render_blur_crop_b64(src, t, box, sw, sh)
              if box.get("fit") == "blur_pad"
              else _render_final_crop_b64(src, t, box, sw, sh))
        if not bb:
            return Image.new("RGB", (sw, sh), (0, 0, 0))
        try:
            im = Image.open(io.BytesIO(base64.b64decode(bb))).convert("RGB")
            return im if im.size == (sw, sh) else im.resize((sw, sh))
        except Exception:  # noqa: BLE001
            return Image.new("RGB", (sw, sh), (0, 0, 0))

    try:
        if b1 and b2:
            canvas = Image.new("RGB", (_OUT_W, _OUT_FULL_H), (0, 0, 0))
            canvas.paste(slot(b1, _OUT_W, _TOP_H), (0, 0))
            canvas.paste(slot(b2, _OUT_W, _BOT_H), (0, _TOP_H))
        elif b1:
            canvas = slot(b1, _OUT_W, _OUT_FULL_H)
        elif b2:
            canvas = slot(b2, _OUT_W, _OUT_FULL_H)
        else:
            return None
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("composite preview @%.2fs failed: %s", t, e)
        return None


def _render_blur_crop_b64(src: Path, t: float, box: dict, slot_w: int, slot_h: int) -> Optional[str]:
    """The FINAL blur_pad SLOT image at time t (base64 JPEG), exactly as the renderer
    composes blur_pad: the whole box scale-CONTAINED over a blurred scale-cover copy
    of itself. Lets the review preview show what a blur_pad box truly looks like (no
    clipping) so the director doesn't keep asking to 'blur' an already-blurred box."""
    cx, cy = int(max(0, round(box["x"]))), int(max(0, round(box["y"])))
    cw, ch = int(max(2, round(box["w"]))), int(max(2, round(box["h"])))
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        vf = (f"crop={cw}:{ch}:{cx}:{cy},setsar=1,split=2[a][b];"
              f"[a]scale={slot_w}:{slot_h}:force_original_aspect_ratio=increase,"
              f"crop={slot_w}:{slot_h},gblur=sigma=30:steps=2,eq=brightness=-0.08[bg];"
              f"[b]scale={slot_w}:{slot_h}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
               "-filter_complex", vf, "-q:v", "4", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.getsize(tmp):
            return None
        return base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("blur-crop render @%.2fs failed: %s", t, e)
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


def _headroom(box: dict, frac: float, w: int, h: int) -> dict:
    """TOP-ONLY expansion: grow the box upward by `frac` of its height so a tight
    person detection keeps headroom (forehead not clipped), especially at
    padding=0. Width/x untouched; the bottom edge is unchanged so the growth only
    adds space above the head. The upward center shift is frac*h/2 — proportional to
    each box's height, so when sizes are stable (the locked/pinned case) it's a
    near-uniform offset that adds only a tiny center-y wobble (<< the moving
    thresholds), and it survives _stabilize_size/_lock_static (they re-center on the
    raised center)."""
    grow = box["h"] * frac
    y = max(0.0, box["y"] - grow)
    return {"x": box["x"], "y": y, "w": box["w"],
            "h": min(h - y, box["h"] + (box["y"] - y))}


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


def _reject_outliers(dets: list, lo: float = 0.5, hi: float = 1.5) -> list:
    """Drop detections whose AREA deviates wildly from the median — the model
    occasionally boxes the whole screen (or a merged region) instead of the
    subject, and one such box poisons both the moving-average smoothing and the
    locked size (verified on real reaction footage: 5 good half-frame boxes + 1
    full-frame box pushed the p85 lock ~40% too wide). Dropped frames become
    misses: a lone one is bridged by the keyframe builder, a run becomes a gap."""
    if len(dets) < 3:
        return dets
    areas = sorted(b["w"] * b["h"] for _, b in dets)
    med = areas[len(areas) // 2]
    if med <= 0:
        return dets
    kept = [(t, b) for t, b in dets if lo <= (b["w"] * b["h"]) / med <= hi]
    return kept if kept else dets


def _stabilize_size(dets: list, w: int, h: int, pct: float = 0.5) -> list:
    """Two-pass size stabilization: after seeing ALL detected boxes, lock ONE size
    (the MEDIAN width/height — outliers are rejected upstream, and the median
    keeps the box tight instead of inflating toward occasional over-wide
    detections) and re-center it on each frame's detected center. The box then
    pans to follow the subject but never zoom-jitters; a constant size also
    renders as a smooth expression-crop instead of stepped per-segment crops."""
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


def _lock_static(dets: list, w: int, h: int, frac: float = 0.02):
    """Whole-clip context pass: if the box's CENTER barely moves across the whole
    range (split-screen panels are static — the per-frame wobble is model noise,
    not subject motion), pin the box at the median center → zero pan jitter and a
    single static box. MAD-based (median abs deviation vs `frac` of the frame
    dimension) so a couple of stray detections don't unlock it; a real pan moves
    the MAD far past the threshold and is left untouched.
    Returns (dets, pinned: bool)."""
    if len(dets) < 3:
        return dets, False
    cxs = sorted(b["x"] + b["w"] / 2.0 for _, b in dets)
    cys = sorted(b["y"] + b["h"] / 2.0 for _, b in dets)
    mcx, mcy = cxs[len(cxs) // 2], cys[len(cys) // 2]
    madx = sorted(abs(c - mcx) for c in cxs)[len(cxs) // 2]
    mady = sorted(abs(c - mcy) for c in cys)[len(cys) // 2]
    if madx > w * frac or mady > h * frac:
        return dets, False   # genuine movement — keep the panning track
    out = []
    for t, b in dets:
        x = max(0.0, min(mcx - b["w"] / 2.0, w - b["w"]))
        y = max(0.0, min(mcy - b["h"] / 2.0, h - b["h"]))
        out.append((t, {"x": x, "y": y, "w": b["w"], "h": b["h"]}))
    return out, True


def _dedupe_keyframes(kfs: list, eps: float = 0.5) -> list:
    """Merge consecutive non-gap keyframes with identical boxes into the first
    one — with interp='hold' they render identically, and a static locked box
    collapses from dozens of kfs to one clean segment (plus gap boundaries)."""
    out: list = []
    for k in kfs:
        p = out[-1] if out else None
        if (p is not None and not k.get("gap") and not p.get("gap")
                and abs(k["x"] - p["x"]) <= eps and abs(k["y"] - p["y"]) <= eps
                and abs(k["w"] - p["w"]) <= eps and abs(k["h"] - p["h"]) <= eps):
            continue
        out.append(k)
    return out


def _smooth(dets: list, med_win: int = 5, avg_win: int = 3) -> list:
    """Temporal smoothing so each box reflects a WINDOW of recent frames, not a
    single (possibly flaky) detection — the model decides each frame
    independently, so one bad frame jumps the box for a fraction of a second.
    TWO passes per coordinate, centered:
      1. MEDIAN over `med_win` (±2) — fully rejects a single-frame spike
         (median of [normal, spike, normal] = normal); this is the 'remember
         the last few frames before deciding' part.
      2. light MOVING-AVERAGE over `avg_win` — irons the few-px residual wobble.
    Keeps each kf's time."""
    keys = ("x", "y", "w", "h")

    def window(seq, i, win):
        half = win // 2
        return seq[max(0, i - half):min(len(seq), i + half + 1)]

    med = []
    for i in range(len(dets)):
        win = window(dets, i, med_win)
        m = {}
        for k in keys:
            vals = sorted(b[k] for _, b in win)
            n = len(vals)
            m[k] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
        med.append((dets[i][0], m))
    out = []
    for i in range(len(med)):
        win = window(med, i, avg_win)
        avg = {k: sum(b[k] for _, b in win) / len(win) for k in keys}
        out.append((med[i][0], avg))
    return out


_SIDE_PROBE_PROMPT = ("the main on-camera person — the host or speaker talking directly "
                      "to the camera (NOT people who appear inside another video, image, "
                      "or post shown on screen)")


def _resolve_side(source_path: Path, times: list, w: int, h: int) -> str:
    """Which horizontal half holds the webcam panel ('left'/'right'), resolved by
    GEOMETRY: detect the panel on up to 5 spread frames and vote by box-center
    half (near-center boxes within ±5% of mid are ambiguous → ignored). Falls
    back to the one-word QA probe, then to 'left'. Geometric voting is far more
    stable than asking the model 'left or right?' — the reacted content often
    shows people talking to camera too, which flips the QA answer."""
    n = len(times)
    idxs = sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1})
    votes = []
    for i in idxs:
        b64 = _extract_frame_b64(source_path, times[i])
        if not b64:
            continue
        det = vision.detect_box(b64, _SIDE_PROBE_PROMPT, w, h)
        if det:
            cx = det["x"] + det["w"] / 2.0
            if abs(cx - w / 2.0) > w * 0.05:
                votes.append("left" if cx < w / 2.0 else "right")
    if votes:
        return max(set(votes), key=votes.count)
    b64 = _extract_frame_b64(source_path, times[n // 2])
    return (vision.detect_side(b64) if b64 else None) or "left"


# {layout} placeholder expansions — assertive, layout-specific phrasing (the
# model needs a CONCRETE structural description; vague wording measurably
# degrades boxing). Chosen per layout segment by _detect_layout_segments.
_LAYOUT_PHRASES = {
    "split": "The screen is split into two side-by-side panels, each spanning the full height: the main on-camera person's panel and a second panel beside it (a co-host/guest, or a content area).",
    "overlay": "A larger area fills most of the screen, with the main on-camera person shown as a smaller overlay window drawn on top of it.",
    "full": "The main on-camera person fills the entire screen here; there is NO second panel in this part of the video.",
    "fullcontent": "A second region — a content area or another person — fills the entire screen here; the main on-camera person is NOT visible in this part of the video.",
}

# box2's subject is GENERAL: a second person (co-host/guest) OR a content area. The
# specific per-clip subject lives in bbox_2; this probe only needs to recognize
# that SOME distinct second region exists beside the main person, while keeping the
# "a face inside shown content is part of that content" guard (else it boxes a
# person inside a meme/video instead of the meme region).
_CONTENT_PROBE_PROMPT = ("a separate on-screen region distinct from the main "
                         "on-camera person — EITHER a second person shown in their own "
                         "camera panel beside the host (a co-host or guest), OR a "
                         "content area: another video, image, screenshot, slide, "
                         "graphic, game feed, social-media post, comment, or text. A "
                         "person shown INSIDE a video, image, screenshot, or post is "
                         "PART of that content — box the whole content region, not that "
                         "person alone (NOT the host/presenter's own camera)")

_CONTENT_PRESENT_QUESTION = (
    "Besides the main on-camera person and their room/background, is there a "
    "SEPARATE region on screen right now in its own panel distinct from that "
    "person — either a SECOND person (a co-host or guest beside them), or a content "
    "area (another video, image, slide, graphic, game feed, social-media post, "
    "comment, screenshot, or text)? A plain wall, shelves, or studio background is "
    "NOT it. Answer with one word: yes or no.")

_CAM_PRESENT_QUESTION = ("Is the main on-camera person (the host or speaker talking to "
                         "the camera) visible in this frame? A face that is PART of "
                         "another video, image, or post being shown does NOT count. "
                         "Answer with one word: yes or no.")


def _geom(det, w, h):
    """(huge, panel, small, side) flags for one detection — the shared geometric
    vocabulary: 'huge' covers (nearly) the whole screen, 'panel' is a wide
    edge-anchored column, 'small' is an overlay-sized window."""
    if not det:
        return (False, False, False, None)
    cx = det["x"] + det["w"] / 2.0
    side = "left" if cx < w / 2.0 else "right"
    edge = det["x"] <= 0.05 * w or (det["x"] + det["w"]) >= 0.95 * w
    area = det["w"] * det["h"] / float(w * h)
    huge = det["w"] >= 0.85 * w or (not edge and area >= 0.45)
    panel = (not huge) and det["w"] >= 0.34 * w and edge
    small = det["w"] < 0.34 * w
    return (huge, panel, small, side)


def _classify_layout(source_path: Path, t: float, w: int, h: int):
    """One layout probe at time t → (layout, side) | None. FOUR classes — not
    every frame is person-beside-content: 'split' (side-by-side panels),
    'full' (the webcam fills the screen → box1 is the whole frame),
    'fullcontent' (the content/text fills the screen, e.g. a thread — box2 is
    the whole frame), 'overlay' (small cam window over content). DUAL geometric
    probe: the person probe decides when it's confident (its prompt excludes
    people inside the content); the content probe catches person-less frames
    the person probe can only miss on. A small person box over huge content is
    ambiguous (corner cam vs a face inside the content) → one yes/no presence
    QA breaks the tie. None = inconclusive, filled from neighboring probes."""
    b64 = _extract_frame_b64(source_path, t)
    if not b64:
        return None
    p = vision.detect_box(b64, _SIDE_PROBE_PROMPT, w, h)
    p_huge, p_panel, p_small, p_side = _geom(p, w, h)
    if p_huge:
        return ("full", None)
    if p_panel:
        # A panel-shaped person box is AMBIGUOUS: in a fullscreen webcam shot
        # where the streamer sits off-center, the person box also comes out
        # edge-anchored at ~half the frame — geometrically identical to a true
        # split panel (systematic, so no amount of majority voting fixes it).
        # The real discriminator is the OTHER side: a true split has content
        # there. Require corroboration from the content probe — anything it
        # finds clear of the person panel on the opposite side counts (it
        # often boxes an object INSIDE the content panel, e.g. a 380px banner
        # within an 825px panel, so no minimum width). Without that, it's a
        # fullscreen webcam shot with the streamer sitting off-center.
        c = vision.detect_box(b64, _CONTENT_PROBE_PROMPT, w, h)
        c_huge2, _, _, c_side2 = _geom(c, w, h)
        if c is not None and not c_huge2 and c_side2 != p_side:
            if p_side == "left":
                clear = c["x"] >= (p["x"] + p["w"]) - 0.08 * w
            else:
                clear = (c["x"] + c["w"]) <= p["x"] + 0.08 * w
            if clear:
                # Geometric corroboration is NOT enough: a fullscreen studio
                # shot with the streamer to one side has dark background on the
                # other, which the content probe flakily boxes as a "panel" —
                # geometrically identical to a real split with a dark video.
                # Confirm with a direct yes/no that REACTED content is actually
                # present (a wall/shelf is not content).
                ans = (vision.describe(b64, _CONTENT_PRESENT_QUESTION, max_tokens=5)
                       or "").strip().lower()
                if ans.startswith("no"):
                    return ("full", None)
                return ("split", p_side)
        # No corroborating content beside an edge-anchored panel is AMBIGUOUS,
        # not proof of fullscreen: the content probe also just misses (e.g. a
        # dark vertical video) — answering 'full' here systematically poisoned
        # a whole clip on a second streamer's footage. Leave it inconclusive
        # and let the neighboring probes decide.
        return None
    c = vision.detect_box(b64, _CONTENT_PROBE_PROMPT, w, h)
    c_huge = _geom(c, w, h)[0]
    if p is None:
        if c_huge:
            return ("fullcontent", None)
        return None
    if p_small:
        # A small person box clearly SEPARATE from the content box IS a corner/
        # side cam — no QA needed, and the content needn't be 'huge' (a 3-column
        # layout's middle video is ~40% of the frame; requiring huge made every
        # such frame inconclusive, and a couple of spurious 'full' labels then
        # filled the whole clip). The QA tiebreak is only for the ambiguous
        # cases: the person box sits INSIDE the content (a face that is part of
        # a meme/video) or no content was found at all.
        if c is not None:
            ix = max(0.0, min(p["x"] + p["w"], c["x"] + c["w"]) - max(p["x"], c["x"]))
            iy = max(0.0, min(p["y"] + p["h"], c["y"] + c["h"]) - max(p["y"], c["y"]))
            if (ix * iy) / max(1.0, p["w"] * p["h"]) < 0.3:
                return ("overlay", p_side)
        ans = (vision.describe(b64, _CAM_PRESENT_QUESTION, max_tokens=5) or "").strip().lower()
        if ans.startswith("no"):
            return ("fullcontent", None)
        if ans.startswith("yes"):
            return ("overlay", p_side)
        return None
    if not p_small:
        # Medium person box NOT anchored to an edge — a fullscreen TORSO shot.
        # The 'huge' rule assumes the person fills the frame, but a streamer
        # framed from the waist up in a room covers only ~40-50% and sits
        # mid-frame (found on a second streamer's footage — every fullscreen
        # frame came back inconclusive and caught the neighbors' overlay
        # label). Only call it fullscreen when the content probe found nothing
        # CLEAR of the person; content beside a centered person stays
        # inconclusive (neighbors fill it).
        edge = p["x"] <= 0.05 * w or (p["x"] + p["w"]) >= 0.95 * w
        if not edge:
            if c is None:
                return ("full", None)
            cx = c["x"] + c["w"] / 2.0
            if p["x"] - 0.04 * w <= cx <= p["x"] + p["w"] + 0.04 * w:
                return ("full", None)   # "content" box sits ON the person → same shot
    return None


# Change-flag probe: ask the model to COMPARE two frames instead of classifying
# each independently. Binary same/changed answers are far more reliable than
# absolute 4-class labels (classification noise was producing spurious micro-
# segments → flickering boxes), and they directly flag WHEN the streamer/content
# panel moves or resizes — even within the same layout class.
_CHANGE_PROBE_QUESTION = (
    "These are two frames from the same video: frame A first, frame B "
    "second. Did the SCREEN LAYOUT change between them — a person's camera panel "
    "or a content panel appearing, disappearing, MOVING, or changing SIZE? Ignore "
    "what plays INSIDE a panel (the video content itself changing does not "
    "count); only the panel geometry matters. "
    "Answer exactly 'SAME' or 'CHANGED: <one short phrase>'."
)


def _detect_layout_segments(source_path: Path, t0: float, t1: float, w: int, h: int,
                            min_gap: float = 1.0, max_probes: int = 9):
    """The layout can CHANGE mid-clip (the cam moves / the split flips).
    Base: the classification-probe path (validated). Then CHANGE-FLAG
    refinement — pairwise frame comparisons VERIFY each boundary (a boundary
    the model says nothing changed across is probe noise → merged away, which
    kills flickery micro-segments) and flag panel moves/resizes INSIDE long
    split segments (so the per-segment size lock re-measures on each side).
    Pure comparison-driven segmentation was tried and measured WORSE (person
    movement in fullscreen stretches flags false CHANGEDs, and one
    classification per span loses the majority-vote noise immunity) — so
    comparisons only ever refine the validated base, never replace it.
    Returns ordered segments [{t0, t1, layout, side}], or None if every probe
    was inconclusive."""
    segs = _segments_from_class_probes(source_path, t0, t1, w, h, min_gap, max_probes)
    if not segs:
        return segs
    try:
        segs = _refine_with_change_flags(source_path, segs, t0, t1, min_gap, w, h)
    except Exception as e:  # noqa: BLE001 — refinement is best-effort
        log.warning("change-flag refinement failed (kept probe segments): %s", e)
    try:
        segs = _sanity_split_segments(source_path, segs, w, h, min_gap)
    except Exception as e:  # noqa: BLE001 — sanity split is best-effort
        log.warning("sanity split failed (kept segments): %s", e)
    try:
        segs = _polish_boundaries(source_path, segs, w, h, min_gap)
    except Exception as e:  # noqa: BLE001 — polish is best-effort
        log.warning("boundary polish failed (kept segments): %s", e)
    return segs


def _make_frame_differ(source_path: Path):
    """Shared change-flag helper: a `differ(ta, tb) -> True/False/None` closure
    over a frame cache, asking the model whether the panel GEOMETRY changed
    between two frames."""
    cache: dict = {}

    def frame(t: float):
        t = round(t, 2)
        if t not in cache:
            cache[t] = _extract_frame_b64(source_path, t)
        return cache[t]

    def differ(ta: float, tb: float):
        a, b = frame(ta), frame(tb)
        if not a or not b:
            return None
        ans = vision.compare(a, b, _CHANGE_PROBE_QUESTION)
        if not ans:
            return None
        up = ans.strip().upper()
        if up.startswith("SAME"):
            return False
        if "CHANGED" in up:
            return True
        return None

    return differ


def _sanity_split_segments(source_path: Path, segs: list, w: int, h: int,
                           min_gap: float, max_cuts: int = 4) -> list:
    """Probe-collapse backstop: when probe classification systematically fails
    over a stretch (seen on a second streamer — the content probe kept missing
    a dark vertical video, so every probe agreed on the SAME wrong label and
    the whole clip collapsed into one segment), no boundary exists for the
    refine/polish passes to fix. So every LONG segment (>12s) gets a few
    spread change-flag comparisons; a CHANGED pair is bisected and the segment
    is cut there — but ONLY when the two halves also CLASSIFY differently.
    That guard keeps person-movement false-CHANGEDs in fullscreen stretches
    from splitting anything (both halves classify 'full' → cut dropped)."""
    if not segs or not vision.enabled():
        return segs
    differ = _make_frame_differ(source_path)
    prec = max(0.3, float(min_gap))
    cuts = 0
    out: list = []
    work = [dict(s) for s in segs]
    while work:
        s = work.pop(0)
        span = s["t1"] - s["t0"]
        if span <= 12.0 or cuts >= max_cuts:
            out.append(s)
            continue
        pts = [s["t0"] + 0.5 + (span - 1.0) * k / 4.0 for k in range(5)]
        cut = None
        for a, b in zip(pts, pts[1:]):
            if not differ(a, b):
                continue
            lo, hi = a, b
            guard = 0
            while hi - lo > prec and guard < 12:
                mid = round((lo + hi) / 2.0, 2)
                if differ(lo, mid):
                    hi = mid
                else:
                    lo = mid
                guard += 1
            cand = round((lo + hi) / 2.0, 2)
            if not (s["t0"] + 1.0 < cand < s["t1"] - 1.0):
                continue
            # The cut only stands when the halves classify apart by MAJORITY —
            # a single classification per half let one flake (the content
            # probe boxing background furniture as 'content' → corroborated
            # split on a fullscreen frame) cut a clean 43s fullscreen stretch.
            # Split vs overlay is QA noise, not a real difference — grouped.
            def _grp(l):
                return ("panels", l[1]) if l[0] in ("split", "overlay") else (l[0], None)

            def _majority(a0, a1):
                labs = [_classify_layout(source_path, a0 + (a1 - a0) * f, w, h)
                        for f in (0.25, 0.5, 0.75)]
                grps = [_grp(l) for l in labs if l]
                if not grps:
                    return None, None
                best = max(set(grps), key=grps.count)
                if grps.count(best) < 2:
                    return None, None
                rep = next(l for l in labs if l and _grp(l) == best)
                return best, rep
            ga, la = _majority(s["t0"], cand)
            gb, lb = _majority(cand, s["t1"])
            if ga and gb and ga != gb:
                cut = (cand, la, lb)
                break
        if cut is None:
            out.append(s)
            continue
        cand, la, lb = cut
        cuts += 1
        left = dict(s, t1=cand, layout=la[0], side=la[1])
        right = dict(s, t0=cand, layout=lb[0], side=lb[1])
        out.append(left)
        work.insert(0, right)   # the right half may hide more switches
    return out


def _polish_boundaries(source_path: Path, segs: list, w: int, h: int,
                       min_gap: float) -> list:
    """Densely re-probe a window around every boundary between DIFFERENT-label
    segments and move the boundary to the actual flip point. Cures the 'box
    keeps following the previous segment' defect: a misplaced boundary holds
    the old geometry for seconds over the wrong frames, and the box detection
    can't self-correct because the filled {layout} prompt ASSERTS the (wrong)
    layout — the model obligingly draws the asserted panel even on a
    fullscreen frame. The geometric classifier here carries no such assertion.

    Window labels get the same inconclusive-fill + lone-flake smoothing as the
    probe grid (majority immunity a single bisection probe lacks). A run of a
    THIRD label between the two sides (e.g. a fullscreen meme between
    fullscreen-webcam and split) is inserted as its own segment. A window
    that's entirely one side slides toward the other (the boundary is further
    out than the window). A quick 2-frame check first skips the dense pass on
    boundaries that are already right."""
    if len(segs) < 2 or not vision.enabled():
        return segs
    step = max(0.4, float(min_gap))
    HALF = 2.4

    def classify_many(ts):
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            return list(ex.map(lambda t: _classify_layout(source_path, t, w, h), ts))

    so = [s["layout"] for s in segs if s["layout"] in ("split", "overlay")]
    maj = max(set(so), key=so.count) if so else None

    def norm(l):
        if not l:
            return None
        if l[0] in ("full", "fullcontent"):
            return (l[0], None)
        return (maj, l[1]) if maj else (l[0], l[1])

    out = [dict(s) for s in segs]
    i = 0
    while i < len(out) - 1:
        L, R = out[i], out[i + 1]
        Llab = (L["layout"], L["side"])
        Rlab = (R["layout"], R["side"])
        if Llab == Rlab:
            i += 1
            continue
        tb = R["t0"]
        lo_lim, hi_lim = L["t0"] + 0.3, R["t1"] - 0.3
        # quick check: both sides of the boundary already match their labels?
        qa, qb = max(lo_lim, tb - 0.5), min(hi_lim, tb + 0.5)
        quick = [norm(l) for l in classify_many([qa, qb])]
        if quick[0] == Llab and quick[1] == Rlab:
            i += 1
            continue
        for _attempt in range(3):
            lo, hi = max(lo_lim, tb - HALF), min(hi_lim, tb + HALF)
            if hi - lo < step:
                break
            ts = [round(lo + k * step, 2) for k in range(int((hi - lo) / step) + 1)]
            labs = [norm(l) for l in classify_many(ts)]
            known = [j for j, l in enumerate(labs) if l]
            if not known:
                break
            labs = [l if l else labs[min(known, key=lambda kj: abs(kj - j))]
                    for j, l in enumerate(labs)]
            for j in range(1, len(labs) - 1):   # lone-flake smoothing
                if labs[j] != labs[j - 1] and labs[j - 1] == labs[j + 1]:
                    labs[j] = labs[j - 1]
            lastL = max((j for j, l in enumerate(labs) if l == Llab), default=None)
            firstR = min((j for j, l in enumerate(labs) if l == Rlab), default=None)
            if lastL is not None and firstR is not None and lastL < firstR:
                mid = labs[lastL + 1:firstR]
                if (len(mid) >= 2 and mid[0] not in (Llab, Rlab)
                        and all(m == mid[0] for m in mid)):
                    # a hidden third state between the sides → own segment
                    m0 = round((ts[lastL] + ts[lastL + 1]) / 2.0, 2)
                    m1 = round((ts[firstR - 1] + ts[firstR]) / 2.0, 2)
                    L["t1"] = m0
                    out.insert(i + 1, {"t0": m0, "t1": m1,
                                       "layout": mid[0][0], "side": mid[0][1]})
                    R["t0"] = m1
                    i += 1            # the inserted segment's edges are fresh
                else:
                    newtb = round((ts[lastL] + ts[firstR]) / 2.0, 2)
                    L["t1"] = newtb
                    R["t0"] = newtb
                break
            if lastL is not None and firstR is None:
                tb = min(hi_lim, hi + HALF)   # whole window is L → slide right
            elif firstR is not None and lastL is None:
                tb = max(lo_lim, lo - HALF)   # whole window is R → slide left
            else:
                break                          # noise — keep the original
        i += 1
    return [s for s in out if s["t1"] - s["t0"] > 0.4]


def _refine_with_change_flags(source_path: Path, segs: list, t0: float, t1: float,
                              min_gap: float, w: int = 0, h: int = 0) -> list:
    """Comparison pass over probe-detected segments:
    1. Each boundary is straddled with one 'did anything change?' probe
       (±1.2s). SAME → the boundary was classification noise → the two
       segments merge (the longer side's label wins). This is what kills the
       spurious sub-second gap/flicker blips.
    2. Long split/overlay segments get one start-vs-end probe; CHANGED →
       the panel moved/resized mid-segment → bisect (by comparison) and split
       the segment in two same-label halves, each with its own size lock.
       Fullscreen segments are skipped — a person moving IS the frame there,
       so comparisons false-positive."""
    if not vision.enabled() or not segs:
        return segs
    differ = _make_frame_differ(source_path)
    prec = max(0.3, float(min_gap))

    # 1) boundary verification — merge across boundaries the model calls SAME
    out = [dict(segs[0])]
    for s in segs[1:]:
        b = s["t0"]
        la, lb = max(t0, b - 1.2), min(t1, b + 1.2)
        r = differ(la, lb) if (lb - la) >= 0.8 else None
        if r is False:
            prev = out[-1]
            if (s["t1"] - s["t0"]) > (prev["t1"] - prev["t0"]):
                prev["layout"], prev["side"] = s["layout"], s["side"]
            prev["t1"] = s["t1"]
        else:                      # CHANGED or inconclusive → trust the probes
            out.append(dict(s))

    # 2) movement flags inside long split/overlay segments. The flagged change
    #    can be a panel move (same layout) OR a misplaced probe boundary (the
    #    half before the cut is actually fullscreen) — so each half is
    #    RE-CLASSIFIED at its midpoint and relabeled when the probe disagrees.
    final: list = []
    for s in out:
        if s["layout"] not in ("split", "overlay") or (s["t1"] - s["t0"]) < 8.0:
            final.append(s)
            continue
        a, b = s["t0"] + 0.5, s["t1"] - 0.5
        if differ(a, b):
            lo, hi = a, b
            guard = 0
            while hi - lo > prec and guard < 12:
                mid = round((lo + hi) / 2.0, 2)
                if differ(lo, mid):
                    hi = mid
                else:
                    lo = mid
                guard += 1
            cut = round((lo + hi) / 2.0, 2)
            if s["t0"] + 1.0 < cut < s["t1"] - 1.0:
                # probe each half once — keep the original label only when the
                # half really is that layout (a flagged change right after a
                # boundary usually means the boundary itself was early)
                halves = []
                for ha, hb in ((s["t0"], cut), (cut, s["t1"])):
                    lab = (_classify_layout(source_path, (ha + hb) / 2.0, w, h)
                           if w and h else None)
                    seg = dict(s, t0=ha, t1=hb)
                    if lab and lab[0] in ("full", "fullcontent"):
                        seg["layout"], seg["side"] = lab[0], None
                    halves.append(seg)
                final.extend(halves)
                continue
        final.append(s)
    return final


def _segments_from_class_probes(source_path: Path, t0: float, t1: float, w: int, h: int,
                                min_gap: float = 1.0, max_probes: int = 9):
    """Fallback segmentation (the original path): probe spread frames, label
    each (layout, side), smooth isolated flaky labels, merge equal runs, refine
    boundaries by recursive bisection of CLASSIFICATIONS."""
    span = max(0.0, t1 - t0)
    n = min(max_probes, max(3, int(span // 6) + 2))
    pts = [t0 + span * (i + 0.5) / n for i in range(n)]   # interior — avoids cut frames
    if span > 4.0:
        # edge probes: clips often open/close on a fullscreen webcam for a few
        # seconds — interior probes alone never land there
        pts = [t0 + 0.3] + pts + [t1 - 0.3]
    labels = [(t, _classify_layout(source_path, t, w, h)) for t in pts]
    known = [(t, l) for t, l in labels if l]
    if not known:
        return None
    # SPLIT-vs-OVERLAY is decided GLOBALLY (majority vote): a probe occasionally
    # boxes the person instead of the panel → a small box → a spurious "overlay"
    # label, and one wrong layout phrase poisons that whole segment's detection.
    # Split/overlay switches mid-clip are rare. "full"/"fullcontent" labels are
    # NOT folded into the vote — fullscreen segments are real mid-clip events
    # (so are SIDE switches), and segmentation keys on both.
    so_votes = [l[0] for _, l in known if l[0] in ("split", "overlay")]
    majority_so = max(set(so_votes), key=so_votes.count) if so_votes else None

    def _norm(l):
        if not l:
            return None
        if l[0] in ("full", "fullcontent"):
            return (l[0], None)
        return (majority_so, l[1]) if majority_so else (l[0], l[1])

    labels = [(t, _norm(l)) for t, l in labels]
    known = [(t, l) for t, l in labels if l]
    # fill inconclusive probes from the nearest conclusive one
    filled = [(t, l if l else min(known, key=lambda kt: abs(kt[0] - t))[1])
              for t, l in labels]
    # kill isolated flakes: a probe disagreeing with BOTH equal neighbors flips;
    # same for a lone SIDE flake at either END — but an END probe whose layout
    # TYPE differs (full vs split/overlay) is kept: fullscreen intros/outros are
    # genuinely short and only the edge probe sees them (bisection + the dense
    # detection pass verify or shrink them later)
    for i in range(1, len(filled) - 1):
        if (filled[i][1] != filled[i - 1][1] and filled[i - 1][1] == filled[i + 1][1]):
            filled[i] = (filled[i][0], filled[i - 1][1])
    if len(filled) >= 3:
        if (filled[0][1] != filled[1][1] and filled[1][1] == filled[2][1]
                and filled[0][1][0] == filled[1][1][0]):
            filled[0] = (filled[0][0], filled[1][1])
        if (filled[-1][1] != filled[-2][1] and filled[-2][1] == filled[-3][1]
                and filled[-1][1][0] == filled[-2][1][0]):
            filled[-1] = (filled[-1][0], filled[-2][1])
    # merge equal runs; refine each disagreement boundary by RECURSIVE bisection:
    # a midpoint matching neither endpoint label is a state the probes never
    # landed on (e.g. a fullscreen-content stretch between a fullscreen-webcam
    # probe and a split probe) — recurse into both halves so it becomes its own
    # segment instead of being silently absorbed by the right-hand label.
    def _refine_boundary(ta, la, tb, lb):
        if tb - ta <= min_gap:
            return [((ta + tb) / 2.0, lb)]
        tm = (ta + tb) / 2.0
        lm = _norm(_classify_layout(source_path, tm, w, h))
        if lm is None or lm == la:
            return _refine_boundary(tm, la, tb, lb)
        if lm == lb:
            return _refine_boundary(ta, la, tm, lb)
        return _refine_boundary(ta, la, tm, lm) + _refine_boundary(tm, lm, tb, lb)

    boundaries = []   # (switch_time, new_label)
    cur = filled[0][1]
    for i in range(1, len(filled)):
        if filled[i][1] == cur:
            continue
        boundaries.extend(_refine_boundary(filled[i - 1][0], cur,
                                           filled[i][0], filled[i][1]))
        cur = filled[i][1]
    segs = []
    seg_start, seg_label = t0, filled[0][1]
    for bt, lbl in boundaries:
        segs.append({"t0": seg_start, "t1": bt, "layout": seg_label[0], "side": seg_label[1]})
        seg_start, seg_label = bt, lbl
    segs.append({"t0": seg_start, "t1": t1, "layout": seg_label[0], "side": seg_label[1]})
    # merge micro-segments (< 2s — almost certainly probe noise) into the previous
    merged = []
    for s in segs:
        if merged and ((s["t1"] - s["t0"]) < 2.0
                       or (merged[-1]["layout"], merged[-1]["side"]) == (s["layout"], s["side"])):
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append(s)
    # a micro FIRST segment can't merge backwards — fold it into the next one
    if len(merged) > 1 and (merged[0]["t1"] - merged[0]["t0"]) < 2.0:
        merged[1]["t0"] = merged[0]["t0"]
        merged.pop(0)
    return merged


def detect_layout_segments(source_path: Path, t0: float, t1: float, w: int, h: int,
                           min_gap: float = 0.2):
    """Public wrapper: probe a clip's layout timeline ONCE so both boxes can
    share it (independent per-box probing can disagree — e.g. box1 calling a
    stretch fullscreen-webcam while box2 calls it fullscreen-content → both
    boxes real at once and the reel shows the same frame twice)."""
    segs = _detect_layout_segments(source_path, t0, t1, w, h, min_gap=min_gap)
    # Boundary LEAD: a switch detected at the first frame that already shows the
    # new layout lands a touch LATE (the box holds the old framing into the new
    # shot — the perceptible "delay"). Nudge each interior boundary ~0.3s
    # EARLIER so the box switches just before the cut, clamped so no segment
    # collapses. A hair-early switch reads far better than a late one.
    if segs and len(segs) > 1:
        LEAD = 0.3
        for i in range(1, len(segs)):
            lo = segs[i - 1]["t0"] + 0.4           # keep the previous segment >= 0.4s
            new_t0 = max(lo, segs[i]["t0"] - LEAD)
            segs[i - 1]["t1"] = new_t0
            segs[i]["t0"] = new_t0
    return segs


def debounce_track(kfs: list, min_hold: float = 0.6, w: int = 0, h: int = 0) -> list:
    """Remove sub-second box EXCURSIONS: a brief change that reverts. The model
    momentarily resizes/moves the streamer box on a transient editing effect
    (a zoom punch, a funny overlay, a flash) — a keyframe that reigns < min_hold
    and whose box differs from BOTH the keyframe before and after it (which are
    themselves similar) is that flicker. Drop it; the previous box just holds
    through. Gap keyframes and genuine sustained changes are kept."""
    if not kfs or len(kfs) < 3 or not (w and h):
        return kfs
    diag = (w * w + h * h) ** 0.5

    def far(a, b):
        if a.get("gap") or b.get("gap"):
            return a.get("gap") != b.get("gap")
        ca = (a["x"] + a["w"] / 2.0, a["y"] + a["h"] / 2.0)
        cb = (b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0)
        moved = ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5 / diag > 0.04
        resized = abs(a["w"] - b["w"]) / max(1.0, w) > 0.05 or \
                  abs(a["h"] - b["h"]) / max(1.0, h) > 0.05
        return moved or resized

    out = list(kfs)
    changed = True
    while changed and len(out) >= 3:
        changed = False
        for i in range(1, len(out) - 1):
            prev, cur, nxt = out[i - 1], out[i], out[i + 1]
            hold = nxt["t"] - cur["t"]
            # cur is a short blip that differs from prev, and prev≈next → revert.
            # A DYNAMIC marker is intentional, never a blip — never debounce it
            # away; nor a MOVING (panning) kf, whose small per-kf steps ARE the pan.
            if (not cur.get("dynamic") and not cur.get("moving")
                    and hold < min_hold and far(prev, cur) and not far(prev, nxt)):
                out.pop(i)
                changed = True
                break
    return out


def settle_track(kfs: list, w: int, h: int, win: int = 5) -> list:
    """Final CALM pass. After the geometric snaps (resolve_split_overlap etc.) a
    box's size/position can FLICKER at layout transitions — e.g. box1 oscillating
    ~586<->1153 wide every 0.25s where the per-keyframe seam-snap lands
    inconsistently, which the per-segment size-lock (run earlier) never sees.
    Median-filter x/y/w/h over `win` within each RUN of consecutive non-gap,
    non-moving keyframes. Median is edge-preserving: a genuine sustained
    transition (535->1920) is kept, but lone/short flicker collapses to the local
    median. Gap kfs (segment boundaries) and moving kfs (a real pan — per-kf
    motion is intentional) are left untouched and act as run boundaries. Re-clamp
    + dedupe at the end."""
    if not kfs or len(kfs) < 3:
        return kfs
    out = [dict(k) for k in kfs]
    keys = ("x", "y", "w", "h")
    half = max(1, win // 2)

    def _full(k):
        # a WHOLE-frame box (synth_full's fullscreen owner) — must NEVER be median-
        # blended toward an adjacent split panel (that re-creates the half-cut-face
        # bug synth_full fixes). Treat it as a run boundary, like gap/moving.
        return (not k.get("gap")
                and k.get("x", 0.0) <= 1.0 and k.get("y", 0.0) <= 1.0
                and k.get("w", 0.0) >= 0.99 * w and k.get("h", 0.0) >= 0.99 * h)

    def _bound(k):
        return k.get("gap") or k.get("moving") or _full(k)

    i, n = 0, len(out)
    while i < n:
        if _bound(out[i]):
            i += 1
            continue
        j = i
        while j < n and not _bound(out[j]):
            j += 1
        run = out[i:j]                      # same dict refs as out
        if len(run) >= 3:
            med = []
            for idx in range(len(run)):
                lo, hi = max(0, idx - half), min(len(run), idx + half + 1)
                m = {}
                for k in keys:
                    vals = sorted(run[r][k] for r in range(lo, hi))
                    nn = len(vals)
                    m[k] = vals[nn // 2] if nn % 2 else (vals[nn // 2 - 1] + vals[nn // 2]) / 2.0
                med.append(m)
            for idx, kf in enumerate(run):    # apply AFTER computing every median
                m = med[idx]
                x = max(0.0, min(m["x"], w - 2.0))
                y = max(0.0, min(m["y"], h - 2.0))
                kf["x"], kf["y"] = round(x, 1), round(y, 1)
                kf["w"] = round(max(2.0, min(m["w"], w - x)), 1)
                kf["h"] = round(max(2.0, min(m["h"], h - y)), 1)
        i = j
    return _dedupe_keyframes(out)


def _fill_placeholders(prompt: str, layout, side) -> str:
    """Substitute {layout}/{side}/{other_side} for one segment. .replace, never
    .format — user prompts may contain other braces."""
    p = prompt
    if side:
        other = "right" if side == "left" else "left"
        p = p.replace("{side}", side).replace("{other_side}", other)
    elif layout in ("full", "fullcontent"):
        # no side in a fullscreen segment; "on the screen" reads fine in the
        # usual "panel on the {side}" context phrasing
        p = p.replace("{side}", "screen").replace("{other_side}", "screen")
    p = p.replace("{layout}", _LAYOUT_PHRASES.get(layout, "") if layout else "")
    return " ".join(p.split())


# Dynamic-segment thresholds: a stretch whose box MOVES or RESIZES a lot — not
# the sub-second flicker debounce_track removes, but sustained large motion — has
# no single box that represents it. Rather than emit a jittery guess we leave it
# empty (a gap) and flag it so the UI marks it for MANUAL drawing. Conservative on
# purpose: a normal talking head pans/zooms far less than this, so it still boxes.
_DYN_MOVE_FRAC = 0.12   # center wander (MAD / frame dim) past which it's "dynamic"
_DYN_SIZE_FRAC = 0.40   # size spread (p85-p15)/median past which it's "dynamic"


def _segment_is_dynamic(dets: list, w: int, h: int) -> bool:
    """True when a segment's detections move/resize so much that no locked box
    fairly represents it (sustained, not the brief reverts debounce_track drops).
    Measured on the post-outlier-rejection hits; too few hits to judge → not
    dynamic (let the normal pipeline box it)."""
    if len(dets) < 5:
        return False
    cxs = sorted(b["x"] + b["w"] / 2.0 for _, b in dets)
    cys = sorted(b["y"] + b["h"] / 2.0 for _, b in dets)
    mcx, mcy = cxs[len(cxs) // 2], cys[len(cys) // 2]
    madx = sorted(abs(c - mcx) for c in cxs)[len(cxs) // 2]
    mady = sorted(abs(c - mcy) for c in cys)[len(cys) // 2]
    if madx > w * _DYN_MOVE_FRAC or mady > h * _DYN_MOVE_FRAC:
        return True

    def spread(vals):
        vals = sorted(vals)
        n = len(vals)
        med = vals[n // 2]
        if med <= 0:
            return 0.0
        p15 = vals[max(0, int(n * 0.15))]
        p85 = vals[min(n - 1, int(n * 0.85))]
        return (p85 - p15) / med

    if (spread([b["w"] for _, b in dets]) > _DYN_SIZE_FRAC
            or spread([b["h"] for _, b in dets]) > _DYN_SIZE_FRAC):
        return True
    return False


def _segment_is_near_static(dets: list, w: int, h: int, frac: float = 0.03) -> bool:
    """True when the center BARELY moves (MAD <= ~frac of the frame) — essentially
    a pinned shot. Used to VETO a director 'subject_moving' only when the geometry
    is genuinely static (the js05 false-positive), while still trusting the director
    on a MODERATE pan that _segment_is_dynamic (threshold 0.12, and size-spread)
    would miss. Too few hits → not near-static (don't veto)."""
    if len(dets) < 5:
        return False
    cxs = sorted(b["x"] + b["w"] / 2.0 for _, b in dets)
    cys = sorted(b["y"] + b["h"] / 2.0 for _, b in dets)
    mcx, mcy = cxs[len(cxs) // 2], cys[len(cys) // 2]
    madx = sorted(abs(c - mcx) for c in cxs)[len(cxs) // 2]
    mady = sorted(abs(c - mcy) for c in cys)[len(cys) // 2]
    return madx <= w * frac and mady <= h * frac


def _track_segment(results: list, w: int, h: int, padding: float,
                   smooth: bool, lock_size: bool,
                   subject_moving: Optional[bool] = None,
                   head_room: float = 0.0):
    """The single-layout pipeline (outlier rejection → pad → smooth → size lock →
    static pin → interior bridge → keyframes), applied to ONE segment's samples.
    Returns (keyframes, raw_detected, dynamic).

    A segment whose subject MOVES a lot (sustained pan/zoom, or the director
    flagging `subject_moving`) is no longer left BLACK — it becomes a SMOOTHED,
    SIZE-LOCKED, CENTER-PANNING track (the static pin is skipped so the box
    follows the subject) with every non-gap kf tagged `moving=True`. That tag
    tells the downstream geometry snaps / debounce / hold-override to leave the
    pan alone. `subject_moving`: None = auto-detect via _segment_is_dynamic;
    True/False = explicit override (e.g. from the director). The returned
    `dynamic` is kept for call-site back-compat but is always False now — the
    panning track replaces the old black-slot behaviour the owner asked to drop
    ("dibuat aja dulu, nanti yang salah bisa saya hapus")."""
    det_items = [(t, b) for t, b in results if b]
    det_items = _reject_outliers(det_items)
    if padding and padding > 0:
        det_items = [(t, _pad(b, padding, w, h)) for t, b in det_items]
    if head_room and head_room > 0:
        # top-only headroom for the person box; constant offset → can't flip moving
        det_items = [(t, _headroom(b, head_room, w, h)) for t, b in det_items]
    raw_detected = len(det_items)
    # Decide whether this segment PANS (locked size, center follows — no static
    # pin) instead of pinning to one box. Director override wins; else auto-detect
    # sustained motion. Needs lock_size (adaptive mode already keeps the raw
    # moving track) and enough hits to trust the motion.
    if subject_moving is None:
        moving = bool(lock_size) and _segment_is_dynamic(det_items, w, h)
    else:
        # director hint: TRUST it unless the geometry is essentially PINNED — a
        # static talking-head the director wrongly flagged 'moving' (e.g. js05)
        # stays static, but a MODERATE director-confirmed pan (below the coarse
        # _segment_is_dynamic threshold) is still honoured.
        moving = (bool(subject_moving) and bool(lock_size) and raw_detected >= 5
                  and not _segment_is_near_static(det_items, w, h))
    if smooth and len(det_items) >= 3:
        det_items = _smooth(det_items)
    pinned = False
    if lock_size:
        det_items = _stabilize_size(det_items, w, h)
        if not moving:
            det_items, pinned = _lock_static(det_items, w, h)
    proc = {t: b for t, b in det_items}
    # Pinned static segment → the panel is there from first to last sighting;
    # interior misses are model noise, bridge them (boundary misses stay gaps).
    if pinned and det_items:
        t_first, t_last = det_items[0][0], det_items[-1][0]
        static_box = dict(det_items[0][1])
        for t, _ in results:
            if t_first < t < t_last and t not in proc:
                proc[t] = dict(static_box)
    seq = [(t, proc.get(t)) for t, _ in results]
    kfs, _ = _build_keyframes(seq, w, h)
    if moving:
        # tag the panning track so debounce / the hold-override / the split-
        # geometry snaps leave it alone, and the UI shows a 'TRACKED' chip.
        for k in kfs:
            if not k.get("gap"):
                k["moving"] = True
    # `dynamic` is retired (a moving subject pans instead of going black); always
    # return False so the caller never emits a black dynamic gap.
    return kfs, raw_detected, False


def classify_fullscreen_owner(source_path: Path, t: float, w: int, h: int):
    """What fills the screen at time t — 'streamer' / 'content' / None
    (inconclusive). Used to decide which box carries a fullscreen blip."""
    lab = _classify_layout(source_path, t, w, h)
    if not lab:
        return None
    return {"full": "streamer", "fullcontent": "content", "overlay": "content"}.get(lab[0])


def _fill_gap_interval(kfs: list, lo: float, hi: float, w: int, h: int) -> None:
    """In-place: turn [lo, hi) of the gap kf reigning over `lo` into a real
    full-frame hold, keeping any gap head before `lo` / tail after `hi`."""
    inf = float("inf")
    owner_i = max((i for i, k in enumerate(kfs)
                   if k.get("gap") and k["t"] <= lo + 1e-6),
                  key=lambda i: kfs[i]["t"], default=None)
    if owner_i is None:
        return
    owner = kfs[owner_i]
    a1 = kfs[owner_i + 1]["t"] if owner_i + 1 < len(kfs) else inf
    full = {"t": round(lo, 3), "x": 0.0, "y": 0.0, "w": float(w), "h": float(h),
            "interp": "hold", "fit": owner.get("fit", "cover"), "gap": False}
    if abs(owner["t"] - lo) < 1e-6:
        kfs[owner_i] = full                   # gap starts exactly here → replace
    else:
        kfs.append(full)                      # keep the gap head before lo
    if hi < a1 - 1e-6 and hi != inf:          # other box resumed before this gap ends
        tail = dict(owner)
        tail["t"] = round(hi, 3)
        kfs.append(tail)


def merge_double_gaps(kfs1: list, kfs2: list, w: int, h: int, classify=None) -> tuple:
    """Intervals where BOTH boxes are gap mean no panel was found anywhere on
    screen — a fullscreen blip too short for the layout probes (a comment card,
    a thread, a transition). One box must carry the FULL FRAME there or the
    reel renders black: `classify(t)` → 'streamer' puts it on box 1, 'content'
    (or inconclusive — blips are nearly always inserted content) on box 2.
    Returns (new_kfs1, new_kfs2). Box gap intervals never overlap other kfs of
    the same box, so each intersection lies inside exactly one gap reign."""
    if not kfs1 or not kfs2:
        return kfs1, kfs2
    inf = float("inf")

    def gap_intervals(kfs):
        # a DYNAMIC gap is deliberate (the user draws it) — never auto-fill it
        return [(k["t"], kfs[i + 1]["t"] if i + 1 < len(kfs) else inf)
                for i, k in enumerate(kfs) if k.get("gap") and not k.get("dynamic")]

    overlaps = []
    for a0, a1 in gap_intervals(kfs1):
        for b0, b1 in gap_intervals(kfs2):
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo > 1e-6:
                overlaps.append((lo, hi))
    if not overlaps:
        return kfs1, kfs2
    out1, out2 = list(kfs1), list(kfs2)
    for lo, hi in overlaps:
        mid = lo + min(hi - lo, 4.0) / 2.0    # hi can be inf — probe just inside
        owner = (classify(mid) if classify else None) or "content"
        _fill_gap_interval(out1 if owner == "streamer" else out2, lo, hi, w, h)
    out1.sort(key=lambda k: k["t"])
    out2.sort(key=lambda k: k["t"])
    return _dedupe_keyframes(out1), _dedupe_keyframes(out2)


def _active_kf(kfs: list, t: float):
    """The keyframe reigning at time t (the last kf whose t <= t), or None."""
    cur = None
    for k in kfs:
        if k["t"] <= t + 1e-6:
            cur = k
        else:
            break
    return cur


def _resolve_pair(T: dict, O: dict, min_overlap_px: float):
    """If box T overlaps box O as a side-by-side SPLIT (not a contained PiP),
    return T's (x, w) snapped off the overlap at the shared divider (overlap
    midpoint); else None. The divider formula is symmetric, so both boxes
    converge to the same seam."""
    Tx0, Tx1 = T["x"], T["x"] + T["w"]
    Ox0, Ox1 = O["x"], O["x"] + O["w"]
    lo, hi = max(Tx0, Ox0), min(Tx1, Ox1)
    if hi - lo <= min_overlap_px:                       # no real overlap
        return None
    if (Tx0 <= Ox0 and Tx1 >= Ox1) or (Ox0 <= Tx0 and Ox1 >= Tx1):
        return None                                     # containment → PiP/overlay, not a split
    if Tx0 <= Ox0:                                      # T is the LEFT panel → trim right edge
        divider = (Tx1 + Ox0) / 2.0
        return (Tx0, max(2.0, divider - Tx0))
    divider = (Ox1 + Tx0) / 2.0                         # T is the RIGHT panel → push left edge
    return (divider, max(2.0, Tx1 - divider))


def resolve_split_overlap(kfs1: list, kfs2: list, w: int, h: int,
                          min_overlap_px: float = 12.0) -> tuple:
    """Two side-by-side split panels must not overlap horizontally. The vision
    model often returns the streamer panel a touch too wide (bleeding into the
    content) and the content panel a touch too far in, so their crops double up
    on the seam — the visible "ngaceo" in a split. For every time both boxes are
    real and overlap as a true left/right split, snap both edges to the shared
    divider. No-op for fullscreen (one box gap), overlay/PiP (containment), or
    clean splits. Returns (new_kfs1, new_kfs2)."""
    if not kfs1 or not kfs2:
        return kfs1, kfs2
    orig1, orig2 = [dict(k) for k in kfs1], [dict(k) for k in kfs2]
    out1, out2 = [dict(k) for k in kfs1], [dict(k) for k in kfs2]
    for i, k in enumerate(out1):
        if k.get("gap") or k.get("moving"):   # never snap a panning track
            continue
        o = _active_kf(orig2, k["t"])
        if o and not o.get("gap"):
            r = _resolve_pair(orig1[i], o, min_overlap_px)
            if r:
                k["x"], k["w"] = r
    for i, k in enumerate(out2):
        if k.get("gap") or k.get("moving"):   # never snap a panning track
            continue
        o = _active_kf(orig1, k["t"])
        if o and not o.get("gap"):
            r = _resolve_pair(orig2[i], o, min_overlap_px)
            if r:
                k["x"], k["w"] = r
    return out1, out2


def expand_content_to_seam(kfs1: list, kfs2: list, w: int, h: int) -> list:
    """In a true left/right SPLIT, the content area IS the geometric complement
    of the streamer panel: everything from the shared seam to the frame edge
    ('edge to edge' — the owner's bbox_2 spec). The model frequently under-boxes
    the content (a poster inside the panel, half the panel, etc.), and no
    overlap-snap can fix an UNDERSIZED box — so whenever box1 (streamer) is a
    full-height, edge-anchored partial panel at some time, box2's box there is
    REPLACED with the complement rectangle. Fullscreen and overlay/PiP segments
    are untouched. Returns the new kfs2."""
    if not kfs1 or not kfs2:
        return kfs2
    inf = float("inf")

    def reign(kfs, i):
        return kfs[i]["t"], (kfs[i + 1]["t"] if i + 1 < len(kfs) else inf)

    def is_split_panel(b1):
        return (not b1.get("gap") and not b1.get("moving")
                and b1["w"] < 0.85 * w              # partial panel, not fullscreen
                and b1["h"] > 0.90 * h)             # full height → side-by-side split

    out = []
    for j, k in enumerate(kfs2):
        k = dict(k)
        if not k.get("gap") and not k.get("moving"):   # leave a panning box2 alone
            a2, b2 = reign(kfs2, j)
            # the box2 kf's reign can span several box1 segments (boundaries
            # don't always align) → use the box1 panel that overlaps it LONGEST
            best, best_ov = None, 0.0
            for i, b1 in enumerate(kfs1):
                if not is_split_panel(b1):
                    continue
                a1, b1e = reign(kfs1, i)
                ov = min(b2, b1e) - max(a2, a1)
                if ov > best_ov:
                    best, best_ov = b1, ov
            if best is not None and best_ov > 0:
                if best["x"] < 0.05 * w:            # streamer LEFT → content = seam..right edge
                    seam = best["x"] + best["w"]
                    if seam < w - 2:
                        k["x"], k["y"] = seam, 0.0
                        k["w"], k["h"] = w - seam, float(h)
                elif best["x"] + best["w"] > 0.95 * w:  # streamer RIGHT → content = left edge..seam
                    seam = best["x"]
                    if seam > 2:
                        k["x"], k["y"] = 0.0, 0.0
                        k["w"], k["h"] = seam, float(h)
        out.append(k)
    return out


def dedupe_fullframe_pair(kfs1: list, kfs2: list, w: int, h: int) -> list:
    """BOTH boxes real and BOTH ~full-frame over the same stretch = the same
    shot stacked twice in the reel (seen on a fullscreen tail: a stray
    'content panel' detection boxed the whole screen right before the full
    segment started). The streamer box owns a fullscreen shot by role
    convention, so box2's kf flips to gap there. Pure geometry, no model
    calls. Returns the new kfs2."""
    if not kfs1 or not kfs2:
        return kfs2
    inf = float("inf")

    def reign(kfs, i):
        return kfs[i]["t"], (kfs[i + 1]["t"] if i + 1 < len(kfs) else inf)

    def isfull(k):
        return ((not k.get("gap")) and not k.get("moving")
                and k["w"] >= 0.92 * w and k["h"] >= 0.92 * h)

    out = []
    for j, k in enumerate(kfs2):
        k = dict(k)
        if isfull(k):
            a2, b2 = reign(kfs2, j)
            for i, b1 in enumerate(kfs1):
                if not isfull(b1):
                    continue
                a1, b1e = reign(kfs1, i)
                if min(b2, b1e) - max(a2, a1) > 0.2:
                    k["gap"] = True
                    break
        out.append(k)
    return out


def dedupe_same_person(kfs1: list, kfs2: list, overlap_thresh: float = 0.6) -> list:
    """box1 and box2 are detected by INDEPENDENT model calls (no cross-box
    awareness), so on a solo shot the layout probe called a split — or any time
    only one subject is really on screen — box2 lands on the SAME subject as box1,
    and the reel shows that person cropped twice. Wherever box1 and box2 overlap
    so much that one is essentially inside the other (intersection / smaller-area
    > overlap_thresh), they are the same subject → drop box2 there (→ gap → the
    renderer's single-box full-focus shows just box1). Pure geometry, no model
    call. Run AFTER resolve_split_overlap (which snaps TRUE side-by-side splits to
    a seam, so two genuinely different people no longer overlap). Returns kfs2."""
    if not kfs1 or not kfs2:
        return kfs2

    def iou(a, b):
        # IoU (intersection / UNION) — high ONLY when the two boxes are similar
        # size AND position (the actual same-subject-twice case). NOT inter/min,
        # which hits 1.0 whenever box2 merely sits INSIDE a much larger box1
        # (a full-frame box1 over a content/2nd-person panel = a legit overlay,
        # must NOT gap box2). That inter/min bug was dropping box2 on podcasts.
        ix = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
        iy = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
        inter = ix * iy
        union = a["w"] * a["h"] + b["w"] * b["h"] - inter
        return inter / union if union > 0 else 0.0

    out = []
    for k in kfs2:
        k = dict(k)
        if not k.get("gap"):
            o = _active_kf(kfs1, k["t"])
            if o and not o.get("gap") and iou(o, k) > overlap_thresh:
                k["gap"] = True   # same subject as box1 (similar box) → box2 empty here
        out.append(k)
    return out


# ── Self-verify framing pass ───────────────────────────────────────────────
_OUT_W, _OUT_FULL_H, _TOP_H, _BOT_H = 1080, 1920, 720, 1200  # locked render layout
_VERIFY_MAX_FRAMES = 24   # (legacy; the geometric cameraman makes no VLM calls)
_VERIFY_VCROP_MAX = 0.22  # cover-crop > this fraction of box height → use blur_pad instead


def _box_runs(kfs: list):
    """Maximal runs [i0, i1] of consecutive NON-gap keyframes — one on-screen
    'segment' between gaps."""
    runs, i, n = [], 0, len(kfs)
    while i < n:
        if kfs[i].get("gap"):
            i += 1
            continue
        j = i
        while j + 1 < n and not kfs[j + 1].get("gap"):
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def verify_framing(source_path: Path, box1: list, box2: list, w: int, h: int,
                   descs: Optional[dict] = None,
                   full_focus_when_single: bool = True) -> tuple:
    """CAMERAMAN fit pass — purely GEOMETRIC, no VLM. (The old VLM judge was
    unreliable: it over-gapped box2 and rarely caught head-crop.) Per on-screen
    segment, compute how much a COVER crop into the slot the box renders in would
    clip off the box HEIGHT; if that exceeds _VERIFY_VCROP_MAX (a portrait person
    box in the wide top slot → head/chin lost), switch the segment to blur_pad,
    which CONTAINS the whole box (no crop; blurred bars fill the slot). It ONLY
    changes `fit` (cover↔blur_pad) — it NEVER gaps or resizes a box, so it can
    never drop a subject. Reliable + instant. source_path/descs kept for
    signature compatibility. Returns (box1, box2)."""
    if not (w and h):
        return box1, box2

    def other_gap_at(other, t):
        o = _active_kf(other, t) if other else None
        return (o is None) or bool(o.get("gap"))

    def is_full(k):
        return k["x"] <= 1 and k["y"] <= 1 and k["w"] >= 0.99 * w and k["h"] >= 0.99 * h

    def slot_for(boxnum, single):
        if single and full_focus_when_single:
            return _OUT_W, _OUT_FULL_H
        return (_OUT_W, _TOP_H) if boxnum == 1 else (_OUT_W, _BOT_H)

    def cover_vcrop(bw, bh, sw, sh):
        # fraction of the box HEIGHT a cover-crop would clip off in slot (sw, sh)
        if bw <= 0 or bh <= 0:
            return 0.0
        scaled_h = bh * max(sw / bw, sh / bh)
        return max(0.0, 1.0 - sh / scaled_h) if scaled_h > sh else 0.0

    def decide(boxnum, kfs, other):
        if not kfs:
            return kfs
        kfs = [dict(k) for k in kfs]
        for k in kfs:
            if k.get("gap") or k.get("moving") or is_full(k):
                continue
            single = other_gap_at(other, k["t"])
            sw, sh = slot_for(boxnum, single)
            if cover_vcrop(k["w"], k["h"], sw, sh) > _VERIFY_VCROP_MAX:
                k["fit"] = "blur_pad"   # cover would clip the head → contain the box
        return kfs

    nb1 = decide(1, box1, box2) if box1 else box1
    nb2 = decide(2, box2, nb1) if box2 else box2
    return nb1, nb2


# ── DIRECTOR review loop (Phase 4): editor + cameraman, revise from preview ──
_REVIEW_ROUNDS = 2        # max revise rounds per segment (cost cap)
_ZOOM_IN_F = 0.86         # tighter crop (subject bigger)
_ZOOM_OUT_F = 1.16        # looser crop (more context)
_UP_FRAC = 0.10           # raise the crop by this fraction of box height ("up")
_NEAR_FULL_AREA = 0.80    # box already ~full → zoom_out can't grow → blur instead


def _seg_idx(kfs: list, t0: float, t1: float) -> list:
    return [i for i, k in enumerate(kfs)
            if t0 - 1e-6 <= float(k.get("t", 0.0)) < t1 - 1e-6]


def _resize_center(k: dict, factor: float, w: int, h: int) -> dict:
    cx = k["x"] + k["w"] / 2.0
    cy = k["y"] + k["h"] / 2.0
    nw = min(float(w), max(8.0, k["w"] * factor))
    nh = min(float(h), max(8.0, k["h"] * factor))
    nx = min(max(0.0, cx - nw / 2.0), w - nw)
    ny = min(max(0.0, cy - nh / 2.0), h - nh)
    return {"x": nx, "y": ny, "w": nw, "h": nh}


def _editor_apply(kfs: list, t0: float, t1: float, action: str, w: int, h: int) -> bool:
    """EDITOR executes ONE bounded visual command on the kfs in [t0,t1). Only ever
    flips fit or resizes/shifts within the frame — NEVER gaps or deletes a box."""
    changed = False
    for i in _seg_idx(kfs, t0, t1):
        k = kfs[i]
        if k.get("gap"):
            continue
        # a deliberate WHOLE-frame box (synth_full's fullscreen owner) must keep its
        # geometry — resizing/shifting it re-creates the half-cut bug. Only a fit
        # change is allowed; "too tight" on a full box → show the whole frame padded.
        if (k["x"] <= 1.0 and k["y"] <= 1.0 and k["w"] >= 0.99 * w and k["h"] >= 0.99 * h
                and action in ("zoom_in", "zoom_out", "up")):
            if action == "zoom_out" and k.get("fit") != "blur_pad":
                k["fit"] = "blur_pad"; changed = True
            continue
        if action == "blur":
            if k.get("fit") != "blur_pad":
                k["fit"] = "blur_pad"; changed = True
        elif action == "cover":
            if k.get("fit") != "cover":
                k["fit"] = "cover"; changed = True
        elif action in ("zoom_in", "zoom_out"):
            area = (k["w"] * k["h"]) / float(w * h or 1)
            if action == "zoom_out" and area >= _NEAR_FULL_AREA:
                if k.get("fit") != "blur_pad":      # can't grow past the frame → show it all
                    k["fit"] = "blur_pad"; changed = True
                continue
            nb = _resize_center(k, _ZOOM_IN_F if action == "zoom_in" else _ZOOM_OUT_F, w, h)
            if abs(nb["w"] - k["w"]) > 1.0 or abs(nb["h"] - k["h"]) > 1.0:
                k.update(nb); changed = True
        elif action == "up":
            if k["y"] <= 1.0:                       # already at the top → can't raise → blur
                if k.get("fit") != "blur_pad":
                    k["fit"] = "blur_pad"; changed = True
                continue
            ny = max(0.0, k["y"] - k["h"] * _UP_FRAC)
            if ny != k["y"]:
                k["y"] = ny; changed = True
    return changed


def _cameraman_redetect(src: Path, kfs: list, t0: float, t1: float,
                        prompt: str, w: int, h: int) -> bool:
    """CAMERAMAN re-frames a box the director flagged as the WRONG subject: re-detect
    over the segment, size-lock a static box, and replace the segment's kfs with it.
    Returns False (keeps the old box) if nothing is detected — NEVER gaps."""
    if not (prompt or "").strip():
        return False
    found = []
    for f in (0.3, 0.5, 0.7):
        b = _extract_frame_b64(src, t0 + (t1 - t0) * f)
        if not b:
            continue
        d = vision.detect_box(b, prompt, w, h,
                              max_area_frac=_GATE_MAX_AREA, max_aspect=_GATE_MAX_ASPECT)
        if d:
            found.append(d)
    if not found:
        return False
    import statistics as _st
    mb = {q: float(_st.median([d[q] for d in found])) for q in ("x", "y", "w", "h")}
    idx = _seg_idx(kfs, t0, t1)
    real_idx = [i for i in idx if not kfs[i].get("gap")]
    if not real_idx:
        # segment is intentionally EMPTY (black) here — the subject is absent, so the
        # director has no business re-detecting a box into it. Leave the gap.
        return False
    # keep the FIRST real kf as the re-detected static box; drop the OTHER real kfs
    # but PRESERVE interior gap kfs (a gap = subject absent there, which redetect has
    # no evidence to overturn — deleting it would resurrect black stretches as boxes).
    target = real_idx[0]
    kfs[target].update(mb)
    kfs[target]["gap"] = False
    kfs[target]["moving"] = False
    kfs[target]["interp"] = "hold"
    for i in reversed([i for i in idx if i != target and not kfs[i].get("gap")]):
        kfs.pop(i)
    return True


def _active_full_kf(kfs: list, t: float) -> Optional[dict]:
    """A full COPY of the keyframe active at time t, retimed to EXACTLY t (so the
    owning segment's _seg_idx finds it). For a linear (panning) segment x/y/w/h are
    INTERPOLATED to t so the pan isn't flattened; gap/fit/interp/moving preserved.
    None if t precedes every kf. Used to SPLIT a held box at a segment boundary."""
    if not kfs:
        return None
    ks = sorted(kfs, key=lambda k: k.get("t", 0.0))
    if t < ks[0].get("t", 0.0) - 1e-6:
        return None
    cur, nxt = ks[0], None
    for a, b in zip(ks, list(ks[1:]) + [None]):
        if a.get("t", 0.0) <= t + 1e-6:
            cur, nxt = a, b
        else:
            break
    nk = dict(cur)
    nk["t"] = float(t)
    if (cur.get("interp") == "linear" and not cur.get("gap")
            and nxt and not nxt.get("gap")):
        span = float(nxt["t"]) - float(cur["t"])
        if span > 1e-6:
            f = (float(t) - float(cur["t"])) / span
            for q in ("x", "y", "w", "h"):
                nk[q] = float(cur[q]) + (float(nxt[q]) - float(cur[q])) * f
    return nk


def _split_at_boundaries(kfs: list, segs: list) -> None:
    """Insert a kf at every interior segment boundary (carrying the value held there)
    so each director segment owns at least one kf the editor/cameraman can edit in
    isolation. The kf is inserted at the EXACT boundary time (matching the t0 passed
    to _seg_idx). A boundary in a gap stretch inserts a GAP kf (segment stays empty —
    never materialises a box). In-place; the first segment's t0 is the clip start."""
    if not kfs or len(segs) < 2:
        return
    for s in segs[1:]:
        t0 = float(s.get("t0", 0.0))
        if any(abs(float(k.get("t", 0.0)) - t0) < 1e-3 for k in kfs):
            continue
        nk = _active_full_kf(kfs, t0)
        if nk is None:
            continue
        kfs.append(nk)
    kfs.sort(key=lambda k: k.get("t", 0.0))


def review_and_revise(source_path: Path, box1: list, box2: list, segs: list,
                      w: int, h: int, expect: str = "", p1: str = "", p2: str = "",
                      words: Optional[list] = None, turns: Optional[list] = None,
                      rounds: int = _REVIEW_ROUNDS) -> tuple:
    """Phase 4 — the DIRECTOR reviews the actual 9:16 CROP PREVIEW per segment and
    issues bounded revision commands to the EDITOR (blur/cover/zoom/up) and CAMERAMAN
    (redetect a wrong subject), looping up to `rounds` times until the shot is good.
    Mutates copies of box1/box2 and returns (box1, box2, note). No-op (returns inputs)
    when vision is off or there are no director segments. The editor/cameraman can
    only ADJUST a box, never gap/delete it — so this can only improve framing."""
    if not vision.enabled() or not segs or not (w and h):
        return box1, box2, ""
    b1 = [dict(k) for k in (box1 or [])]
    b2 = [dict(k) for k in (box2 or [])]
    # Give each segment its OWN editable kf(s) so a box held across segments can be
    # revised in one segment without dragging the others (the held-box editing gap).
    _split_at_boundaries(b1, segs)
    _split_at_boundaries(b2, segs)
    words = words or []
    turns = turns or []
    try:
        import diarize as _diar          # pure-python dominant_speaker (no pyannote)
    except Exception:  # noqa: BLE001
        _diar = None
    acts: list = []

    def sample_times(t0, t1):
        span = t1 - t0
        fr = [0.2, 0.5, 0.8] if span >= 1.2 else ([0.33, 0.66] if span >= 0.6 else [0.5])
        return [round(t0 + span * f, 3) for f in fr]

    def composite_at(tm):
        a1 = _box_at(b1, tm); a2 = _box_at(b2, tm)
        if not a1 and not a2:
            return None
        return _composite_preview_b64(source_path, tm, a1, a2)

    for seg in segs:
        t0 = float(seg.get("t0", 0.0)); t1 = float(seg.get("t1", t0 + 1.0))
        if t1 - t0 < 0.4:
            continue
        desc = (seg.get("box1_desc") or p1 or "").strip()
        mid = round((t0 + t1) / 2.0, 3)
        # SOUND access for the review director: what is SAID in this shot + who is
        # speaking (so it can judge whether the RIGHT subject is framed, not just the
        # geometry). transcript = active; speaker = needs diarization (HF-gated).
        tr = " ".join(x.get("word", "") for x in words
                      if x.get("start", 0.0) < t1 and x.get("end", 0.0) > t0).strip()
        spk = None
        if turns and _diar is not None:
            try:
                spk = _diar.dominant_speaker(turns, t0, t1)
            except Exception:  # noqa: BLE001
                spk = None
        orig = _extract_frame_b64(source_path, mid)   # context frame: once per segment
        if not orig:
            continue
        acted: set = set()   # (bx, action-class) already applied here — no flip-flop
        for _rnd in range(max(1, rounds)):
            times = sample_times(t0, t1)
            # build the SEQUENCE of crop previews across the shot (one review call
            # sees motion, like the main director's window — not one noisy frame)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                previews = [p for p in ex.map(composite_at, times) if p]
            if not previews:
                break
            v = vision.review_shot(orig, previews, expect=expect,
                                   box1_desc=desc, box2_desc=(p2 or ""),
                                   transcript=tr, main_speaker=spk)
            if not isinstance(v, dict):
                break
            changed = False
            for bx, kfs in ((1, b1), (2, b2)):
                # only revise a box that actually OWNS a real kf in THIS segment
                # (not one merely clamped in from before t0 / after t1)
                if not any(not kfs[i].get("gap") for i in _seg_idx(kfs, t0, t1)):
                    continue
                cmd = v.get(f"box{bx}")
                if not isinstance(cmd, dict):
                    continue
                act = (cmd.get("action") or "keep").strip().lower()
                agent = (cmd.get("agent") or "").strip().lower()
                if act in ("keep", ""):
                    continue
                cls = ("zoom" if act in ("zoom_in", "zoom_out")
                       else "fit" if act in ("blur", "cover") else act)
                if (bx, cls) in acted:
                    continue   # already applied this class on this box → no oscillation
                prompt = (desc or p1) if bx == 1 else p2
                if agent == "cameraman" or act == "redetect":
                    if _cameraman_redetect(source_path, kfs, t0, t1, prompt, w, h):
                        changed = True; acted.add((bx, "redetect"))
                        acts.append(f"{mid:.0f}s b{bx}:redetect")
                elif _editor_apply(kfs, t0, t1, act, w, h):
                    changed = True; acted.add((bx, cls))
                    acts.append(f"{mid:.0f}s b{bx}:{act}")
            if not changed:
                break
    # collapse any boundary kfs we inserted but never edited (identical neighbours)
    b1 = _dedupe_keyframes(b1)
    b2 = _dedupe_keyframes(b2)
    # HARD INVARIANT safety net: a box that HAD real content must never come out
    # empty/all-gap. If it somehow did, revert that box to its pre-review input.
    def _has_real(kk):
        return any(not k.get("gap") for k in (kk or []))
    if _has_real(box1) and not _has_real(b1):
        b1 = [dict(k) for k in box1]; acts.append("b1:reverted")
    if _has_real(box2) and not _has_real(b2):
        b2 = [dict(k) for k in box2]; acts.append("b2:reverted")
    note = ("review: " + " · ".join(acts[:8])) if acts else "review: no changes"
    return b1, b2, note


# ── Windowed shot-director pre-pass (Phase 2) ──────────────────────────────
_DIR_WIN = 2.5         # director window width (s)
_DIR_STEP = 1.0        # director window hop (s)
_DIR_FRAMES = 5        # frames shown to the director per window
_DIR_LEAD = 0.3        # DEPRECATED — boundaries are now CENTRED on the change, not nudged early
_DIR_MAX_WINDOWS = 48  # cost cap: widen the hop on long clips
_DIR_MIN_SEG = 1.5     # merge segments shorter than this (kills full<->split flicker)
_DIR_SIDE_MIN_WINDOWS = 1  # consecutive opposite-side windows before box1 flips side (debounce)


def run_director(source_path: Path, t0: float, t1: float, w: int, h: int,
                 prompt: str, words: Optional[list] = None,
                 turns: Optional[list] = None, expect: str = "") -> tuple:
    """Phase 2 'shot director' pre-pass. Slide a ~2.5s window over [t0,t1]; per
    window show vision.director() K frames + the transcript slice + the dominant
    speaker (from diarization `turns`, if any), then reconcile the per-window
    verdicts into a CONTIGUOUS segment timeline of the same shape
    detect_layout_segments emits, plus extra keys `moving` (pan this segment) and
    `box1_desc` (who/what box1 is — appended to box1's prompt by predict_track).
    Returns (segments, note). segments == [] when the director produced nothing
    (the caller then falls back to the geometric segmenter / single segment)."""
    if not vision.enabled() or t1 <= t0:
        return [], ""
    words = words or []
    turns = turns or []
    span = t1 - t0
    hop = max(_DIR_STEP, span / _DIR_MAX_WINDOWS)
    starts = []
    s = t0
    while s < t1 - 1e-3:
        starts.append(round(s, 3))
        s += hop
    if not starts:
        starts = [t0]

    cache: dict = {}

    def frame(t):
        k = round(t, 2)
        if k not in cache:
            cache[k] = _extract_frame_b64(source_path, k)
        return cache[k]

    try:
        import diarize as _diar          # pure-python dominant_speaker (no pyannote)
    except Exception:  # noqa: BLE001
        _diar = None

    def one_window(ws: float):
        we = min(ws + _DIR_WIN, t1)
        pts = [ws + (we - ws) * (i + 0.5) / _DIR_FRAMES for i in range(_DIR_FRAMES)]
        frames = [f for f in (frame(t) for t in pts) if f]
        if not frames:
            return None
        tr = " ".join(x.get("word", "") for x in words
                      if x.get("start", 0.0) < we and x.get("end", 0.0) > ws).strip()
        spk = None
        if turns and _diar is not None:
            try:
                spk = _diar.dominant_speaker(turns, ws, we)
            except Exception:  # noqa: BLE001
                spk = None
        v = vision.director(frames, prompt=prompt, transcript=tr, main_speaker=spk,
                            expectation=expect)
        return {"t0": ws, "t1": we, "v": v} if isinstance(v, dict) else None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        verdicts = [r for r in ex.map(one_window, starts) if r]
    if not verdicts:
        return [], ""

    def conf(v):
        try:
            return float(v.get("confidence", 0.5) or 0.5)
        except (TypeError, ValueError):
            return 0.5

    # split-vs-overlay = ONE global confidence-weighted vote (a single flaky probe
    # must not poison the clip); fullscreen labels stay per-window (real events).
    split_w = sum(conf(r["v"]) for r in verdicts if r["v"].get("layout") == "split")
    overlay_w = sum(conf(r["v"]) for r in verdicts if r["v"].get("layout") == "overlay")
    two_lay = "overlay" if overlay_w > split_w else "split"

    def norm_layout(lay):
        return lay if lay in ("full", "fullcontent") else two_lay

    # PER-WINDOW box1_side with DEBOUNCE: box1 FOLLOWS whoever is speaking in a
    # 2-shot, but a side flip commits only after _DIR_SIDE_MIN_WINDOWS consecutive
    # opposite windows (so it doesn't flicker every ~1s). None/'screen' windows
    # inherit the current side. Seed from the global majority, else 'left' (a
    # split/overlay segment always needs a concrete side so {side}/{other_side}
    # never leak literally into the prompt).
    raw_sides = [r["v"].get("box1_side") if r["v"].get("box1_side") in ("left", "right")
                 else None for r in verdicts]
    decisive = [s for s in raw_sides if s]
    cur_side = max(set(decisive), key=decisive.count) if decisive else "left"
    stable_sides, pend = [], 0
    for s in raw_sides:
        if s and s != cur_side:
            pend += 1
            if pend >= _DIR_SIDE_MIN_WINDOWS:
                cur_side, pend = s, 0
        elif s == cur_side:
            pend = 0
        stable_sides.append(cur_side)

    # Build segments: merge consecutive windows on (layout, side). subject_moving is
    # a CONFIDENCE-WEIGHTED vote accumulated across the segment's windows (mov_w /
    # tot_w), NOT an OR of any one window — one eager window must not flag the whole
    # segment as panning. box1_desc follows the speaker per-segment (first non-empty).
    segs: list = []
    for r, st_side in zip(verdicts, stable_sides):
        v = r["v"]
        lay = norm_layout(v.get("layout"))
        side = st_side if lay in ("split", "overlay") else None
        desc = (v.get("box1_desc") or "").strip()
        c = conf(v)
        mov = c if v.get("subject_moving") else 0.0
        if segs and segs[-1]["layout"] == lay and segs[-1]["side"] == side:
            segs[-1]["t1"] = r["t1"]
            segs[-1]["_mov"] += mov
            segs[-1]["_tot"] += c
            if not segs[-1]["box1_desc"] and desc:
                segs[-1]["box1_desc"] = desc
        else:
            segs.append({"t0": r["t0"], "t1": r["t1"], "layout": lay, "side": side,
                         "box1_desc": desc, "_mov": mov, "_tot": c})

    # MIN-SEGMENT-DURATION merge: collapse <_DIR_MIN_SEG flicker (e.g. a 1s split
    # blip between two full shots) into its larger-duration neighbour, which absorbs
    # the span + the moving vote and keeps its own layout/side/desc. Duration is
    # GAP-TO-NEXT (segs[i+1].t0 - segs[i].t0), NOT t1-t0 (every single-window seg has
    # t1-t0 == _DIR_WIN so t1-t0 would never trip).
    def _dur(i):
        return (segs[i + 1]["t0"] - segs[i]["t0"]) if i + 1 < len(segs) \
            else (segs[i]["t1"] - segs[i]["t0"])
    while len(segs) > 1:
        short = [i for i in range(len(segs)) if _dur(i) < _DIR_MIN_SEG]
        if not short:
            break
        i = min(short, key=_dur)
        cands = [j for j in (i - 1, i + 1) if 0 <= j < len(segs)]
        nb = max(cands, key=lambda j: (_dur(j),
                 int(segs[j]["layout"] == segs[i]["layout"] and segs[j]["side"] == segs[i]["side"])))
        keep, drop = segs[nb], segs[i]
        keep["t0"] = min(keep["t0"], drop["t0"])
        keep["t1"] = max(keep["t1"], drop["t1"])
        keep["_mov"] += drop["_mov"]
        keep["_tot"] += drop["_tot"]
        segs.pop(i)

    # finalize the moving flag by majority of the (merged) window mass, drop scratch
    for s in segs:
        s["moving"] = (s["_mov"] > 0.5 * s["_tot"]) if s["_tot"] > 0 else False
        s.pop("_mov", None)
        s.pop("_tot", None)

    # CONTIGUOUS boundaries placed at the CHANGE, not the window start. Each window
    # votes ONE label over its whole forward-looking [ws, ws+_DIR_WIN] span, so it
    # flips to the NEW state as soon as NEW dominates that span — i.e. the real cut
    # is ~(_DIR_WIN-hop)/2 AFTER the first NEW window's start, not before it. The old
    # `- _DIR_LEAD` nudge made the box switch ~1s too EARLY (owner: "belum waktunya
    # tapi bbox udah keganti duluan"). So push each boundary FORWARD by that centring
    # offset, clamped to leave >=0.4s on both sides.
    off = max(0.0, _DIR_WIN / 2.0 - hop / 2.0)
    for i in range(1, len(segs)):
        lo = segs[i - 1]["t0"] + 0.4
        hi = segs[i]["t1"] - 0.4
        b = segs[i]["t0"] + off
        b = max(lo, min(b, hi)) if hi > lo else lo
        segs[i - 1]["t1"] = b
        segs[i]["t0"] = b
    segs[0]["t0"] = t0
    segs[-1]["t1"] = t1
    note = "director: " + " · ".join(
        f"{s['layout']}{('(' + s['side'] + ')') if s['side'] else ''} "
        f"{s['t0']:.1f}–{s['t1']:.1f}s{' [pan]' if s['moving'] else ''}"
        for s in segs)
    return segs, note


def predict_track(
    source_path: Path,
    prompt: str,
    t_start: float = 0.0,
    t_end: Optional[float] = None,
    step_seconds: float = 0.4,
    padding: float = 0.05,
    smooth: bool = True,
    lock_size: bool = True,
    role: Optional[str] = None,
    segments: Optional[list] = None,
    use_director: bool = False,
    words: Optional[list] = None,
    turns: Optional[list] = None,
    head_room: float = 0.0,
    expect: str = "",
) -> dict:
    """Predict a box track over [t_start, t_end]. Returns
    {keyframes, sampled, detected, width, height, segments}. With `lock_size`
    (default), the box SIZE is locked and only the center pans — per LAYOUT
    SEGMENT: when the prompt carries {side}/{other_side}/{layout} placeholders,
    the clip is first probed for layout changes (the cam can move mid-clip) and
    each segment gets its own resolved prompt, its own size lock/static pin, and
    a keyframe exactly at the refined switch time.

    `step_seconds` is the temporal PRECISION (how exactly switches/motion are
    timed), not a uniform sampling rate: detection runs on a ~1s coarse grid
    and ADAPTIVELY subdivides only where something changes — position, size or
    hit/miss — down to `step_seconds`. A static panel costs the same at 0.2s
    precision as at 1.5s.

    `role` ("streamer"/"content"/None) tells fullscreen segments what this box
    IS: in a fullscreen-webcam segment the streamer box becomes the full frame
    and the content box a gap; in a fullscreen-content segment (a thread, a
    meme) the reverse — no model call (asserting "box the content panel" on a
    frame that has none just makes the model hallucinate one). With role=None
    the model is asked anyway, with the fullscreen layout phrase filled in."""
    w, h, dur = _probe(source_path)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not probe source dimensions")

    t0 = max(0.0, float(t_start or 0.0))
    t1 = float(t_end) if t_end is not None else (dur or t0 + 10.0)
    if dur:
        t1 = min(t1, dur)
    step = max(0.2, float(step_seconds or 0.2))
    if t1 <= t0:
        t1 = min(t0 + step, dur or t0 + step)
    times, eff_step, capped = _sample_times(t0, t1, max(step, 1.0))

    # Layout/side resolution per SEGMENT (the model needs concrete anchors —
    # measured: median width 1030 with a stated side vs 1206-1679 without; and
    # the geometric probe beats asking "left or right?" which flips when the
    # content also shows people talking to camera).
    has_ph = any(tok in prompt for tok in ("{side}", "{other_side}", "{layout}"))
    director_note = ""
    if segments is not None:
        # caller supplies a shared, precomputed timeline (one probe pass for
        # both boxes — e.g. the batch director runs once for both) — copy it:
        # detection feedback below mutates boundaries
        segments = [dict(s) for s in segments]
    else:
        if use_director and vision.enabled():
            # interactive path: run the windowed director here (the batch path
            # runs it once upstream and passes segments=)
            segments, director_note = run_director(source_path, t0, t1, w, h,
                                                   prompt, words=words, turns=turns,
                                                   expect=expect)
        else:
            segments = []
        if not segments:        # director off / produced nothing → geometric path
            if has_ph:
                segments = _detect_layout_segments(source_path, t0, t1, w, h,
                                                   min_gap=step)
                if not segments:
                    s = _resolve_side(source_path, times, w, h)
                    segments = [{"t0": t0, "t1": t1, "layout": "split", "side": s}]
            else:
                segments = [{"t0": t0, "t1": t1, "layout": None, "side": None}]
    # box1_desc (the director's WHO/WHAT for box1) is appended to box1's prompt
    # only — never box2 (role='content'). The PLAIN (layout-only) prompts are used
    # by the boundary-correction redetect so it never applies the wrong person's
    # identity to boundary frames.
    seg_prompts_plain = [_fill_placeholders(prompt, s["layout"], s["side"]) for s in segments]
    seg_prompts = list(seg_prompts_plain)
    if role != "content":
        for i, s in enumerate(segments):
            desc = (s.get("box1_desc") or "").strip()
            if desc:
                seg_prompts[i] = (seg_prompts[i] + ". Focus on " + desc).strip()

    def seg_idx_of(t: float) -> int:
        for i in range(len(segments) - 1, -1, -1):
            if t >= segments[i]["t0"] - 1e-9:
                return i
        return 0

    def synth_full(seg_i: int):
        """Fullscreen segment + known role → deterministic answer, no model
        call: whichever of streamer/content fills the screen IS the whole
        frame, the other box is a gap. Returns (handled, detection_or_None)."""
        lay = segments[seg_i]["layout"]
        if lay in ("full", "fullcontent") and role in ("streamer", "content"):
            # A fullscreen subject IS the whole frame even if the director flagged
            # it 'moving' — you cannot pan a box that already spans the frame, and
            # leaving it to detect_box yields a stale partial box (the streamer's
            # split panel reused full-screen → half-cut face). owner → whole frame,
            # the other box → gap.
            owner = "streamer" if lay == "full" else "content"
            det = {"x": 0.0, "y": 0.0, "w": float(w), "h": float(h)} if role == owner else None
            return True, det
        return False, None

    def work(t: float):
        handled, det = synth_full(seg_idx_of(t))
        if handled:
            return (t, det)
        b64 = _extract_frame_b64(source_path, t)
        if not b64:
            return (t, None)
        return (t, vision.detect_box(b64, seg_prompts[seg_idx_of(t)], w, h,
                                     max_area_frac=_GATE_MAX_AREA, max_aspect=_GATE_MAX_ASPECT))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(work, times))
    results.sort(key=lambda r: r[0])

    # ADAPTIVE REFINEMENT down to `step`: the coarse grid is enough where
    # nothing changes (a pinned panel needs ONE box), but transitions — layout
    # switches, gap edges, a moving cam — deserve `step` precision. Subdivide
    # every adjacent pair whose state differs (hit/miss flip, center moved >2%,
    # size changed >3%) until the pair is <= step apart. Costs a handful of
    # calls per transition instead of a 5x denser uniform grid.
    if step < eff_step - 1e-9:
        def _differs(a, b):
            if (a is None) != (b is None):
                return True
            if a is None:
                return False
            return (abs((a["x"] + a["w"] / 2) - (b["x"] + b["w"] / 2)) > 0.02 * w
                    or abs((a["y"] + a["h"] / 2) - (b["y"] + b["h"] / 2)) > 0.02 * h
                    or abs(a["w"] - b["w"]) > 0.03 * w
                    or abs(a["h"] - b["h"]) > 0.03 * h)

        res_map = dict(results)
        budget = MAX_FRAMES   # refinement gets its own frame budget
        frontier = [(ta, tb) for (ta, _), (tb, _) in zip(results, results[1:])
                    if tb - ta > step + 1e-9 and _differs(res_map[ta], res_map[tb])]
        while frontier and budget > 0:
            mids = []
            for ta, tb in frontier:
                tm = round((ta + tb) / 2.0, 3)
                if tm not in res_map and budget > 0:
                    mids.append((ta, tm, tb))
                    budget -= 1
            if not mids:
                break
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                got = list(ex.map(lambda m: work(m[1]), mids))
            frontier = []
            for (ta, tm, tb), (_, bm) in zip(mids, got):
                res_map[tm] = bm
                if tm - ta > step + 1e-9 and _differs(res_map[ta], bm):
                    frontier.append((ta, tm))
                if tb - tm > step + 1e-9 and _differs(bm, res_map[tb]):
                    frontier.append((tm, tb))
        results = sorted(res_map.items())

    # Boundary correction by DETECTION FEEDBACK: the coarse probes can lie (the
    # content shows people too), but the dense sample grid is ground truth — a
    # contiguous MISS run touching a segment edge usually means the boundary is
    # wrong and those frames belong to the neighboring segment. Re-detect such
    # runs with the neighbor's prompt; where they hit, move the boundary to the
    # first hit. This pins the switch to within one sample step regardless of
    # probe flakiness.
    if len(segments) > 1:
        res_by_t = dict(results)

        def redetect(t: float, seg_i: int):
            handled, det = synth_full(seg_i)
            if handled:
                return det
            b64 = _extract_frame_b64(source_path, t)
            # layout-only prompt (no director box1_desc) for boundary feedback
            return vision.detect_box(b64, seg_prompts_plain[seg_i], w, h,
                                     max_area_frac=_GATE_MAX_AREA,
                                     max_aspect=_GATE_MAX_ASPECT) if b64 else None

        for i in range(len(segments) - 1):
            seg_times = [t for t, _ in results
                         if segments[i]["t0"] - 1e-9 <= t < segments[i + 1]["t0"] - 1e-9]
            # trailing misses of segment i → maybe they're already segment i+1.
            # Skip when segment i's results are SYNTHESIZED (full + role): a
            # content box misses the whole segment by construction — that's not
            # evidence the boundary is wrong.
            run = []
            if not synth_full(i)[0]:
                for t in reversed(seg_times):
                    if res_by_t.get(t) is None:
                        run.insert(0, t)
                    else:
                        break
            if run:
                hits = {t: redetect(t, i + 1) for t in run}
                hit_times = sorted(t for t, b in hits.items() if b)
                if hit_times:
                    boundary = hit_times[0]
                    segments[i + 1]["t0"] = boundary
                    segments[i]["t1"] = boundary
                    for t in hit_times:
                        res_by_t[t] = hits[t]
            # leading misses of segment i+1 → maybe they're still segment i
            # (same synth guard: a synthesized all-miss segment proves nothing)
            next_times = [t for t, _ in results if t >= segments[i + 1]["t0"] - 1e-9]
            lead = []
            if not synth_full(i + 1)[0]:
                for t in next_times:
                    if res_by_t.get(t) is None:
                        lead.append(t)
                    else:
                        break
            if lead:
                hits = {t: redetect(t, i) for t in lead}
                hit_times = sorted(t for t, b in hits.items() if b)
                if hit_times:
                    last_hit = hit_times[-1]
                    new_t0 = next((t for t, _ in results if t > last_hit), segments[i + 1]["t0"])
                    segments[i + 1]["t0"] = max(segments[i + 1]["t0"], new_t0)
                    segments[i]["t1"] = segments[i + 1]["t0"]
                    for t in hit_times:
                        res_by_t[t] = hits[t]
        results = sorted(res_by_t.items())
        # drop segments that lost all their samples to a moved boundary
        segments = [s for s in segments
                    if any(s["t0"] - 1e-9 <= t < s["t1"] + 1e-9 for t, _ in results)] or segments

    # Post-process PER SEGMENT (its own outlier stats, size lock, static pin —
    # a layout switch must not blend sizes/positions across the boundary), then
    # snap each segment's first keyframe to the refined switch time so the box
    # changes exactly when the layout does.
    keyframes: list = []
    detected = 0
    for i, seg in enumerate(segments):
        seg_results = [(t, b) for t, b in results if seg_idx_of(t) == i]
        if not seg_results:
            continue
        kfs, rd, _dyn = _track_segment(seg_results, w, h, padding, smooth,
                                       lock_size, subject_moving=seg.get("moving"),
                                       head_room=(head_room if role in ("streamer", None) else 0.0))
        detected += rd
        if kfs:
            kfs[0]["t"] = round(max(t0, seg["t0"]), 3)
            keyframes.extend(kfs)
        elif not keyframes or not keyframes[-1]["gap"]:
            # segment with zero detections (e.g. the content box during a
            # fullscreen-webcam segment) → explicit gap kf, otherwise the
            # renderer holds the previous segment's box across the absence
            ref = keyframes[-1] if keyframes else {
                "x": 0.0, "y": 0.0, "w": float(max(2, w)), "h": float(max(2, h)),
                "fit": "cover"}
            keyframes.append({"t": round(max(t0, seg["t0"]), 3),
                              "x": ref["x"], "y": ref["y"], "w": ref["w"], "h": ref["h"],
                              "interp": "hold", "fit": ref.get("fit", "cover"),
                              "gap": True})
    keyframes = _dedupe_keyframes(keyframes)

    # Per-box defaults (owner's spec): box1/streamer = hold + cover, box2/content
    # = hold + blur_pad (content panels keep their full AR, blurred padding).
    # hold > linear as default — panels are static; the user edits per-kf anyway.
    if role in ("streamer", "content"):
        fit_default = "cover" if role == "streamer" else "blur_pad"
        for k in keyframes:
            if not k.get("gap"):
                k["fit"] = fit_default
                # a moving (panning) track keeps its linear interp — forcing hold
                # would step the pan; only static boxes get the hold default.
                if not k.get("moving"):
                    k["interp"] = "hold"

    # Final calm pass: median-filter size/position so transition flicker collapses
    # (interactive single-box path; the batch 2-box path settles again after its
    # geometric snaps, which is where the worst flicker is introduced).
    keyframes = settle_track(keyframes, w, h)

    return {
        "keyframes": keyframes,
        "sampled": len(results),
        "detected": detected,
        "width": w,
        "height": h,
        "step": round(step, 2),       # the precision actually honored
        "capped": capped,
        "side": segments[0]["side"],   # first segment's side (None w/o placeholders)
        "segments": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2),
                      "layout": s["layout"], "side": s["side"]} for s in segments],
        "director_note": director_note,
    }
