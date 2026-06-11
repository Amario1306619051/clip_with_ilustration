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


_SIDE_PROBE_PROMPT = ("the streamer's webcam panel — the live person talking directly "
                      "to the camera (not people inside the video content being reacted to)")


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
    "split": "The screen is split into two side-by-side panels: the webcam panel and the content panel, each spanning the full height.",
    "overlay": "The content fills most of the screen, and the streamer's webcam is a smaller overlay window drawn on top of it.",
    "full": "The streamer's webcam fills the entire screen here; there is NO separate content panel in this part of the video.",
    "fullcontent": "The content fills the entire screen here; the streamer's webcam is NOT visible in this part of the video.",
}

_CONTENT_PROBE_PROMPT = ("the content being shown or reacted to — an embedded video, "
                         "image, meme, text post, comment, thread or screenshot "
                         "(not the live streamer's webcam)")

_CAM_PRESENT_QUESTION = ("Is the live streamer's own webcam view visible in this frame "
                         "(the person reacting — not people inside the content being "
                         "shown)? Answer with one word: yes or no.")


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
        return ("split", p_side)
    c = vision.detect_box(b64, _CONTENT_PROBE_PROMPT, w, h)
    c_huge = _geom(c, w, h)[0]
    if p is None:
        if c_huge:
            return ("fullcontent", None)
        return None
    if p_small and (c_huge or c is None):
        ans = (vision.describe(b64, _CAM_PRESENT_QUESTION, max_tokens=5) or "").strip().lower()
        if ans.startswith("no"):
            return ("fullcontent", None)
        if ans.startswith("yes"):
            return ("overlay", p_side)
        return None
    return None


def _detect_layout_segments(source_path: Path, t0: float, t1: float, w: int, h: int,
                            min_gap: float = 1.0, max_probes: int = 9):
    """The layout can CHANGE mid-clip (the cam moves / the split flips). Probe
    spread frames, label each (layout, side), smooth isolated flaky labels
    (majority-of-3), merge equal runs into segments, and refine each boundary by
    BISECTION down to ~`min_gap`s so the switch time is accurate. Returns ordered
    segments [{t0, t1, layout, side}] covering [t0, t1], or None if every probe
    was inconclusive."""
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
    return _detect_layout_segments(source_path, t0, t1, w, h, min_gap=min_gap)


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


def _track_segment(results: list, w: int, h: int, padding: float,
                   smooth: bool, lock_size: bool):
    """The single-layout pipeline (outlier rejection → pad → smooth → size lock →
    static pin → interior bridge → keyframes), applied to ONE segment's samples.
    Returns (keyframes, raw_detected)."""
    det_items = [(t, b) for t, b in results if b]
    det_items = _reject_outliers(det_items)
    if padding and padding > 0:
        det_items = [(t, _pad(b, padding, w, h)) for t, b in det_items]
    raw_detected = len(det_items)
    if smooth and len(det_items) >= 3:
        det_items = _smooth(det_items)
    pinned = False
    if lock_size:
        det_items = _stabilize_size(det_items, w, h)
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
    return kfs, raw_detected


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
        return [(k["t"], kfs[i + 1]["t"] if i + 1 < len(kfs) else inf)
                for i, k in enumerate(kfs) if k.get("gap")]

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


def predict_track(
    source_path: Path,
    prompt: str,
    t_start: float = 0.0,
    t_end: Optional[float] = None,
    step_seconds: float = 0.2,
    padding: float = 0.05,
    smooth: bool = True,
    lock_size: bool = True,
    role: Optional[str] = None,
    segments: Optional[list] = None,
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
    if segments is not None:
        # caller supplies a shared, precomputed timeline (one probe pass for
        # both boxes) — copy it: detection feedback below mutates boundaries
        segments = [dict(s) for s in segments]
    elif has_ph:
        segments = _detect_layout_segments(source_path, t0, t1, w, h,
                                           min_gap=step)
        if not segments:
            s = _resolve_side(source_path, times, w, h)
            segments = [{"t0": t0, "t1": t1, "layout": "split", "side": s}]
    else:
        segments = [{"t0": t0, "t1": t1, "layout": None, "side": None}]
    seg_prompts = [_fill_placeholders(prompt, s["layout"], s["side"]) for s in segments]

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
        return (t, vision.detect_box(b64, seg_prompts[seg_idx_of(t)], w, h))

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
            return vision.detect_box(b64, seg_prompts[seg_i], w, h) if b64 else None

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
        kfs, rd = _track_segment(seg_results, w, h, padding, smooth, lock_size)
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
    }
