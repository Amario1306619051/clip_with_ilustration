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


class SfxPlacement(BaseModel):
    """A soundboard sound placed onto the clip.
      - kind 'oneshot' — plays once starting at `t` (seconds, clip time).
      - kind 'range'   — plays over [t, t_end]; `loop` repeats it when the sound
                         is shorter than the range. `t_end` required for range.
    `volume` is a linear multiplier (1.0 = original) to balance against the
    clip's own audio. Times are clip-relative (re-based to a render sub-range)."""
    sound_id: str
    kind: str = "oneshot"          # 'oneshot' | 'range'
    t: float = 0.0
    t_end: Optional[float] = None
    volume: float = 1.0
    loop: bool = False


class SoundPatch(BaseModel):
    """Rename / set default volume for a library sound."""
    name: Optional[str] = None
    volume: Optional[float] = None


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
    # Soundboard sound effects mixed into the audio (one-shot + range/loop).
    sfx: list[SfxPlacement] = Field(default_factory=list)


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
    step_seconds: float = 0.4   # timing PRECISION — sampling is adaptive (~1s grid, denser only at changes)
    padding: float = 0.05
    smooth: bool = True
    lock_size: bool = True   # lock one box size across the range (pan only) — stable framing


class AutoBoxResponse(BaseModel):
    keyframes: list[Keyframe] = Field(default_factory=list)
    sampled: int = 0
    detected: int = 0
    message: str = ""


class ThumbnailTextRequest(BaseModel):
    """Ask the text LLM for eye-catching thumbnail headline options derived from
    the video's context. The frame capture + compositing are done client-side;
    this only returns suggested wording (the user can always type their own)."""
    context: str = ""           # title + description + transcript (whatever the UI has)
    n: int = 5                  # how many options to return
    language: str = ""          # optional hint; empty = match the content language
    tone: str = ""              # "" default | "funny" (kocak) | "serious" | "clickbait"


class ThumbnailTextResponse(BaseModel):
    titles: list[str] = Field(default_factory=list)


class QueueImportRequest(BaseModel):
    """Raw text of the uploaded JSON file. Parsed server-side (tolerant of the
    Python-dict single-quote style the user pastes)."""
    content: str


class QueueJobPatch(BaseModel):
    """Edits saved back to a queue job from the editor (auto-save). All optional —
    only the provided fields are written. `box1` holds the single crop box's
    keyframes (box2 is unused in illustrator but kept for a shared backend)."""
    title: Optional[str] = None
    description: Optional[str] = None
    box1: Optional[list[Keyframe]] = None
    box2: Optional[list[Keyframe]] = None
    # Editable layout context + per-box prompt, so re-Generate from the UI uses
    # the same (now user-tuned) text the batch ran with. box2/prompt2 unused in
    # illustrator (single box) but kept for a shared backend shape.
    context: Optional[str] = None
    prompt1: Optional[str] = None
    prompt2: Optional[str] = None
