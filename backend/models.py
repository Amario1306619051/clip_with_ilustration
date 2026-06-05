from typing import Optional
from pydantic import BaseModel, Field


class Keyframe(BaseModel):
    """Single position/size sample for the TOP crop box at time `t` (seconds,
    relative to clip start). Same semantics as clipper's Keyframe.
      - interp: 'hold' (default) | 'linear'
      - fit:    'cover' (default) | 'blur_pad'   ← the "blur or not" choice
      - gap:    True marks the segment as empty (black slot)
    """
    t: float = 0.0
    x: float
    y: float
    w: float
    h: float
    interp: str = "hold"
    fit: str = "cover"
    gap: bool = False


class Word(BaseModel):
    word: str
    start: float
    end: float


# ───────────────────────── source / transcribe ─────────────────────────

class DownloadRequest(BaseModel):
    url: str
    start: str = "00:00:00"
    end: Optional[str] = None
    title: str = "clip"
    description: str = ""


class DownloadResponse(BaseModel):
    job_id: str
    video_path: str
    duration: float
    width: int
    height: int


class TranscribeRequest(BaseModel):
    job_id: str


class TranscribeResponse(BaseModel):
    words: list[Word]


# ───────────────────────── illustration planning ─────────────────────────

class PlanRequest(BaseModel):
    """Cut the clip into N-second segments, derive a stock-photo search query
    per segment from the transcript, and fetch candidate images per segment.
    title/description = the video's overall topic, passed to the LLM as global
    context so every query stays anchored to the theme (not literal words)."""
    job_id: str
    words: list[Word] = Field(default_factory=list)
    segment_seconds: float = 5.0
    duration: float  # clip duration in seconds (from /api/download)
    title: str = ""
    description: str = ""


class Candidate(BaseModel):
    """One stock-photo option. `thumb` is shown in the UI (loaded straight from
    the Pexels CDN — never stored server-side). `full` is downloaded only if
    this candidate is the one picked, at render time."""
    id: str
    thumb: str
    full: str
    alt: str = ""
    photographer: str = ""


class Segment(BaseModel):
    idx: int
    t_start: float
    t_end: float
    text: str
    query: str
    candidates: list[Candidate] = Field(default_factory=list)


class PlanResponse(BaseModel):
    segments: list[Segment]


class SearchRequest(BaseModel):
    """Re-search one segment with a user-edited query."""
    query: str


class SearchResponse(BaseModel):
    candidates: list[Candidate]


# ───────────────────────── render ─────────────────────────

class IllustrationPick(BaseModel):
    """A chosen image bound to a time window in the bottom slot."""
    t_start: float
    t_end: float
    url: str  # the picked candidate's `full` URL


CAPTION_FONTS = [
    "Anton",          # bundled (assets/fonts) — heavy display, TikTok default
    "Bebas Neue",     # bundled — tall condensed
    "Bricolage Grotesque",
    "JetBrains Mono",
    "Inter",
    "Arial",
    "Impact",
]


class RenderRequest(BaseModel):
    job_id: str
    title: str = "clip"
    # Single crop box (the TOP slot). List of keyframes, len >= 1.
    box: list[Keyframe]
    # Bottom slot: ordered, non-overlapping illustration windows.
    illustrations: list[IllustrationPick] = Field(default_factory=list)
    words: list[Word] = Field(default_factory=list)
    caption_font: str = "Anton"
    caption_size: int = 64
    cleanup: bool = False
    render_start: Optional[float] = None
    render_end: Optional[float] = None


class RenderResponse(BaseModel):
    output_path: str
    filename: str


class CleanupRequest(BaseModel):
    job_id: str


class AutoBoxRequest(BaseModel):
    """Ask the vision model to draw a box track for `prompt` over [t_start, t_end]
    for the top crop box. Returns keyframes the user can edit in the Crop step."""
    job_id: str
    prompt: str
    t_start: float = 0.0
    t_end: Optional[float] = None
    box: int = 1
    step_seconds: float = 1.5
    padding: float = 0.05
    smooth: bool = True
    lock_size: bool = True   # lock one box size across the range (pan only) — stable framing


class AutoBoxResponse(BaseModel):
    keyframes: list[Keyframe] = Field(default_factory=list)
    sampled: int = 0
    detected: int = 0
    message: str = ""
