"""Thumbnail headline generator.

Turns a video's context (title / description / transcript) into a few short,
eye-catching headline options via the text vLLM — the same OpenAI-compatible
Qwen3 endpoint the rest of the stack uses. The frame capture and the
compositing happen client-side on a canvas; this module only supplies the
suggested wording (the user can always type their own).

Self-loads the project-root .env (clipper has no config.py) and reads the
VLLM_* vars, defaulting to the internal endpoint. Thinking is disabled on the
reasoning model (a <think> block stalls for minutes and 504s the gateway) and
cold-start 504s are retried — same handling as illustrator/backend/llm.py.
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
    override). Self-contained — mirrors vision.py."""
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

BASE_URL = os.getenv("VLLM_BASE_URL", "https://internal-rnd.balitower.co.id/models/qwen35").strip()
MODEL = os.getenv("VLLM_MODEL", "gb10-qwen35-122b-nvfp4-4node-100k").strip()
API_KEY = os.getenv("VLLM_API_KEY", "dummy")

_client_singleton: Optional[OpenAI] = None


def enabled() -> bool:
    """True when a text endpoint is configured — gates the 'Generate ideas'
    button. Manual headline typing works regardless."""
    return bool(BASE_URL and MODEL)


def _client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        # Bound each request so a slow/hung call fails fast (then retries /
        # falls back) instead of blocking for minutes.
        _client_singleton = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=90)
    return _client_singleton


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# The internal Qwen3 is a reasoning model: left on it spends minutes emitting a
# <think> block before answering (504s the gateway). Headlines need no
# reasoning, so thinking is disabled — turns a multi-minute call into ~1s.
_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
_MAX_ATTEMPTS = 3            # cold start 504s while the 122B model warms up
_MAX_CONTEXT_CHARS = 6000    # guard against a pathologically long transcript

_SYSTEM = (
    "You write punchy, eye-catching thumbnail headlines for short-form videos "
    "(TikTok / YouTube Shorts / Reels). You are given the video's context "
    "(title, description and/or transcript). Output a list of distinct headline "
    "options.\n"
    "RULES:\n"
    "- Write in the SAME LANGUAGE as the video content (if the transcript is "
    "Indonesian, write Indonesian headlines — do NOT translate to English).\n"
    "- Each headline is SHORT (2-6 words) and high-impact: a hook that sparks "
    "curiosity but stays TRUE to the content. No fake or exaggerated claims.\n"
    "- Vary the angle across options (a question, a bold statement, a number, a "
    "curiosity gap).\n"
    "- No surrounding quotes, no numbering, no emoji, no hashtags, no trailing "
    "punctuation. Output ONE headline per line and nothing else."
)


def _strip_thinking(s: str) -> str:
    return _THINK_RE.sub("", s)


def _chat(user: str) -> str:
    """One chat completion with retries. The first request to the internal vLLM
    often 504s while the 122B model warms up; retrying lands on the warm model."""
    last: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = _client().chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.8,   # a little spread so the options differ
                max_tokens=400,
                extra_body=_NO_THINK,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("thumbnail LLM call failed (attempt %d/%d): %s",
                        attempt + 1, _MAX_ATTEMPTS, e)
    raise last  # type: ignore[misc]


# Strip leading bullets / numbering / quotes and trailing quotes / punctuation
# the model sometimes adds despite the instruction.
_LEAD_RE = re.compile(r'^[\s\-\*\d\.\)\]:"\'•]+')
_TRAIL_RE = re.compile(r'[\s"\'.]+$')


def _clean_line(line: str) -> str:
    return _TRAIL_RE.sub("", _LEAD_RE.sub("", line)).strip()


def generate_titles(context: str, n: int = 5, language: str = "") -> list[str]:
    """Return up to `n` distinct eye-catching headline options derived from the
    video context. Falls back to a keyword guess if the LLM call/parse fails so
    the UI never hard-stops on a bad model response."""
    context = (context or "").strip()[:_MAX_CONTEXT_CHARS]
    n = max(1, min(int(n or 5), 10))
    if not context:
        context = "(no transcript available — write a generic but catchy headline)"
    lang_hint = f"\nThe content language is: {language.strip()}." if language.strip() else ""
    user = (
        f"Give me {n} thumbnail headline options for this video.{lang_hint}\n\n"
        f"VIDEO CONTEXT:\n{context}"
    )
    try:
        raw = _strip_thinking(_chat(user))
        lines = [_clean_line(l) for l in raw.splitlines()]
    except Exception as e:  # noqa: BLE001 — never break the UI on an LLM hiccup
        log.warning("thumbnail title generation failed, using fallback: %s", e)
        return _fallback(context, n)

    out: list[str] = []
    seen: set[str] = set()
    for l in lines:
        if not l or len(l) > 60:
            continue
        key = l.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
        if len(out) >= n:
            break
    return out or _fallback(context, n)


def _fallback(context: str, n: int) -> list[str]:
    """Cheap last resort: the first few meaningful words of the context, capped."""
    words = re.findall(r"[\wÀ-ɏ']{3,}", context)
    base = " ".join(words[:4]).upper() if words else "WATCH THIS"
    return [base][:max(1, n)]
