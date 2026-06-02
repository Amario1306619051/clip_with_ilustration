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
        )
    return _client_singleton


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(s: str) -> str:
    """Qwen3 sering output <think>...</think> dulu — buang block-nya."""
    return _THINK_RE.sub("", s)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


_SYSTEM = (
    "You turn short transcript snippets into stock-photo search queries. "
    "For each snippet, output a concise ENGLISH visual search query (2-4 words) "
    "that captures the literal topic being discussed, so a relevant illustration "
    "can be found on a stock photo site. Translate non-English snippets. Prefer "
    "concrete, photographable nouns over abstractions. No punctuation, no quotes."
)


def queries_for_segments(snippets: list[str]) -> list[str]:
    """Batch: given a list of transcript snippets, return one search query each.
    Falls back to a trimmed snippet if the LLM call/parse fails so the pipeline
    never hard-stops on a bad model response."""
    if not snippets:
        return []

    numbered = "\n".join(f"{i}. {s.strip()[:300]}" for i, s in enumerate(snippets))
    user = (
        "Return ONLY a JSON array of objects [{\"idx\": <int>, \"query\": <str>}], "
        "one per snippet below. Snippets:\n" + numbered
    )

    try:
        resp = _client().chat.completions.create(
            model=config.VLLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or ""
        raw = _strip_fences(_strip_thinking(raw))
        data = json.loads(raw)
        by_idx = {int(o["idx"]): str(o["query"]).strip() for o in data if "query" in o}
        return [by_idx.get(i) or _fallback(snippets[i]) for i in range(len(snippets))]
    except Exception as e:  # noqa: BLE001 — never break the pipeline on LLM hiccup
        log.warning("LLM query generation failed, using fallback: %s", e)
        return [_fallback(s) for s in snippets]


def _fallback(snippet: str) -> str:
    """Cheap keyword guess: first few meaningful words of the snippet."""
    words = re.findall(r"[A-Za-z]{3,}", snippet)
    return " ".join(words[:3]) or "abstract background"
