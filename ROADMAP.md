# Roadmap / Notes — ILLUSTRATOR

Planned future work. Issue-style so it can be copy-pasted into GitHub Issues once
this project gets its own repo (it isn't a git repo yet — see CLAUDE.md).

---

## Thumbnail generator (cover image for the clip) — enhancement

**Status:** planned

**Why**

Same need as clipper: after rendering, you need a **thumbnail / cover image** to
post the short (TikTok/Shorts/Reels). Today only the video comes out; the cover is
made in a separate tool.

**Proposed**

A **"Generate Thumbnail"** button that produces a cover IMAGE for the clip:
- **Frame pick** — scrub + grab a frame, or auto-suggest (brightest / face frame,
  or reuse the top crop box keyframe times the user already set).
- **Bold title text** overlay — reuse the bundled **Anton / Bebas Neue** + the
  fat-stroke caption style (separate from the burned karaoke captions), with
  position + accent-color options.
- **Aspect/size** — primarily **1080×1920 (9:16)** to match the output, plus
  optional 1280×720 (16:9) / 1080×1080 (square). Export **PNG/JPG** to `output/`.
- **Compose with the illustration** (illustrator-specific) — the thumbnail can mix
  the top video frame + the picked bottom illustration, or just one. Could offer a
  few **candidate thumbnails** (different frames / illustrations / text) to pick
  from, reusing the existing illustration-picker UI pattern.
- **Stretch** — subject cutout (person segmentation) over an accent/illustration bg.

**Implementation notes**

- New route `POST /api/thumbnail` `{job_id, frame_time, title, font, size, accent,
  illustration_url?}` → ffmpeg single-frame render (`-ss frame_time -frames:v 1`,
  ASS/drawtext text via the bundled fonts + `to_ass_color()`, compose top crop +
  bottom illustration like the main render, scale/pad to chosen size) →
  `output/{slug}_thumb.png`. **ffmpeg-only, no new heavy deps.**
- Reuse `assets/fonts/`, `renderer.to_ass_color()`, the crop/compose machinery in
  `renderer.py`, and `illustrator.download_pick()` for the chosen illustration.

**Acceptance**

- One click → a ready-to-post 9:16 thumbnail image with bold title text (and
  optionally the chosen illustration), saved next to the rendered video.

---

## (placeholder for future items)

When this project gets a GitHub repo, move these into Issues and keep this file as
the short backlog index (like clipper's ROADMAP.md + CLAUDE.md split).
