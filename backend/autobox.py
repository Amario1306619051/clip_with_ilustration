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
}


def _classify_layout(source_path: Path, t: float, w: int, h: int):
    """One layout probe at time t: detect the webcam panel and classify
    (layout, side) geometrically — 'split' when the panel is a large share of the
    screen width, 'overlay' when it's a small window. None = inconclusive."""
    b64 = _extract_frame_b64(source_path, t)
    if not b64:
        return None
    det = vision.detect_box(b64, _SIDE_PROBE_PROMPT, w, h)
    if not det:
        return None
    cx = det["x"] + det["w"] / 2.0
    side = "left" if cx < w / 2.0 else "right"
    layout = "split" if det["w"] >= 0.34 * w else "overlay"
    return (layout, side)


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
    labels = [(t, _classify_layout(source_path, t, w, h)) for t in pts]
    known = [(t, l) for t, l in labels if l]
    if not known:
        return None
    # LAYOUT TYPE is decided GLOBALLY (majority vote): a probe occasionally boxes
    # the person instead of the panel → a small box → a spurious "overlay" label,
    # and one wrong layout phrase poisons that whole segment's detection. Type
    # switches mid-clip are rare; SIDE switches are the real mid-clip event, so
    # segmentation is by SIDE only.
    lay_votes = [l[0] for _, l in known]
    majority_layout = max(set(lay_votes), key=lay_votes.count)
    labels = [(t, (majority_layout, l[1]) if l else None) for t, l in labels]
    known = [(t, l) for t, l in labels if l]
    # fill inconclusive probes from the nearest conclusive one
    filled = [(t, l if l else min(known, key=lambda kt: abs(kt[0] - t))[1])
              for t, l in labels]
    # kill isolated flakes: a probe disagreeing with BOTH equal neighbors flips;
    # same for a lone flake at either END
    for i in range(1, len(filled) - 1):
        if (filled[i][1] != filled[i - 1][1] and filled[i - 1][1] == filled[i + 1][1]):
            filled[i] = (filled[i][0], filled[i - 1][1])
    if len(filled) >= 3:
        if filled[0][1] != filled[1][1] and filled[1][1] == filled[2][1]:
            filled[0] = (filled[0][0], filled[1][1])
        if filled[-1][1] != filled[-2][1] and filled[-2][1] == filled[-3][1]:
            filled[-1] = (filled[-1][0], filled[-2][1])
    # merge equal runs; refine each disagreement boundary by bisection
    boundaries = []   # (switch_time, new_label)
    cur = filled[0][1]
    for i in range(1, len(filled)):
        if filled[i][1] == cur:
            continue
        ta, tb, la = filled[i - 1][0], filled[i][0], cur
        while tb - ta > min_gap:
            tm = (ta + tb) / 2.0
            lm = _classify_layout(source_path, tm, w, h)
            # compare by SIDE only — layout type is already fixed globally
            if lm is None or lm[1] == la[1]:
                ta = tm
            else:
                tb = tm
        boundaries.append(((ta + tb) / 2.0, filled[i][1]))
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
    return merged


def _fill_placeholders(prompt: str, layout, side) -> str:
    """Substitute {layout}/{side}/{other_side} for one segment. .replace, never
    .format — user prompts may contain other braces."""
    p = prompt
    if side:
        other = "right" if side == "left" else "left"
        p = p.replace("{side}", side).replace("{other_side}", other)
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
    {keyframes, sampled, detected, width, height, segments}. With `lock_size`
    (default), the box SIZE is locked and only the center pans — per LAYOUT
    SEGMENT: when the prompt carries {side}/{other_side}/{layout} placeholders,
    the clip is first probed for layout changes (the cam can move mid-clip) and
    each segment gets its own resolved prompt, its own size lock/static pin, and
    a keyframe exactly at the refined switch time."""
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

    # Layout/side resolution per SEGMENT (the model needs concrete anchors —
    # measured: median width 1030 with a stated side vs 1206-1679 without; and
    # the geometric probe beats asking "left or right?" which flips when the
    # content also shows people talking to camera).
    has_ph = any(tok in prompt for tok in ("{side}", "{other_side}", "{layout}"))
    if has_ph:
        segments = _detect_layout_segments(source_path, t0, t1, w, h,
                                           min_gap=max(1.0, eff_step * 0.67))
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

    def work(t: float):
        b64 = _extract_frame_b64(source_path, t)
        if not b64:
            return (t, None)
        return (t, vision.detect_box(b64, seg_prompts[seg_idx_of(t)], w, h))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(work, times))
    results.sort(key=lambda r: r[0])

    # Boundary correction by DETECTION FEEDBACK: the coarse probes can lie (the
    # content shows people too), but the dense sample grid is ground truth — a
    # contiguous MISS run touching a segment edge usually means the boundary is
    # wrong and those frames belong to the neighboring segment. Re-detect such
    # runs with the neighbor's prompt; where they hit, move the boundary to the
    # first hit. This pins the switch to within one sample step regardless of
    # probe flakiness.
    if len(segments) > 1:
        res_by_t = dict(results)

        def redetect(t: float, prompt_str: str):
            b64 = _extract_frame_b64(source_path, t)
            return vision.detect_box(b64, prompt_str, w, h) if b64 else None

        for i in range(len(segments) - 1):
            seg_times = [t for t, _ in results
                         if segments[i]["t0"] - 1e-9 <= t < segments[i + 1]["t0"] - 1e-9]
            # trailing misses of segment i → maybe they're already segment i+1
            run = []
            for t in reversed(seg_times):
                if res_by_t.get(t) is None:
                    run.insert(0, t)
                else:
                    break
            if run:
                hits = {t: redetect(t, seg_prompts[i + 1]) for t in run}
                hit_times = sorted(t for t, b in hits.items() if b)
                if hit_times:
                    boundary = hit_times[0]
                    segments[i + 1]["t0"] = boundary
                    segments[i]["t1"] = boundary
                    for t in hit_times:
                        res_by_t[t] = hits[t]
            # leading misses of segment i+1 → maybe they're still segment i
            next_times = [t for t, _ in results if t >= segments[i + 1]["t0"] - 1e-9]
            lead = []
            for t in next_times:
                if res_by_t.get(t) is None:
                    lead.append(t)
                else:
                    break
            if lead:
                hits = {t: redetect(t, seg_prompts[i]) for t in lead}
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
    keyframes = _dedupe_keyframes(keyframes)

    return {
        "keyframes": keyframes,
        "sampled": len(times),
        "detected": detected,
        "width": w,
        "height": h,
        "step": round(eff_step, 2),
        "capped": capped,
        "side": segments[0]["side"],   # first segment's side (None w/o placeholders)
        "segments": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2),
                      "layout": s["layout"], "side": s["side"]} for s in segments],
    }
