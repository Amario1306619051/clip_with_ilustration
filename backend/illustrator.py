"""Illustration planning + stock-image fetching.

Flow:
  1. segment_clip()  — cut [0, duration] into N-second windows, attach the
     transcript text spoken in each window.
  2. llm.queries_for_segments() — topic → English search query per segment.
  3. search_pexels()  — query → candidate images (URLs only; nothing stored).
  4. download_pick()  — at render time, download ONLY the picked image, at a
     slot-sized resolution (Pexels `portrait` ~800x1200), deduped by URL.

Storage policy: candidates are URLs streamed by the browser straight from the
Pexels CDN. The server only ever writes the handful of images the user actually
picked, and only at render time. Everything is deleted on cleanup.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import requests

import config
import llm

log = logging.getLogger(__name__)

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

PEXELS_SEARCH = "https://api.pexels.com/v1/search"


# ───────────────────────── segmentation ─────────────────────────

def segment_clip(words: list[dict], duration: float, seg_seconds: float) -> list[dict]:
    """Return [{idx, t_start, t_end, text}] covering [0, duration]."""
    seg_seconds = max(1.0, float(seg_seconds))
    duration = max(seg_seconds, float(duration))
    segments: list[dict] = []
    idx = 0
    t = 0.0
    while t < duration - 1e-6:
        t_end = min(t + seg_seconds, duration)
        text = " ".join(
            w["word"] for w in words
            if w["start"] < t_end and w["end"] > t
        ).strip()
        segments.append({"idx": idx, "t_start": round(t, 3), "t_end": round(t_end, 3), "text": text})
        idx += 1
        t = t_end
    return segments


# ───────────────────────── Pexels ─────────────────────────

def search_pexels(query: str, per_page: Optional[int] = None) -> list[dict]:
    """Return candidate dicts {id, thumb, full, alt, photographer}. Empty list
    if no key configured or the request fails (UI shows 'no results')."""
    if not config.PEXELS_API_KEY:
        log.warning("PEXELS_API_KEY not set — returning no candidates")
        return []
    per_page = per_page or config.CANDIDATES_PER_SEGMENT
    try:
        resp = requests.get(
            PEXELS_SEARCH,
            headers={"Authorization": config.PEXELS_API_KEY},
            params={"query": query or "abstract", "per_page": per_page, "orientation": "portrait"},
            timeout=15,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
    except Exception as e:  # noqa: BLE001
        log.warning("Pexels search failed for %r: %s", query, e)
        return []

    out: list[dict] = []
    for p in photos:
        src = p.get("src", {})
        out.append({
            "id": str(p.get("id", "")),
            "thumb": src.get("medium") or src.get("small") or src.get("tiny", ""),
            # `portrait` is ~800x1200 — already close to the 9:10 bottom slot,
            # so we download a small file instead of the multi-MB original.
            "full": src.get("portrait") or src.get("large") or src.get("original", ""),
            "alt": p.get("alt", ""),
            "photographer": p.get("photographer", ""),
        })
    return out


# ───────────────────────── plan (segment + query + search) ─────────────────────────

def plan(words: list[dict], duration: float, seg_seconds: float) -> list[dict]:
    """Full plan: segments with queries and candidate images attached."""
    segments = segment_clip(words, duration, seg_seconds)
    queries = llm.queries_for_segments([s["text"] for s in segments])

    # Cache searches by query so repeated topics don't re-hit the API.
    cache: dict[str, list[dict]] = {}
    for seg, q in zip(segments, queries):
        seg["query"] = q
        if q not in cache:
            cache[q] = search_pexels(q)
        seg["candidates"] = cache[q]
    return segments


# ───────────────────────── download picked images ─────────────────────────

def download_pick(job_id: str, url: str) -> Path:
    """Download one picked image to temp/, deduped by URL hash so two segments
    using the same image share one file. Returns the local path."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    dest = TEMP_DIR / f"{job_id}_ill_{digest}.jpg"
    if dest.exists():
        return dest
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest
