from typing import Optional
from pydantic import BaseModel, Field


class Keyframe(BaseModel):
    """Single position/size sample for the TOP crop box at time `t` (seconds,
    relative to clip start). Same semantics as clipper's Keyframe.
      - interp: 'hold' (default) | 'linear'
      - fit:    'cover' (default) | 'blur_pad'   ← the "blur or not" choice
      - gap:    True marks the segment as empty (black slot)
      - dynamic: True (implies gap) → auto-box judged it too unstable; left empty
        on purpose and flagged in the UI for manual drawing (auto-box no longer
        emits these — a moving subject now pans — but the field stays for back-compat)
      - moving: True (implies gap=False) → a panning track for a moving subject
        (size locked, center pans, interp='linear'); drives a 'TRACKED' chip
    """
    t: float = 0.0
    x: float
    y: float
    w: float
    h: float
    interp: str = "hold"
    fit: str = "cover"
    gap: bool = False
    dynamic: bool = False
    moving: bool = False


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
    # Scope planning to ONE sub-clip's range (default 0/None = whole clip).
    t_start: float = 0.0
    t_end: Optional[float] = None
    # Image source: 'all' (mixed) | pexels | openverse | wikimedia | unsplash | pixabay.
    source: str = "all"


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
    # Image source: pexels | openverse | wikimedia | unsplash | pixabay.
    # None → backend default (Pexels if keyed, else a keyless source).
    source: Optional[str] = None


class SearchResponse(BaseModel):
    candidates: list[Candidate]


# ───────────────────────── render ─────────────────────────

class IllustrationPick(BaseModel):
    """A chosen image bound to a time window in the bottom slot."""
    t_start: float
    t_end: float
    url: str  # the picked candidate's `full` URL
    # Ken Burns motion for this image, always kept inside the frame:
    # "none" | "zoom_in" | "zoom_out" | "pan_left" | "pan_right".
    motion: str = "none"


CAPTION_FONTS = [
    "Anton",          # bundled (assets/fonts) — heavy display, TikTok default
    "Bebas Neue",     # bundled — tall condensed
    "Archivo Black",  # bundled — heavy grotesque
    "Poppins",        # bundled (Bold) — clean modern
    "Fjalla One",     # bundled — condensed display
    "Bangers",        # bundled — comic / loud
    "Titan One",      # bundled — heavy rounded
    "Alfa Slab One",  # bundled — heavy slab
    "Oswald",         # bundled — condensed sans (news/sport look)
    "Bungee",         # bundled — urban block caps
    "Righteous",      # bundled — geometric retro
    "Luckiest Guy",   # bundled — fun comic caps
    "Permanent Marker",  # bundled — handwritten marker
    "Pacifico",       # bundled — script / handwriting
    "Bricolage Grotesque",
    "JetBrains Mono",
    "Inter",
    "Arial",
    "Impact",
]


class CaptionPosRange(BaseModel):
    """A time window [start,end] (seconds, clip time) where the caption sits at
    `pos` ('top' | 'middle' | 'bottom'). Outside every range the caption uses the
    global caption_pos. Lets the caption move around over the clip."""
    start: float
    end: float
    pos: str = "middle"


class TextOverlay(BaseModel):
    """A styled text label burned over the video for a time window [start,end]
    (seconds, clip time). Positioned by fractional center (x_frac/y_frac, 0–1 of
    the 1080×1920 frame). `font` must be one of CAPTION_FONTS (bundled .ttf burned
    via libass fontsdir). `color` is a #RRGGBB hex. Distinct from captions: this is
    arbitrary user text, not the transcript."""
    text: str
    start: float
    end: float
    x_frac: float = 0.5
    y_frac: float = 0.5
    size: int = 56
    font: str = "Anton"
    color: str = "#FFFFFF"


class Sticker(BaseModel):
    """A PNG sticker (alpha preserved) overlaid over a time window [start,end]
    (seconds, clip time). `url` is a /temp/ uploaded image. Positioned by fractional
    CENTER (x_frac/y_frac, 0–1 of the 1080×1920 frame); `scale` = width as a fraction
    of the frame width (aspect kept); `opacity` 0–1."""
    url: str
    start: float
    end: float
    x_frac: float = 0.5
    y_frac: float = 0.5
    scale: float = 0.3
    opacity: float = 1.0


class KeepSegment(BaseModel):
    """A [start, end] window to KEEP (seconds, clip time). At render, everything
    OUTSIDE all kept windows is dropped and the kept parts concatenated — lets the
    user cut out dead air / noisy stretches by marking what to keep (same as clipper)."""
    start: float
    end: float


class FullscreenWindow(BaseModel):
    """A [start, end] window (seconds, clip time) where the VIDEO fills the whole
    9:16 frame — no illustration split. Outside these windows the layout is the
    normal top-video + bottom-illustration. Lets the editor toggle, per window,
    whether an illustration is shown ('ON') or the video goes full-screen ('OFF')."""
    t_start: float
    t_end: float


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
    caption_pos: str = "middle"   # default vertical caption position: "top" | "middle" | "bottom"
    # Per-time-window caption position overrides (outside any window → caption_pos).
    caption_pos_ranges: list[CaptionPosRange] = Field(default_factory=list)
    # Styled text labels burned at chosen positions over chosen time windows.
    text_overlays: list[TextOverlay] = Field(default_factory=list)
    # PNG stickers (alpha) overlaid at chosen positions over chosen time windows.
    stickers: list[Sticker] = Field(default_factory=list)
    cleanup: bool = False
    render_start: Optional[float] = None
    render_end: Optional[float] = None
    # Soundboard sound effects mixed into the audio (one-shot + range/loop).
    sfx: list[SfxPlacement] = Field(default_factory=list)
    # Top/bottom split: video crop fills top_eighths/8 of the height (3, 3.5 or
    # 4); the illustration slot takes the rest. 3.0 = the original 720/1200.
    top_eighths: float = 3.0
    # Windows where the video fills the whole 9:16 frame (no illustration).
    fullscreen_windows: list[FullscreenWindow] = Field(default_factory=list)
    # Multi-segment KEEP trim: only these windows survive (concatenated), the rest
    # is cut. Overrides render_start/render_end (same as clipper).
    keep_segments: list[KeepSegment] = Field(default_factory=list)
    # Optional per-render token: ffmpeg progress is published under it so the UI can
    # poll /api/render-progress and show a live percent (per sub-clip).
    progress_id: Optional[str] = None


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
    head_room: float = 0.10     # extra TOP-only headroom for the person box (no chin/forehead clip)
    smooth: bool = True
    lock_size: bool = True   # lock one box size across the range (pan only) — stable framing
    director: bool = False      # windowed shot-director pre-pass (frames+transcript → richer segments+pan)
    diarization: bool = False   # add the dominant-speaker hint (needs pyannote + HF token)
    expect: str = ""            # desired-OUTPUT expectation prompt (guides the director)


class AutoBoxResponse(BaseModel):
    keyframes: list[Keyframe] = Field(default_factory=list)
    sampled: int = 0
    detected: int = 0
    message: str = ""
    director_note: str = ""     # the director's segment timeline (when director=True)


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
    Python-dict single-quote style the user pastes). `room_id` = the room the new
    jobs join (None = unassigned)."""
    content: str
    room_id: Optional[int] = None


class RoomCreate(BaseModel):
    name: str


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
    # Per-clip top/bottom split (3, 3.5 or 4 eighths for the video slot).
    top_eighths: Optional[float] = None
    # Full editor state JSON (sub-clips + buffer) — auto-saved so nothing is lost.
    editor_state: Optional[str] = None
