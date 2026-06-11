"""Vision client for AI auto-box — a prompt → a bounding box on a video frame.

Talks to an OpenAI-compatible Qwen-VL endpoint (config via env, same model as
browser_agent). Empirically characterised (across timestamps, prompt variants,
and aspect ratios):

- The model returns boxes as `bbox_2d` = [xmin, ymin, xmax, ymax] in a **0-1000
  NORMALIZED** space (NOT pixels, NOT Gemini's [ymin,xmin,...] order), regardless
  of prompt wording. Convert per-axis: px = coord / 1000 * frame_dim. This is
  aspect-independent — works for 16:9 / portrait / square as long as you use the
  dimensions of the image actually sent.
- Output formatting is non-deterministic even at temperature 0: ```json fences vs
  bare JSON, an intermittent `label` field, occasionally a missing `bbox_2d` key
  (bare [[...]]), and sometimes malformed JSON. So parsing is regex-based, never a
  bare json.loads.
- A singular prompt ("the speaker") returns one box on the subject; if several
  boxes come back, the largest-area one is the main subject. Absent subjects yield
  an empty array / prose, never a hallucinated box.

Frames may be downscaled before sending (normalized coords are scale-free, so we
still convert with the SOURCE dimensions).
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional

from openai import OpenAI

log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the project-root .env into os.environ (no
    override). Self-contained — clipper has no config.py."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

BASE_URL = os.getenv("VISION_BASE_URL", "").strip()
MODEL = os.getenv("VISION_MODEL", "").strip()
API_KEY = os.getenv("VISION_API_KEY", os.getenv("VLLM_API_KEY", "dummy"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


TIMEOUT = _env_float("VISION_TIMEOUT", 30.0)

_client_singleton: Optional[OpenAI] = None


def enabled() -> bool:
    """True when a vision endpoint is configured — else the auto-box feature is off."""
    return bool(BASE_URL and MODEL)


def _client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=TIMEOUT)
    return _client_singleton


# Built by concatenation (NOT str.format) so a user prompt containing { or } is safe.
_PROMPT_TAIL = (
    ". Return ONLY a JSON array and nothing else (no markdown, no code fences, no "
    "explanation): [{\"bbox_2d\":[x1,y1,x2,y2]}]. If the object is not present, return []."
)


def _build_prompt(q: str) -> str:
    return "Detect: " + q + _PROMPT_TAIL

_NUM = r"(\d+(?:\.\d+)?)"
_QUAD = rf"\[\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*\]"
# Prefer arrays explicitly tied to the bbox_2d key; only if none exist fall back
# to any bare 4-number array (handles the model's [[...]]-without-key case). This
# avoids matching 4 numbers that happen to appear inside a "label" string.
_BBOX_KEY_RE = re.compile(r'"bbox_2d"\s*:\s*' + _QUAD)
_QUAD_RE = re.compile(_QUAD)


def _parse_boxes(text: str) -> list[tuple]:
    """Every box as (x1,y1,x2,y2) in 0-1000 units, corners normalized (min/max).
    Regex-based — the model's JSON is not always valid. bbox_2d-keyed arrays win;
    bare arrays are a fallback only when no keyed ones are present."""
    text = text or ""
    matches = list(_BBOX_KEY_RE.finditer(text)) or list(_QUAD_RE.finditer(text))
    out = []
    for m in matches:
        x1, y1, x2, y2 = (float(g) for g in m.groups())
        out.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return out


def describe(image_b64: str, question: str, max_tokens: int = 80) -> Optional[str]:
    """Free-form short answer about a frame (used by auto-context: the model
    "studies" the video first — e.g. describes the streamer's appearance — and
    that description is then baked into the box prompts)."""
    if not enabled():
        return None
    try:
        resp = _client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            temperature=0,
            max_tokens=max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("vision.describe failed: %s", e)
        return None


def detect_side(image_b64: str, subject: str = "the streamer's webcam panel (the live person talking to the camera)") -> Optional[str]:
    """Ask which horizontal side of the screen `subject` is on. Returns 'left' /
    'right' or None. Used to resolve the {side}/{other_side} prompt placeholders —
    empirically the model boxes panels FAR better when told the concrete side, so
    we probe the side first instead of asking position-agnostically."""
    if not enabled():
        return None
    try:
        resp = _client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text":
                    f"On which side of this screen is {subject}? "
                    "Answer with exactly one word: left or right."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            temperature=0,
            max_tokens=10,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        text = (resp.choices[0].message.content or "").strip().lower()
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("vision.detect_side failed: %s", e)
        return None
    if "left" in text:
        return "left"
    if "right" in text:
        return "right"
    return None


def detect_box(image_b64: str, prompt: str, frame_w: int, frame_h: int) -> Optional[dict]:
    """Return {x,y,w,h} in SOURCE PIXELS for the prompted subject, or None when the
    model finds nothing. frame_w/frame_h are the SOURCE dimensions (the image may
    have been downscaled — normalized coords are scale-free). Picks the
    largest-area box when several are returned."""
    if not enabled():
        return None
    q = (prompt or "the main subject").strip()
    try:
        resp = _client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _build_prompt(q)},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            temperature=0,
            max_tokens=300,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except Exception as e:  # noqa: BLE001 — per-frame best-effort
        log.warning("vision.detect_box request failed: %s", e)
        return None

    text = resp.choices[0].message.content or ""
    boxes = _parse_boxes(text)
    if not boxes:
        return None
    # Largest-area box (in normalized units) = the main subject, not background.
    x1, y1, x2, y2 = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))

    # 0-1000 normalized → source pixels, per-axis (aspect-independent), clamped.
    px1 = max(0.0, min(x1 / 1000.0 * frame_w, frame_w))
    px2 = max(0.0, min(x2 / 1000.0 * frame_w, frame_w))
    py1 = max(0.0, min(y1 / 1000.0 * frame_h, frame_h))
    py2 = max(0.0, min(y2 / 1000.0 * frame_h, frame_h))
    w, h = px2 - px1, py2 - py1
    if w < 2 or h < 2:
        return None
    return {"x": px1, "y": py1, "w": w, "h": h}
