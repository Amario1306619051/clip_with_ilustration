import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import illustrator
from models import IllustrationPick, Keyframe, Word

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Bundled caption fonts (OFL). libass is pointed here via `fontsdir=` so burned
# captions use these instead of a generic host fallback. Families: "Anton",
# "Bebas Neue".
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
CAPTION_FILL = "#FFFFFF"
CAPTION_HIGHLIGHT = "#E8FF3A"

OUT_W = 1080
OUT_H = 1920
TOP_H = 720      # 3/8 of 1920 — video crop (the bbox)
BOTTOM_H = 1200  # 5/8 of 1920 — AI illustration track

MAX_GROUP_WORDS = 3
MAX_GROUP_CHARS = 18


# ───────────────────────── encoder detection ─────────────────────────
_ENCODER_CACHE: Optional[str] = None


def _detect_encoder() -> str:
    """Return 'h264_nvenc' if NVENC is available AND functional, else 'libx264'.
    Override with ILLUSTRATOR_ENCODER=libx264. Cached after first call."""
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    override = os.environ.get("ILLUSTRATOR_ENCODER", "").strip().lower()
    if override in {"libx264", "h264_nvenc"}:
        _ENCODER_CACHE = override
        return _ENCODER_CACHE
    try:
        listed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        if "h264_nvenc" in listed.stdout:
            probe = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=black:s=320x240:d=0.1",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0:
                _ENCODER_CACHE = "h264_nvenc"
                return _ENCODER_CACHE
    except Exception:
        pass
    _ENCODER_CACHE = "libx264"
    return _ENCODER_CACHE


def _encode_args() -> list[str]:
    enc = _detect_encoder()
    if enc == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5",
            "-rc", "vbr", "-cq", "23", "-b:v", "0", "-pix_fmt", "yuv420p",
        ]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


# ───────────────────────── captions (ASS) ─────────────────────────

def to_ass_color(hex_rgb: str) -> str:
    s = hex_rgb.lstrip("#")
    if len(s) != 6:
        s = "FFFFFF"
    r, g, b = s[0:2], s[2:4], s[4:6]
    return f"&H00{b}{g}{r}".upper()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()) or "clip"
    return s[:80]


def _fmt_ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _group_words(words: list[Word]) -> list[dict]:
    groups: list[dict] = []
    buf: list[Word] = []
    buf_chars = 0

    def flush():
        nonlocal buf, buf_chars
        if not buf:
            return
        text = " ".join(w.word for w in buf).strip()
        groups.append({
            "text": text,
            "start": buf[0].start,
            "end": buf[-1].end,
            "words": [{"text": w.word, "start": w.start, "end": w.end} for w in buf],
        })
        buf = []
        buf_chars = 0

    for w in words:
        wlen = len(w.word)
        if buf and (len(buf) >= MAX_GROUP_WORDS or buf_chars + 1 + wlen > MAX_GROUP_CHARS):
            flush()
        buf.append(w)
        buf_chars = buf_chars + (1 if buf_chars else 0) + wlen
    flush()
    return groups


def _ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _build_ass(groups: list[dict], font: str, size: int, caption_y: int) -> str:
    """TikTok-karaoke captions: each word group → one Dialogue line per word
    time-slice, with the active word recolored to the accent + slightly scaled.
    Fat outline + shadow. Per-word timing comes from group["words"] (Whisper)."""
    primary = to_ass_color(CAPTION_FILL)
    highlight = to_ass_color(CAPTION_HIGHLIGHT)
    outline = to_ass_color("#000000")
    back = to_ass_color("#000000")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{primary},{outline},{back},1,0,0,0,100,100,0,0,1,6,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pos = f"{{\\pos({OUT_W // 2},{int(caption_y)})}}"
    lines = []
    for g in groups:
        words = g.get("words") or [{"text": g["text"], "start": g["start"], "end": g["end"]}]
        n = len(words)
        for j in range(n):
            seg_start = words[j]["start"]
            seg_end = words[j + 1]["start"] if j + 1 < n else g["end"]
            if seg_end <= seg_start:
                seg_end = seg_start + 0.05
            parts = []
            for k, wk in enumerate(words):
                t = _ass_escape(wk["text"]).upper()
                if k == j:
                    parts.append(f"{{\\1c{highlight}\\fscx112\\fscy112}}{t}{{\\1c{primary}\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            text = pos + " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_fmt_ass_time(seg_start)},{_fmt_ass_time(seg_end)},Caption,,0,0,0,,{text}"
            )
    return header + "\n".join(lines) + "\n"


# ───────────────────────── keyframe → ffmpeg expression ─────────────────────────

def _fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.4f}"


def _build_expr(keyframes: list[dict], key: str) -> str:
    if len(keyframes) == 1:
        return _fmt_num(keyframes[0][key])
    expr = _fmt_num(keyframes[-1][key])
    for i in range(len(keyframes) - 2, -1, -1):
        k0, k1 = keyframes[i], keyframes[i + 1]
        t0, t1 = k0["t"], k1["t"]
        v0, v1 = k0[key], k1[key]
        mode = (k0.get("interp") or "hold").lower()
        if mode == "linear" and t1 > t0 + 1e-6 and not k1.get("gap"):
            segment = (
                f"({_fmt_num(v0)}+({_fmt_num(v1)}-{_fmt_num(v0)})"
                f"*(t-{_fmt_num(t0)})/({_fmt_num(t1)}-{_fmt_num(t0)}))"
            )
        else:
            segment = _fmt_num(v0)
        expr = f"if(lt(t,{_fmt_num(t1)}),{segment},{expr})"
    t0 = keyframes[0]["t"]
    if t0 > 0:
        expr = f"if(lt(t,{_fmt_num(t0)}),{_fmt_num(keyframes[0][key])},{expr})"
    return expr


def _shift_keyframes(kfs: list[Keyframe], start: float) -> list[Keyframe]:
    if start <= 0 or not kfs:
        return kfs
    ordered = sorted(kfs, key=lambda k: k.t)
    pre = [k for k in ordered if k.t <= start]
    post = [k for k in ordered if k.t > start]
    out: list[Keyframe] = []
    if pre:
        out.append(pre[-1].model_copy(update={"t": 0.0}))
    for k in post:
        out.append(k.model_copy(update={"t": k.t - start}))
    return out


def _shift_words(words: list[Word], start: float, end: Optional[float]) -> list[Word]:
    if (start <= 0 or start is None) and end is None:
        return words
    start = max(0.0, start or 0.0)
    out: list[Word] = []
    for w in words:
        if end is not None and w.start >= end:
            continue
        if w.end <= start:
            continue
        new_start = max(0.0, w.start - start)
        new_end = w.end - start
        if end is not None:
            new_end = min(new_end, end - start)
        if new_end <= new_start:
            continue
        out.append(Word(word=w.word, start=new_start, end=new_end))
    return out


def _shift_illustrations(picks: list[IllustrationPick], start: float, end: Optional[float]) -> list[IllustrationPick]:
    """Re-base illustration windows onto the render sub-range, dropping/clipping
    windows outside [start, end]."""
    if (start <= 0) and end is None:
        return picks
    start = max(0.0, start or 0.0)
    out: list[IllustrationPick] = []
    for p in picks:
        if end is not None and p.t_start >= end:
            continue
        if p.t_end <= start:
            continue
        ns = max(0.0, p.t_start - start)
        ne = p.t_end - start
        if end is not None:
            ne = min(ne, end - start)
        if ne <= ns:
            continue
        out.append(IllustrationPick(t_start=ns, t_end=ne, url=p.url))
    return out


def _normalize_keyframes(keyframes: list[Keyframe]) -> list[dict]:
    cleaned: list[dict] = []
    for k in sorted(keyframes, key=lambda kk: kk.t):
        cleaned.append({
            "t": max(0.0, float(k.t)),
            "x": max(0.0, float(k.x)),
            "y": max(0.0, float(k.y)),
            "w": max(2.0, float(k.w)),
            "h": max(2.0, float(k.h)),
            "interp": (getattr(k, "interp", None) or "hold").lower(),
            "fit": (getattr(k, "fit", None) or "cover").lower(),
            "gap": bool(getattr(k, "gap", False)),
        })
    return cleaned


def _segment_enable_expr(kfs: list[dict], pred) -> str:
    parts: list[str] = []
    for i, k in enumerate(kfs):
        if not pred(k):
            continue
        t0 = k["t"]
        t1 = kfs[i + 1]["t"] if i + 1 < len(kfs) else 1e9
        parts.append(f"between(t,{_fmt_num(t0)},{_fmt_num(t1)})")
    return "+".join(parts) if parts else "0"


def _blur_enable_expr(kfs: list[dict]) -> str:
    return _segment_enable_expr(kfs, lambda k: k["fit"] == "blur_pad" and not k["gap"])


def _crop_chain_segmented(input_label: str, kfs: list[dict],
                          out_w: int, out_h: int, out_label: str) -> list[str]:
    """Render a box whose SIZE changes between keyframes. ffmpeg's `crop` w/h are
    init-locked (only x/y animate per-frame), so an animated-size box can't be a
    single expression-crop — crop each segment with a literal box and composite
    via overlay enable= switching. Per-keyframe fit + gaps handled here too.
    (Size is stepped per segment — crop can't do smooth per-frame zoom.)"""
    cover_filters = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"
    )
    n = len(kfs)
    real_idx = [i for i, k in enumerate(kfs) if not k["gap"]]
    parts: list[str] = []
    if not real_idx:
        k = kfs[0]
        parts.append(
            f"[{input_label}]crop={int(k['w'])}:{int(k['h'])}:{int(k['x'])}:{int(k['y'])},"
            f"{cover_filters},setsar=1,eq=brightness=-1.0:contrast=0[{out_label}]"
        )
        return parts

    splits = [f"{out_label}_sp{j}" for j in range(len(real_idx))]
    parts.append(f"[{input_label}]split={len(real_idx)}{''.join('[' + s + ']' for s in splits)}")

    seg_info: list[tuple[str, float, float]] = []
    for j, i in enumerate(real_idx):
        k = kfs[i]
        crop = f"crop={int(k['w'])}:{int(k['h'])}:{int(k['x'])}:{int(k['y'])}"
        seg = f"{out_label}_seg{j}"
        if k["fit"] == "blur_pad":
            parts.append(f"[{splits[j]}]{crop},setsar=1,split=2[{seg}a][{seg}b]")
            parts.append(f"[{seg}a]{cover_filters},gblur=sigma=30:steps=2,eq=brightness=-0.08[{seg}bg]")
            parts.append(f"[{seg}b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[{seg}fg]")
            parts.append(f"[{seg}bg][{seg}fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{seg}]")
        else:
            parts.append(f"[{splits[j]}]{crop},{cover_filters},setsar=1[{seg}]")
        t0 = 0.0 if i == 0 else k["t"]
        t1 = kfs[i + 1]["t"] if i + 1 < n else 1e9
        seg_info.append((seg, t0, t1))

    parts.append(f"color=c=black:s={out_w}x{out_h}:r=30[{out_label}_base]")
    cur = f"{out_label}_base"
    for idx, (seg, t0, t1) in enumerate(seg_info):
        nxt = out_label if idx == len(seg_info) - 1 else f"{out_label}_ov{idx}"
        parts.append(
            f"[{cur}][{seg}]overlay=enable='between(t,{_fmt_num(t0)},{_fmt_num(t1)})':shortest=1[{nxt}]"
        )
        cur = nxt
    return parts


def _crop_chain(input_label: str, keyframes: list[Keyframe],
                out_w: int, out_h: int, out_label: str) -> list[str]:
    """Turn a per-frame source crop into a fixed out_w×out_h slot. Per-keyframe
    fit (cover / blur_pad) — identical mechanics to clipper. Used for the TOP
    video slot here (the single bbox).

    crop w/h are init-locked in ffmpeg, so a box that changes SIZE between
    keyframes routes to `_crop_chain_segmented` (literal per-segment crops);
    the expression path handles single-kf or constant-size pan only."""
    kfs = _normalize_keyframes(keyframes)
    real = [k for k in kfs if not k["gap"]]
    if len(kfs) > 1 and len({(round(k["w"], 1), round(k["h"], 1)) for k in real}) > 1:
        return _crop_chain_segmented(input_label, kfs, out_w, out_h, out_label)
    if len(kfs) == 1:
        k = kfs[0]
        crop = f"crop={int(k['w'])}:{int(k['h'])}:{int(k['x'])}:{int(k['y'])}"
    else:
        crop = (
            f"crop=w='{_build_expr(kfs, 'w')}':h='{_build_expr(kfs, 'h')}'"
            f":x='{_build_expr(kfs, 'x')}':y='{_build_expr(kfs, 'y')}'"
        )

    real_fits = {k["fit"] for k in kfs if not k["gap"]}
    has_blur = "blur_pad" in real_fits
    has_cover = "cover" in real_fits or not real_fits
    has_gap = any(k["gap"] for k in kfs)

    cover_filters = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"
    )
    inner = f"{out_label}_box" if has_gap else out_label
    parts: list[str] = []

    if not has_blur:
        parts.append(f"[{input_label}]{crop},{cover_filters},setsar=1[{inner}]")
    elif not has_cover:
        parts.append(f"[{input_label}]{crop},setsar=1,split=2[{out_label}_a][{out_label}_b]")
        parts.append(f"[{out_label}_a]{cover_filters},gblur=sigma=30:steps=2,eq=brightness=-0.08[{out_label}_bg]")
        parts.append(f"[{out_label}_b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[{out_label}_fg]")
        parts.append(f"[{out_label}_bg][{out_label}_fg]overlay=(W-w)/2:(H-h)/2[{inner}]")
    else:
        cov, blr = f"{out_label}_cov", f"{out_label}_blr"
        enable_expr = _blur_enable_expr(kfs)
        parts.append(f"[{input_label}]{crop},setsar=1,split=3[{out_label}_s1][{out_label}_s2][{out_label}_s3]")
        parts.append(f"[{out_label}_s1]{cover_filters}[{cov}]")
        parts.append(f"[{out_label}_s2]{cover_filters},gblur=sigma=30:steps=2,eq=brightness=-0.08[{out_label}_bg]")
        parts.append(f"[{out_label}_s3]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[{out_label}_fg]")
        parts.append(f"[{out_label}_bg][{out_label}_fg]overlay=(W-w)/2:(H-h)/2[{blr}]")
        parts.append(f"[{cov}][{blr}]overlay=enable='{enable_expr}'[{out_label}_o]")
        parts.append(f"[{out_label}_o]setsar=1[{inner}]")

    if has_gap:
        gap_expr = _segment_enable_expr(kfs, lambda k: k["gap"])
        parts.append(f"color=c=black:s={out_w}x{out_h}:r=30[{out_label}_blk]")
        parts.append(f"[{inner}][{out_label}_blk]overlay=enable='{gap_expr}':shortest=1[{out_label}]")
    return parts


# ───────────────────────── duration probe ─────────────────────────

def _probe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(json.loads(out.stdout).get("format", {}).get("duration") or 0.0)
    except Exception:
        return 0.0


def _probe_dims(path: Path) -> tuple[int, int]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        s = json.loads(out.stdout).get("streams", [{}])[0]
        return int(s.get("width", 0)), int(s.get("height", 0))
    except Exception:
        return 0, 0


def _clamp_kfs(kfs: list[Keyframe], w: int, h: int) -> list[Keyframe]:
    """Defense-in-depth: cap each keyframe's box inside the source frame so an
    off-frame box can't make ffmpeg's crop fail. No-op for in-bounds boxes."""
    if not kfs or w <= 0 or h <= 0:
        return kfs
    out: list[Keyframe] = []
    for k in kfs:
        x = min(max(0.0, float(k.x)), max(0.0, w - 2.0))
        y = min(max(0.0, float(k.y)), max(0.0, h - 2.0))
        cw = max(2.0, min(float(k.w), w - x))
        ch = max(2.0, min(float(k.h), h - y))
        out.append(k.model_copy(update={"x": x, "y": y, "w": cw, "h": ch}))
    return out


# ───────────────────────── main render ─────────────────────────

def render(
    job_id: str,
    source_path: Path,
    title: str,
    box: list[Keyframe],
    illustrations: list[IllustrationPick],
    words: list[Word],
    caption_font: str = "Bricolage Grotesque",
    caption_size: int = 64,
    render_start: Optional[float] = None,
    render_end: Optional[float] = None,
) -> dict:
    if not box:
        raise ValueError("box (top crop) is required with >= 1 keyframe")

    rs = max(0.0, float(render_start)) if render_start is not None else 0.0
    re_ = float(render_end) if render_end is not None else None
    if re_ is not None and re_ <= rs:
        re_, rs = None, 0.0

    if rs > 0 or re_ is not None:
        box = _shift_keyframes(box, rs)
        words = _shift_words(words, rs, re_)
        illustrations = _shift_illustrations(illustrations, rs, re_)

    # Defense-in-depth: clamp the box inside the source frame (no-op for valid
    # boxes; prevents an off-frame crop from crashing ffmpeg).
    _sw, _sh = _probe_dims(source_path)
    box = _clamp_kfs(box, _sw, _sh)

    # Effective output duration (bounds the black base + looped image inputs).
    src_dur = _probe_duration(source_path)
    if re_ is not None:
        dur = re_ - rs
    elif src_dur > 0:
        dur = src_dur - rs
    else:
        dur = max((p.t_end for p in illustrations), default=0.0) or 60.0
    dur = max(0.5, dur)

    filename = f"{_slugify(title)}_{job_id}.mp4"
    output_path = OUTPUT_DIR / filename

    # Download ONLY the picked images (deduped by URL). This is the only point
    # the server writes image bytes — candidates were never stored.
    img_inputs: list[tuple[Path, IllustrationPick]] = []
    seen: dict[str, Path] = {}
    for p in sorted(illustrations, key=lambda x: x.t_start):
        if not p.url:
            continue
        if p.url not in seen:
            seen[p.url] = illustrator.download_pick(job_id, p.url)
        img_inputs.append((seen[p.url], p))

    # Build ASS captions — caption always sits at the top/bottom slot boundary.
    groups = _group_words(words) if words else []
    ass_text = _build_ass(groups, caption_font, caption_size, TOP_H)
    ass_path = source_path.parent / f"{job_id}.ass"
    ass_path.write_text(ass_text, encoding="utf-8")

    # ── filter graph ──
    parts: list[str] = []
    # Top slot: the video crop (single bbox, cover/blur per keyframe).
    parts.extend(_crop_chain("0:v", box, OUT_W, TOP_H, "top"))

    # Bottom slot: black base + each illustration overlaid during its window.
    # Input 0 = video; inputs 1..N = looped images (one per window).
    parts.append(f"color=c=black:s={OUT_W}x{BOTTOM_H}:r=30:d={dur:.3f}[botbase]")
    last_bot = "botbase"
    for i, (_path, pick) in enumerate(img_inputs):
        in_idx = i + 1  # video is input 0
        parts.append(
            f"[{in_idx}:v]scale={OUT_W}:{BOTTOM_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{BOTTOM_H},setsar=1[ill{i}]"
        )
        nxt = f"b{i}"
        parts.append(
            f"[{last_bot}][ill{i}]overlay=enable='between(t,{_fmt_num(pick.t_start)},{_fmt_num(pick.t_end)})':eof_action=pass[{nxt}]"
        )
        last_bot = nxt
    parts.append(f"[{last_bot}]null[bot]")

    parts.append("[top][bot]vstack=inputs=2:shortest=1[stacked]")
    last = "stacked"

    ass_path_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    fonts_escaped = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:")
    if groups:
        # fontsdir → libass uses bundled Anton/Bebas Neue, not a host fallback.
        parts.append(f"[{last}]subtitles={ass_path_escaped}:fontsdir={fonts_escaped}[outv]")
        vmap = "[outv]"
    else:
        vmap = f"[{last}]"

    filter_complex = ";".join(parts)

    # ── inputs ──
    input_seek: list[str] = []
    if rs > 0:
        input_seek += ["-ss", f"{rs:.3f}"]
    if re_ is not None:
        input_seek += ["-t", f"{(re_ - rs):.3f}"]

    hwaccel = ["-hwaccel", "cuda"] if _detect_encoder() == "h264_nvenc" else []

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-filter_threads", "8", "-filter_complex_threads", "8",
        *hwaccel,
        *input_seek,
        "-i", str(source_path),
    ]
    # Each picked image is fed ONLY for its own window (-itsoffset start + -t
    # window) at a low framerate — it's a still, so it doesn't need 30fps across
    # the whole clip. Old way (loop every image at 30fps over the full duration)
    # scaled+overlaid each image ~N×full_duration frames; this is ~9x faster with
    # identical output (overlay enable=between still gates the window; eof_action=
    # pass lets the black base show before/after each image's window).
    for path, pick in img_inputs:
        win = max(0.1, float(pick.t_end) - float(pick.t_start))
        cmd += ["-itsoffset", f"{float(pick.t_start):.3f}", "-loop", "1",
                "-framerate", "2", "-t", f"{win:.3f}", "-i", str(path)]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", vmap,
        "-map", "0:a?",
        *_encode_args(),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed:\n{proc.stderr}")

    try:
        ass_path.unlink()
    except OSError:
        pass

    return {"output_path": f"/output/{filename}", "filename": filename}
