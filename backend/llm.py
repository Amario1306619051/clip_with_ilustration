"""vLLM client (OpenAI-compatible endpoint). Same provider as email_categorizer.
Used to turn a chunk of transcript into a short English stock-photo search query.
"""
from __future__ import annotations

import json
import logging
import re

from openai import OpenAI

import config

log = logging.getLogger(__name__)

_client_singleton: OpenAI | None = None


def _client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = OpenAI(
            api_key=config.VLLM_API_KEY,
            base_url=config.VLLM_BASE_URL,
            # Bound each request so a slow/hung call fails fast (then retries /
            # falls back) instead of blocking the whole render for minutes.
            timeout=90,
        )
    return _client_singleton


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(s: str) -> str:
    """Qwen3 often emits a <think>...</think> block first — strip it out."""
    return _THINK_RE.sub("", s)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


_SYSTEM = (
    "You pick stock-photo illustrations for a vertical video. You are given the "
    "video's FULL TRANSCRIPT (and sometimes a title/description), then a list of "
    "transcript snippets — one per time window. FIRST infer the video's OVERALL "
    "TOPIC/THEME from the full transcript. THEN for EACH snippet, output a concise "
    "ENGLISH visual search query (2-4 words) for a relevant stock photo shown under "
    "the video.\n"
    "RULES:\n"
    "- Every query MUST illustrate the video's overall topic. Treat each snippet as "
    "context for WHICH part of that topic to show — never as a literal object list. "
    "Ignore incidental everyday nouns (food, drinks, places, small talk, names of "
    "people/things mentioned only in passing) that are not themselves the video's "
    "subject; illustrate the surrounding meaning within the topic instead.\n"
    "- If a snippet is abstract, generic, has no speech, or names something that "
    "can't be photographed, fall back to imagery that represents the overall topic.\n"
    "- Prefer concrete, photographable scenes. For spiritual, religious, historical "
    "or abstract subjects choose evocative, respectful imagery that fits the theme "
    "(landscapes, light, nature, places, objects) rather than literal nouns.\n"
    "- Translate non-English snippets. No punctuation, no quotes."
)

# Cap how much transcript we inline as topic context. The model has a large
# context window, but a window or two of clip is tiny; this just guards against a
# pathologically long transcript blowing up the prompt.
_MAX_TRANSCRIPT_CHARS = 8000

# The internal Qwen3 is a *reasoning* model: left on, it spends minutes emitting a
# <think> block before the answer, which 504s the nginx gateway. Query generation
# needs no reasoning, so we disable thinking — turns a multi-minute call into ~1s.
_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}

# Cold start: the first request after idle 504s while the 122B model loads onto the
# GPUs (~1-2 min), but that same request warms it — so a couple of retries usually
# land on a warm model. Each failed attempt is bounded by the client timeout above.
_MAX_ATTEMPTS = 3


def _chat(user: str) -> str:
    """One chat completion with retries. The first request to the internal vLLM
    often 504s while the 122B model warms up; retrying lands on the now-warm model.
    Thinking is disabled so the call returns in ~1s instead of stalling on reasoning."""
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = _client().chat.completions.create(
                model=config.VLLM_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=2000,
                extra_body=_NO_THINK,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, _MAX_ATTEMPTS, e)
    raise last  # type: ignore[misc]


def queries_for_segments(
    snippets: list[str],
    title: str = "",
    description: str = "",
    full_transcript: str = "",
) -> list[str]:
    """Batch: derive the video's overall topic from the full transcript (auto — no
    user input needed; title/description are optional extra hints) and return one
    on-theme search query per snippet. Falls back to a keyword guess (snippet first,
    then the video topic) if the LLM call/parse fails so the pipeline never
    hard-stops on a bad model response."""
    if not snippets:
        return []

    transcript = (full_transcript or "").strip()[:_MAX_TRANSCRIPT_CHARS]
    topic_fallback = _fallback(transcript or f"{title} {description}")

    # Topic context: the full transcript is the primary, auto-derived source; the
    # title/description are optional hints layered on top if the user filled them.
    ctx_lines: list[str] = []
    if title.strip():
        ctx_lines.append(f"Title: {title.strip()}")
    if description.strip():
        ctx_lines.append(f"Description: {description.strip()}")
    if transcript:
        ctx_lines.append(f"Full transcript: {transcript}")
    topic_block = ""
    if ctx_lines:
        topic_block = (
            "VIDEO CONTEXT — infer the overall topic from this and anchor every "
            "query to it:\n" + "\n".join(ctx_lines) + "\n\n"
        )

    numbered = "\n".join(f"{i}. {s.strip()[:300]}" for i, s in enumerate(snippets))
    user = (
        topic_block
        + "Return ONLY a JSON array of objects [{\"idx\": <int>, \"query\": <str>}], "
        "one per snippet below. Snippets:\n" + numbered
    )

    try:
        raw = _strip_fences(_strip_thinking(_chat(user)))
        data = json.loads(raw)
        by_idx = {int(o["idx"]): str(o["query"]).strip() for o in data if "query" in o}
        return [
            by_idx.get(i) or _fallback(snippets[i], topic_fallback)
            for i in range(len(snippets))
        ]
    except Exception as e:  # noqa: BLE001 — never break the pipeline on LLM hiccup
        log.warning("LLM query generation failed, using fallback: %s", e)
        return [_fallback(s, topic_fallback) for s in snippets]


def _fallback(snippet: str, topic: str = "") -> str:
    """Cheap keyword guess: first few meaningful words of the snippet. When the
    snippet has nothing usable, fall back to the video topic keywords (so an empty
    window still gets an on-theme image) before the last-resort default."""
    words = re.findall(r"[A-Za-z]{3,}", snippet)
    return " ".join(words[:3]) or topic or "abstract background"
