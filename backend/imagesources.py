"""Multi-source stock-image search for the illustration cutaways / bottom slot.

Returns the SAME candidate shape everywhere — {id, thumb, full, alt, photographer}
— so the existing pick/download/render flow is unchanged (the renderer's
download_pick fetches any http `full` URL, or a /temp/ upload, at render time).

Sources:
  - pexels     — needs PEXELS_API_KEY (the original source)
  - openverse  — KEYLESS (openverse.org aggregates CC / public-domain images)
  - wikimedia  — KEYLESS (Wikimedia Commons)
  - unsplash   — needs UNSPLASH_API_KEY (Client-ID)
  - pixabay    — needs PIXABAY_API_KEY

Copyright: Openverse/Wikimedia return openly-licensed / public-domain media;
Pexels/Unsplash/Pixabay are royalty-free under their own licenses. We surface the
photographer/creator string so the user can attribute when a license needs it.

Self-loads the project-root .env (works whether or not the app has a config.py).
"""
import logging
import os
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
UNSPLASH_API_KEY = os.getenv("UNSPLASH_API_KEY", "").strip()
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
# Optional — an Openverse API token lifts the anonymous rate limit (not required).
OPENVERSE_TOKEN = os.getenv("OPENVERSE_API_KEY", "").strip()

try:
    CANDIDATES = int(os.getenv("CLIPPER_CANDIDATES", os.getenv("ILLUSTRATOR_CANDIDATES", "12")) or 12)
except ValueError:
    CANDIDATES = 12

# Sources that need no key are always on; keyed ones appear only when configured.
_KEYLESS = ("openverse", "wikimedia")


def available() -> dict:
    """Which sources can return results right now (drives the UI source picker)."""
    return {
        "pexels": bool(PEXELS_API_KEY),
        "openverse": True,
        "wikimedia": True,
        "unsplash": bool(UNSPLASH_API_KEY),
        "pixabay": bool(PIXABAY_API_KEY),
    }


def default_source() -> str:
    """First usable source — Pexels if keyed, else the first keyless one."""
    return "pexels" if PEXELS_API_KEY else "openverse"


# ───────────────────────── per-source search ─────────────────────────

def _search_pexels(query: str, n: int) -> list[dict]:
    if not PEXELS_API_KEY:
        return []
    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query or "abstract", "per_page": n, "orientation": "portrait"},
        timeout=15,
    )
    resp.raise_for_status()
    out = []
    for p in resp.json().get("photos", []):
        src = p.get("src", {})
        out.append({
            "id": f"px_{p.get('id', '')}",
            "thumb": src.get("medium") or src.get("small") or src.get("tiny", ""),
            "full": src.get("portrait") or src.get("large") or src.get("original", ""),
            "alt": p.get("alt", ""),
            "photographer": p.get("photographer", ""),
        })
    return out


def _search_openverse(query: str, n: int) -> list[dict]:
    headers = {"User-Agent": "clipper-illustrator/1.0"}
    if OPENVERSE_TOKEN:
        headers["Authorization"] = f"Bearer {OPENVERSE_TOKEN}"
    resp = requests.get(
        "https://api.openverse.org/v1/images/",
        params={"q": query or "abstract", "page_size": n,
                "license_type": "all", "mature": "false"},
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    out = []
    for r in resp.json().get("results", []):
        full = r.get("url") or r.get("thumbnail")
        thumb = r.get("thumbnail") or full
        if not full:
            continue
        out.append({
            "id": f"ov_{r.get('id', '')}",
            "thumb": thumb,
            "full": full,
            "alt": r.get("title", ""),
            "photographer": r.get("creator", "") or (r.get("source", "") or ""),
        })
    return out


def _search_wikimedia(query: str, n: int) -> list[dict]:
    resp = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrnamespace": "6", "gsrsearch": query or "abstract", "gsrlimit": n,
            "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1024",
        },
        headers={"User-Agent": "clipper-illustrator/1.0 (image search)"},
        timeout=15,
    )
    resp.raise_for_status()
    pages = (resp.json().get("query", {}) or {}).get("pages", {}) or {}
    out = []
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        full = info.get("url")
        thumb = info.get("thumburl") or full
        title = (p.get("title", "") or "").replace("File:", "")
        if not full or not full.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "gif", "webp"):
            continue   # skip svg/tif/pdf/ogv — not directly usable as a still
        meta = info.get("extmetadata", {}) or {}
        artist = (meta.get("Artist", {}) or {}).get("value", "") or ""
        # strip any HTML in the artist field
        import re as _re
        artist = _re.sub(r"<[^>]+>", "", artist).strip()
        out.append({
            "id": f"wm_{p.get('pageid', '')}",
            "thumb": thumb, "full": full,
            "alt": title, "photographer": artist[:80],
        })
    return out


def _search_unsplash(query: str, n: int) -> list[dict]:
    if not UNSPLASH_API_KEY:
        return []
    resp = requests.get(
        "https://api.unsplash.com/search/photos",
        params={"query": query or "abstract", "per_page": n, "orientation": "portrait"},
        headers={"Authorization": f"Client-ID {UNSPLASH_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    out = []
    for r in resp.json().get("results", []):
        urls = r.get("urls", {}) or {}
        out.append({
            "id": f"us_{r.get('id', '')}",
            "thumb": urls.get("small") or urls.get("thumb", ""),
            "full": urls.get("regular") or urls.get("full") or urls.get("raw", ""),
            "alt": r.get("alt_description") or r.get("description") or "",
            "photographer": ((r.get("user", {}) or {}).get("name", "")),
        })
    return out


def _search_pixabay(query: str, n: int) -> list[dict]:
    if not PIXABAY_API_KEY:
        return []
    resp = requests.get(
        "https://pixabay.com/api/",
        params={"key": PIXABAY_API_KEY, "q": query or "abstract", "per_page": max(3, n),
                "image_type": "photo", "orientation": "vertical", "safesearch": "true"},
        timeout=15,
    )
    resp.raise_for_status()
    out = []
    for h in resp.json().get("hits", []):
        out.append({
            "id": f"pb_{h.get('id', '')}",
            "thumb": h.get("webformatURL") or h.get("previewURL", ""),
            "full": h.get("largeImageURL") or h.get("webformatURL", ""),
            "alt": h.get("tags", ""),
            "photographer": h.get("user", ""),
        })
    return out


_DISPATCH = {
    "pexels": _search_pexels,
    "openverse": _search_openverse,
    "wikimedia": _search_wikimedia,
    "unsplash": _search_unsplash,
    "pixabay": _search_pixabay,
}


def search(query: str, source: Optional[str] = None, per_page: Optional[int] = None) -> list[dict]:
    """Search one source — or ALL of them mixed (source='all') — returning candidate
    dicts {id, thumb, full, alt, photographer}. 'all' fans out to every available
    source and INTERLEAVES the results so one clip can pull from Pexels + Openverse +
    Wikimedia (+ Unsplash/Pixabay if keyed) at once. Any failure → [] for that source
    so a flaky one never breaks the flow."""
    n = per_page or CANDIDATES
    src = (source or default_source()).lower()
    if src == "all":
        avail = [k for k in _DISPATCH if available().get(k, False)]
        if not avail:
            return []
        per = max(3, -(-n // len(avail)))   # ceil division per source
        buckets = []
        for k in avail:
            try:
                buckets.append(_DISPATCH[k](query, per))
            except Exception as e:  # noqa: BLE001
                log.warning("%s search failed for %r: %s", k, query, e)
                buckets.append([])
        out, depth = [], max((len(b) for b in buckets), default=0)
        for i in range(depth):              # interleave → variety across sources
            for b in buckets:
                if i < len(b):
                    out.append(b[i])
        return out
    if src not in _DISPATCH or not available().get(src, False):
        src = default_source()
    fn = _DISPATCH.get(src)
    if not fn:
        return []
    try:
        return fn(query, n)
    except Exception as e:  # noqa: BLE001
        log.warning("%s search failed for %r: %s", src, query, e)
        return []
