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
import imagesources
import llm

log = logging.getLogger(__name__)

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

PEXELS_SEARCH = "https://api.pexels.com/v1/search"


# ───────────────────────── segmentation ─────────────────────────

def segment_clip(words: list[dict], duration: float, seg_seconds: float,
                 t_start: float = 0.0, t_end_limit: Optional[float] = None) -> list[dict]:
    """Return [{idx, t_start, t_end, text}] covering [t_start, t_end_limit or duration].
    A range can be passed to plan illustrations for ONE sub-clip only (so a long
    source isn't segmented end-to-end when you just want a few moments)."""
    seg_seconds = max(1.0, float(seg_seconds))
    duration = max(seg_seconds, float(duration))
    lo = max(0.0, float(t_start or 0.0))
    hi = min(float(duration), float(t_end_limit)) if t_end_limit else float(duration)
    if hi <= lo:
        hi = duration
    segments: list[dict] = []
    idx = 0
    t = lo
    while t < hi - 1e-6:
        t_end = min(t + seg_seconds, hi)
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

def plan(
    words: list[dict],
    duration: float,
    seg_seconds: float,
    title: str = "",
    description: str = "",
    t_start: float = 0.0,
    t_end: Optional[float] = None,
    source: str = "all",
) -> list[dict]:
    """Full plan: segments with queries and candidate images attached. The FULL
    transcript (joined from all words) is passed to the LLM as global context so it
    can infer the video's overall topic and anchor every per-window query to it
    instead of chasing literal words. title/description are optional extra hints.
    [t_start, t_end] scopes planning to ONE sub-clip's range (default = whole clip)."""
    segments = segment_clip(words, duration, seg_seconds, t_start, t_end)
    full_transcript = " ".join(w["word"] for w in words).strip()
    queries = llm.queries_for_segments(
        [s["text"] for s in segments],
        title=title,
        description=description,
        full_transcript=full_transcript,
    )

    # Cache searches by query so repeated topics don't re-hit the API.
    cache: dict[str, list[dict]] = {}
    for seg, q in zip(segments, queries):
        seg["query"] = q
        if q not in cache:
            cache[q] = imagesources.search(q, source=source)
        seg["candidates"] = cache[q]
    return segments


# ───────────────────────── download picked images ─────────────────────────

def download_pick(job_id: str, url: str) -> Path:
    """Download one picked image to temp/, deduped by URL hash so two segments
    using the same image share one file. Returns the local path.
    A `/temp/...` url is a user-UPLOADED image already on disk — used directly
    (keeps its original bytes/format, so PNG alpha for stickers is preserved)."""
    if url.startswith("/temp/"):
        local = TEMP_DIR / Path(url).name        # Path().name strips any ../
        if not local.exists():
            raise FileNotFoundError(f"uploaded image missing: {url}")
        return local
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    dest = TEMP_DIR / f"{job_id}_ill_{digest}.jpg"
    if dest.exists():
        return dest
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest
