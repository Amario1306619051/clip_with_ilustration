// ILLUSTRATOR frontend — single crop box (top) + AI illustration track (bottom).
// Vanilla JS, no build step. Coordinate spaces mirror clipper:
//   1. source pixel space — boxes stored here (video native res)
//   2. overlay display space — mouse events, = canvas size
//   3. preview canvas space — fixed 270×480 mock-up
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const PREVIEW_W = 270, PREVIEW_H = 480;
// Top slot = top_eighths/8 of the height — CHOSEN per clip (3, 3.5 or 4). The
// crop box renders into this slot, so the slot boundary + the box's target AR
// both depend on the split. Functions (not consts) so they track state.topEighths.
function topFrac() { return (state.topEighths || 3) / 8; }          // 0.375 · 0.4375 · 0.5
function topPH() { return Math.round(PREVIEW_H * topFrac()); }      // slot boundary in the preview
// The box is FREE-FORM (any size); a render-area guide shows what survives the
// cover-crop into the top slot (AR = 1080 : slot-height).
function boxAR() { return 1080 / (1920 * topFrac()); }             // 1.5 · 1.286 · 1.125

// Apply a top/bottom split (3 · 3.5 · 4 eighths): snap to an allowed value, sync
// state + the selector, and redraw the crop guide (its AR changes) + the preview
// (the slot boundary moves). Does NOT autosave — callers decide (job-open must not).
function applyTopEighths(v) {
  const allowed = [3, 3.5, 4];
  let e = Number(v) || 3;
  e = allowed.reduce((a, b) => (Math.abs(b - e) < Math.abs(a - e) ? b : a), 3);
  state.topEighths = e;
  const sel = document.querySelector('#top-eighths');
  if (sel) sel.value = String(e);
  if (typeof drawOverlay === 'function') drawOverlay();
  if (typeof drawPreview === 'function') drawPreview(document.querySelector('#preview'));
}

// Centered slot-AR sub-rect that survives a cover-crop (the rest is cropped).
// Mirrors the backend cover math + the preview. Pure.
function coverKeepRect(box, ar) {
  const boxAR = box.w / box.h;
  let kw, kh;
  if (boxAR > ar) { kh = box.h; kw = box.h * ar; }  // box wider → crop L/R
  else { kw = box.w; kh = box.w / ar; }             // box taller → crop T/B
  return { x: box.x + (box.w - kw) / 2, y: box.y + (box.h - kh) / 2, w: kw, h: kh };
}

// Free-form clamp into the source frame (size-preserving slide; shrink only if
// bigger than the frame). Stops an off-frame box from crashing the backend crop.
function clampToSource(r, srcW, srcH) {
  let { x, y, w, h } = r;
  w = Math.min(w, srcW); h = Math.min(h, srcH);
  x = Math.max(0, Math.min(x, srcW - w));
  y = Math.max(0, Math.min(y, srcH - h));
  return { x, y, w, h };
}

const state = {
  jobId: null,
  duration: 0,
  srcW: 0, srcH: 0,
  box: [],          // keyframes: {t,x,y,w,h,interp,fit,gap}
  words: [],
  segments: [],     // plan result; each gets .picked (url or null)
  segSeconds: 5,
  topEighths: 3,    // top video slot height in eighths (3 · 3.5 · 4); bottom = rest
  keep: [],         // Trim: keep-windows {start,end} (everything else cut at render)
  keepA: null,      // pending in-point while marking A→B
  keepDrag: null,   // drag state for a trim bar
  subclips: [],     // sub-clips: one source → many exports [{name,start,end,selected,snap}]
  activeSub: null,  // index of the sub-clip selected (for Trim editing) / loaded
  subDrag: null,    // drag state for a sub-clip range bar
  subA: null,       // pending sub-clip range start (Sub-clips page)
  subB: null,       // pending sub-clip range end
  subTrimA: null,   // pending Trim in-point for the selected sub-clip
  subTrimDrag: null,// drag state for a sub-clip trim bar
  capPos: [],       // caption-position windows {start,end,pos} (else default cap-pos)
  capPosA: null,    // pending in-point while marking a caption-pos window
  capPosDrag: null, // drag state for a caption-pos bar
  overlays: [],     // text overlays {text,start,end,x_frac,y_frac,size,font,color}
  ovlSel: null,     // index of the overlay being edited in the controls
  ovlDrag: null,    // drag state for an overlay bar
  stickers: [],     // PNG stickers {url,thumb,start,end,x_frac,y_frac,scale,opacity}
  stkSel: null,     // index of the sticker being edited
  stkDrag: null,    // drag state for a sticker bar

  activeBox: null,  // null | 1
  currentTime: 0,
  abDrag: null,                          // 'start' | 'end' while dragging a range handle
  cutDrag: null,                         // drag state for a crop cut bar (Crop step)
  illDrag: null,                         // drag state for an illustration timing bar (Illustration step)
  autoRange: { start: 0, end: null },    // AI auto-box time range (null end = clip end)
  activeQueueKey: null,                  // batch-queue job currently loaded (null = ad-hoc)
  queueSig: null,                        // signature of last auto-saved queue state
  sounds: [],                            // soundboard library (from /api/soundboard)
  rooms: [],                             // queue rooms [{id,name,jobs}]
  sfx: [],                               // SFX placements for this clip (sent at render)
  sfxPreview: null,                      // currently-playing preview Audio
};

// ───────────────────────── step navigation ─────────────────────────
function showStep(n) {
  if (typeof illPreviewStop === 'function') illPreviewStop();   // stop the preview's audio on any navigation
  $$('.panel').forEach((p) => p.classList.add('hidden'));
  $(`#panel-${n}`).classList.remove('hidden');
  $$('.steps .step').forEach((b) => b.classList.toggle('active', b.dataset.step === String(n)));
  if (n === 2) { resizeOverlay(); drawOverlay(); renderKfList(); renderAutoRange();
                 renderTimelineDots(); renderCutLane(); updateTimelineCursor(); }
  if (n === 4) { drawPreview($('#preview-2')); refreshRenderSubs(); }
  if (n === 5) requestAnimationFrame(initThumbStep);
  if (n === 6) requestAnimationFrame(initSfxStep);
  if (n === 3) { populateActiveSubPicker($('#ill-active-sub')); renderSegments(); requestAnimationFrame(initIllPreview); }
  if (n === 7) requestAnimationFrame(initSubStep);
  if (n === 8) requestAnimationFrame(initOvStep);
  if (n === 9) requestAnimationFrame(initTrimStep);
}
function initSubStep() {
  if (subVideo && video) {
    const src = video.currentSrc || video.src;
    if (src && subVideo.src !== src) subVideo.src = src;
  }
  const scr = $('#sub-scrubber');
  if (scr && subVideo && subVideo.duration) scr.max = subVideo.duration;
  renderSubclips(); updateSubRangeLbl();
  requestAnimationFrame(updateSubCursor);
}
$$('.steps .step').forEach((b) => b.addEventListener('click', () => {
  const n = Number(b.dataset.step);
  if (n > 1 && !state.jobId) return;
  showStep(n);
}));
$$('[data-go]').forEach((b) => b.addEventListener('click', () => showStep(Number(b.dataset.go))));

function setStatus(el, msg, kind) {
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

async function api(path, body) {
  const res = await fetch('/api/' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

// ───────────────────────── step 1: download ─────────────────────────
const video = $('#source-video');

$('#btn-download').addEventListener('click', async () => {
  const url = $('#f-url').value.trim();
  if (!url) { setStatus($('#dl-status'), 'URL is empty', 'err'); return; }
  setStatus($('#dl-status'), 'Downloading + trimming…');
  $('#btn-download').disabled = true;
  try {
    const r = await api('download', {
      url,
      start: $('#f-start').value.trim() || '00:00:00',
      end: $('#f-end').value.trim() || null,
      title: $('#f-title').value.trim() || 'clip',
      description: $('#f-desc').value.trim(),
    });
    state.jobId = r.job_id;
    state.duration = r.duration;
    state.autoRange = { start: 0, end: r.duration };
    state.srcW = r.width; state.srcH = r.height;
    state.sfx = [];                // placements are per-clip
    state.keep = []; state.keepA = null;   // trim windows are per-clip
    state.capPos = []; state.capPosA = null;   // caption-position windows are per-clip
    state.overlays = []; state.ovlSel = null;   // text overlays are per-clip
    state.stickers = []; state.stkSel = null;   // stickers are per-clip
    _subRenderRes = {};                          // per-sub-clip render outcomes
    state.subclips = []; state.activeSub = null;   // sub-clips are per-session
    state.subA = null; state.subB = null; state.subTrimA = null;
    state.autoObs = '';            // no model observation for an ad-hoc clip
    if ($('#ab-context')) $('#ab-context').value = '';   // context is per-clip
    state.activeQueueKey = null;   // ad-hoc download — not editing a queue job
    histReset();                   // fresh clip → clear edit history
    video.src = r.video_path;
    $('#range-meta').textContent = `source: ${r.width}×${r.height} · ${r.duration.toFixed(1)}s`;
    setStatus($('#dl-status'), `OK · ${r.width}×${r.height} · ${r.duration.toFixed(1)}s`, 'ok');
    showStep(2);
  } catch (e) {
    setStatus($('#dl-status'), e.message, 'err');
  } finally {
    $('#btn-download').disabled = false;
  }
});

// ───────────────────────── video controls ─────────────────────────
const subVideo = $('#sub-video');
const ovVideo = $('#ov-video');
const trimVideo = $('#trim-video');
// Playhead for the Text & Stickers page (its own video); falls back to the main
// editor time if that page hasn't loaded a source yet.
function ovPlayhead() { return (ovVideo && ovVideo.duration) ? (ovVideo.currentTime || 0) : (state.currentTime || 0); }
function ovDur() { return (ovVideo && ovVideo.duration) || cutDur() || 1; }
const overlay = $('#overlay');
const octx = overlay.getContext('2d');
const scrubber = $('#scrubber');

function fmtTime(t) {
  const m = Math.floor(t / 60), s = (t % 60);
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}

video.addEventListener('loadedmetadata', () => {
  if (!state.srcW) { state.srcW = video.videoWidth; state.srcH = video.videoHeight; }
  scrubber.max = video.duration || state.duration || 1;
  $('#time-dur').textContent = fmtTime(video.duration || 0);
  resizeOverlay();
  drawOverlay();
  try { renderTrimTrack(); renderCapPosTrack(); renderOverlayTrack(); renderOverlayList(); renderStickerTrack(); renderStickerList(); renderSubclips(); } catch (e) { /* not ready */ }
});
video.addEventListener('timeupdate', () => {
  state.currentTime = video.currentTime;
  scrubber.value = video.currentTime;
  $('#time-cur').textContent = fmtTime(video.currentTime);
  $('#cur-time').textContent = video.currentTime.toFixed(2) + 's';
  updateTimelineCursor();
  updateKfListHighlight();
  drawOverlay();
  drawPreview($('#preview'));
  // live short-preview on the Illustration step (driven by the same video)
  const ip = $('#preview-ill'); if (ip) { drawPreview(ip); const sc = $('#ill-scrub'); if (sc && document.activeElement !== sc) sc.value = video.currentTime; }
});
video.addEventListener('seeked', () => { drawPreview($('#preview')); const ip = $('#preview-ill'); if (ip) drawPreview(ip); });
$('#ill-scrub') && $('#ill-scrub').addEventListener('input', (e) => { if (video.duration) { illPreviewStop(); video.currentTime = +e.target.value; } });

// ── Illustration short-preview playback (with sound) ──────────────────────
// Plays the ACTIVE sub-clip's range on the main video (audio on) and redraws the
// composite canvas via rAF for smoothness. The range clamp only runs while THIS
// preview is driving playback, so Crop-step playback is unaffected.
let _illRaf = 0, _illPlaying = false;
function _illRange() {
  const asc = state.subclips[state.activeSub];
  return { lo: asc ? asc.start : 0, hi: asc ? asc.end : (video.duration || state.duration || 0) };
}
// The windows the preview actually plays = the trim KEEP windows, clamped to the
// sub-clip range. No trim set → the whole sub-clip is one window. This makes the
// preview skip the cut-out parts exactly like the render does.
function _keepsIn(keep, lo, hi) {
  let ks = (keep || [])
    .map((w) => ({ a: Math.max(lo, w.start), b: Math.min(hi, w.end) }))
    .filter((w) => w.b > w.a + 0.02)
    .sort((x, y) => x.a - y.a);
  if (!ks.length) ks = [{ a: lo, b: hi }];
  return ks;
}
function _illKeeps() { const { lo, hi } = _illRange(); return _keepsIn(state.keep, lo, hi); }

// How a segment [t_start,t_end] overlaps the keep windows → fully kept / partial /
// fully cut. Defaults to the ACTIVE sub-clip's trim; pass (keep,lo,hi) for another.
function segTrimInfo(seg, keep, lo, hi) {
  const ks = (keep === undefined) ? _illKeeps() : _keepsIn(keep, lo, hi);
  const segDur = Math.max(0.001, seg.t_end - seg.t_start);
  let kept = 0;
  for (const w of ks) {
    const a = Math.max(seg.t_start, w.a), b = Math.min(seg.t_end, w.b);
    if (b > a) kept += b - a;
  }
  return { kept, segDur, fullyCut: kept < 0.05, partial: kept >= 0.05 && kept < segDur - 0.05 };
}
function _illPlayLoop() {
  if (!_illPlaying) return;
  state.currentTime = video.currentTime;   // sync at 60Hz so the Ken Burns motion is smooth (timeupdate is only ~4Hz)
  const ip = $('#preview-ill'); if (ip) drawPreview(ip);
  const sc = $('#ill-scrub'); if (sc && document.activeElement !== sc) sc.value = video.currentTime;
  if (video.paused) { illPreviewStop(); return; }
  const ks = _illKeeps();
  const t = video.currentTime;
  const last = ks[ks.length - 1];
  if (t >= last.b - 0.03) { video.currentTime = last.b; illPreviewStop(); return; }   // past the last kept window
  if (!ks.some((w) => t >= w.a - 0.05 && t < w.b)) {           // sitting in a cut → jump to the next kept window
    const next = ks.find((w) => w.a > t);
    if (next) video.currentTime = next.a; else { illPreviewStop(); return; }
  }
  _illRaf = requestAnimationFrame(_illPlayLoop);
}
function illPreviewPlay() {
  const ks = _illKeeps();
  const t = video.currentTime;
  if (!ks.some((w) => t >= w.a - 0.05 && t < w.b - 0.05)) video.currentTime = ks[0].a;   // start at the first kept window
  video.muted = false;
  _illPlaying = true;
  const b = $('#ill-play'); if (b) b.textContent = '❚❚';
  video.play().catch(() => {});
  cancelAnimationFrame(_illRaf); _illRaf = requestAnimationFrame(_illPlayLoop);
}
function illPreviewStop() {
  if (!_illPlaying) return;
  _illPlaying = false;
  cancelAnimationFrame(_illRaf);
  const b = $('#ill-play'); if (b) b.textContent = '▶';
  if (!video.paused) video.pause();
}
$('#ill-play') && $('#ill-play').addEventListener('click', () => { _illPlaying ? illPreviewStop() : illPreviewPlay(); });
video.addEventListener('pause', () => { if (_illPlaying) illPreviewStop(); });
$('#btn-play').addEventListener('click', () => { video.paused ? video.play() : video.pause(); });
video.addEventListener('play', () => { $('#btn-play').textContent = '❚❚'; });
video.addEventListener('pause', () => { $('#btn-play').textContent = '▶'; });
$('#btn-back-1').addEventListener('click', () => { video.currentTime = Math.max(0, video.currentTime - 1); });
$('#btn-fwd-1').addEventListener('click', () => { video.currentTime = Math.min(video.duration, video.currentTime + 1); });
scrubber.addEventListener('input', () => { video.currentTime = Number(scrubber.value); });

window.addEventListener('keydown', (e) => {
  if ($('#panel-2').classList.contains('hidden')) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  if (e.key === ' ') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  else if (e.key === 'ArrowLeft') { video.currentTime = Math.max(0, video.currentTime - 1); }
  else if (e.key === 'ArrowRight') { video.currentTime = Math.min(video.duration, video.currentTime + 1); }
  else if (e.key === '1') { toggleArm(1); }
  else if (e.key === 'Escape') { state.activeBox = null; refreshArmUI(); }
});

// Spacebar plays/pauses the short preview while on the Illustration step.
window.addEventListener('keydown', (e) => {
  if ($('#panel-3').classList.contains('hidden')) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  if (e.key === ' ') { e.preventDefault(); _illPlaying ? illPreviewStop() : illPreviewPlay(); }
});

// ───────────────────────── coordinate transforms ─────────────────────────
function resizeOverlay() {
  const w = video.clientWidth, h = video.clientHeight;
  if (!w || !h) return;
  overlay.width = w; overlay.height = h;
}
window.addEventListener('resize', () => { resizeOverlay(); drawOverlay(); });

function overlayToSource(mx, my) {
  const sx = state.srcW / overlay.width;
  const sy = state.srcH / overlay.height;
  return { x: mx * sx, y: my * sy };
}
function sourceToOverlay(x, y) {
  const sx = overlay.width / state.srcW;
  const sy = overlay.height / state.srcH;
  return { x: x * sx, y: y * sy };
}

// ───────────────────────── keyframe model ─────────────────────────
function sortedKfs() { return [...state.box].sort((a, b) => a.t - b.t); }

// value of the box at time t (mirror backend hold/linear semantics)
function boxAt(t) {
  const kfs = sortedKfs();
  if (kfs.length === 0) return null;
  if (kfs.length === 1) return kfs[0];
  if (t <= kfs[0].t) return kfs[0];
  for (let i = 0; i < kfs.length - 1; i++) {
    const k0 = kfs[i], k1 = kfs[i + 1];
    if (t >= k0.t && t < k1.t) {
      if ((k0.interp || 'hold') === 'linear' && !k1.gap && k1.t > k0.t) {
        const f = (t - k0.t) / (k1.t - k0.t);
        return {
          x: k0.x + (k1.x - k0.x) * f, y: k0.y + (k1.y - k0.y) * f,
          w: k0.w + (k1.w - k0.w) * f, h: k0.h + (k1.h - k0.h) * f,
          fit: k0.fit, interp: k0.interp, gap: k0.gap,
        };
      }
      return k0;
    }
  }
  return kfs[kfs.length - 1];
}

function nearestKf(t) {
  const kfs = sortedKfs();
  if (!kfs.length) return null;
  return kfs.reduce((best, k) => Math.abs(k.t - t) < Math.abs(best.t - t) ? k : best, kfs[0]);
}

function upsertKf(rect) {
  const t = state.currentTime;
  const near = nearestKf(t);
  const fit = near ? near.fit : 'cover';
  const interp = near ? near.interp : 'hold';
  // replace if a kf already sits within 0.15s of current time
  const existing = state.box.find((k) => Math.abs(k.t - t) < 0.15);
  if (existing) {
    Object.assign(existing, rect);
  } else {
    state.box.push({ t, ...rect, interp, fit, gap: false });
  }
  refreshKfUI();
}

function refreshKfUI() {
  $('#kf-count-1').textContent = state.box.length + ' kf';
  renderKfList();
  renderTimelineDots();
  renderCutLane();
  drawOverlay();
  drawPreview($('#preview'));
}

$('[data-clear="1"]').addEventListener('click', () => { state.box = []; refreshKfUI(); });

// ───────────────────────── crop cut bars ─────────────────────────
// A "cut" empties the crop over a time range — stored as a GAP keyframe at the
// start + a restore keyframe at the end, so it renders BLACK and persists like
// any gap (no backend change). Each gap-region is a draggable bar: body = move,
// edges = resize, × = restore the crop.
const CUT_MIN = 0.2;
function cutDur() { return video.duration || state.duration || 1; }

function renderCutLane() {
  const host = $('#cuts-1');
  if (!host) return;
  const dur = cutDur();
  const kfs = sortedKfs();
  const bars = [];
  kfs.forEach((k, i) => {
    const start = k.t;
    const end = (i + 1 < kfs.length) ? kfs[i + 1].t : dur;
    if (end - start <= 0.001) return;
    const left = (start / dur) * 100;
    const width = Math.max(0.8, ((end - start) / dur) * 100);
    const endLbl = end >= dur - 1e-3 ? 'end' : end.toFixed(2) + 's';
    let cls, lbl;
    if (k.gap) {
      cls = k.dynamic ? 'cut-bar gap dynamic' : 'cut-bar gap';
      lbl = (k.dynamic ? 'DYNAMIC' : 'OFF') + ' ' + (end - start).toFixed(1) + 's';
    } else {
      cls = 'cut-bar on';
      lbl = `${Math.round(k.w)}×${Math.round(k.h)}`;
    }
    bars.push(
      `<div class="${cls}" data-i="${i}" style="left:${left}%;width:${width}%" ` +
      `title="Crop ${start.toFixed(2)}–${endLbl}${k.gap ? ' (off)' : ''} — drag body to move, edges to resize` +
      `${k.gap ? ', double-click to restore' : ''}">` +
      `<span class="cut-bar-h l"></span>` +
      `<span class="cut-bar-lbl">${lbl}</span>` +
      `<span class="cut-bar-h r"></span>` +
      `</div>`);
  });
  host.innerHTML = bars.join('') || '<span class="cut-lane-empty">— no boxes —</span>';
}

function _cutUpsertAt(t, props) {
  const ex = state.box.find((k) => Math.abs(k.t - t) < 0.15);
  if (ex) Object.assign(ex, props, { t });
  else state.box.push({ t, interp: 'hold', ...props });
}

function addBoxCut() {
  if (!state.box.length) {
    setStatus($('#tr-status'), 'Crop has no box to cut — draw or auto-box it first', 'err');
    return;
  }
  const dur = cutDur();
  const a = state.currentTime;
  const b = Math.min(a + 2.0, dur);
  if (b - a < CUT_MIN + 1e-3) {
    setStatus($('#tr-status'), 'Move the playhead earlier — not enough room to cut here', 'err');
    return;
  }
  const at = boxAt(b);
  const resumeOn = !!(at && !at.gap);
  const prior = sortedKfs().filter((k) => k.t <= a + 0.15).pop();
  const ref = (resumeOn ? at : prior) || { x: 0, y: 0, w: state.srcW || video.videoWidth || 100,
                                           h: state.srcH || video.videoHeight || 100 };
  const fit = (prior && prior.fit) || 'cover';
  state.box = state.box.filter((k) => !(k.t > a + 0.15 && k.t < b - 0.15));
  _cutUpsertAt(a, { x: ref.x, y: ref.y, w: ref.w, h: ref.h, interp: 'hold', fit, gap: true });
  if (resumeOn && b < dur - 1e-3) {
    _cutUpsertAt(b, { x: at.x, y: at.y, w: at.w, h: at.h, interp: 'hold', fit: at.fit || fit, gap: false });
  }
  setStatus($('#tr-status'),
    `Crop cut ${a.toFixed(2)}–${b.toFixed(2)}s · drag the bar to adjust, × to restore`, 'ok');
  refreshKfUI();
}

function addBoxSplit() {
  // Carve an EDITABLE sub-segment out of the crop (vs addBoxCut which carves a
  // black gap): the middle window starts as a COPY of the current box, the
  // surrounding box resumes after — scrub into the window + redraw to change ONLY
  // that stretch (e.g. frames 40–50 get a different crop, 1–40 / 50–100 stay).
  if (!state.box.length) {
    setStatus($('#tr-status'), 'Crop has no box to split — draw or auto-box it first', 'err');
    return;
  }
  const dur = cutDur();
  const a = state.currentTime;
  const cur = boxAt(a);
  if (!cur || cur.gap) {
    setStatus($('#tr-status'), 'Crop is empty (off) here — scrub to where it shows, then split', 'err');
    return;
  }
  const b = Math.min(a + 2.0, dur);
  if (b - a < CUT_MIN + 1e-3) {
    setStatus($('#tr-status'), 'Move the playhead earlier — not enough room to split here', 'err');
    return;
  }
  const at = boxAt(b);
  const resumeOn = !!(at && !at.gap);
  const fit = cur.fit || 'cover';
  state.box = state.box.filter((k) => !(k.t > a + 0.15 && k.t < b - 0.15));
  _cutUpsertAt(a, { x: cur.x, y: cur.y, w: cur.w, h: cur.h, interp: 'hold', fit, gap: false });
  if (resumeOn && b < dur - 1e-3) {
    _cutUpsertAt(b, { x: at.x, y: at.y, w: at.w, h: at.h, interp: 'hold', fit: at.fit || fit, gap: false });
  }
  setStatus($('#tr-status'),
    `Crop split ${a.toFixed(2)}–${b.toFixed(2)}s · scrub into this window + draw to change it; drag the bar edges to resize`, 'ok');
  refreshKfUI();
}

function onCutBarDblClick(e) {
  const bar = e.target.closest('.cut-bar');
  if (!bar) return;
  removeBoxCut(+bar.dataset.i);
}

function onCutBarDown(e) {
  const bar = e.target.closest('.cut-bar');
  if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i;
  const kfs = sortedKfs();
  const gapK = kfs[i];   // the kf owning this segment (gap OR a real box) — drag retimes it
  if (!gapK) return;
  const nextK = (i + 1 < kfs.length) ? kfs[i + 1] : null;
  const dur = cutDur();
  const rect = $('#cuts-1').getBoundingClientRect();
  const h = e.target.classList.contains('cut-bar-h');
  const mode = h && e.target.classList.contains('l') ? 'resize-l'
             : h && e.target.classList.contains('r') ? 'resize-r'
             : 'move';
  state.cutDrag = {
    mode, startX: e.clientX, trackW: rect.width, gapK, nextK,
    a0: gapK.t, b0: nextK ? nextK.t : dur,
    hasPrev: i > 0, prevT: i > 0 ? kfs[i - 1].t : 0,
    afterT: nextK ? (i + 2 < kfs.length ? kfs[i + 2].t : dur) : dur,
  };
}

function onCutDragMove(e) {
  const d = state.cutDrag;
  if (!d) return;
  const dur = cutDur();
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  if (d.mode === 'resize-l') {
    const lo = d.hasPrev ? d.prevT + CUT_MIN : 0;
    d.gapK.t = +Math.max(lo, Math.min(d.a0 + dt, d.b0 - CUT_MIN)).toFixed(2);
  } else if (d.mode === 'resize-r' && d.nextK) {
    const ne = Math.max(d.gapK.t + CUT_MIN, Math.min(d.b0 + dt, d.afterT - CUT_MIN));
    d.nextK.t = +Math.min(dur, ne).toFixed(2);
  } else if (d.mode === 'move') {
    const len = d.b0 - d.a0;
    const lo = d.hasPrev ? d.prevT : 0;   // just don't cross the prior kf
    const hi = d.nextK ? d.afterT - len : dur - CUT_MIN;
    const ns = Math.max(lo, Math.min(d.a0 + dt, hi));
    d.gapK.t = +ns.toFixed(2);
    if (d.nextK) d.nextK.t = +(ns + len).toFixed(2);
  }
  state.box.sort((x, y) => x.t - y.t);   // keep state.box canonical (matches the sorted render)
  renderCutLane();
  drawPreview($('#preview'));
}

function _boxesEqual(a, b) {
  return a && b && Math.abs(a.x - b.x) < 1 && Math.abs(a.y - b.y) < 1
    && Math.abs(a.w - b.w) < 1 && Math.abs(a.h - b.h) < 1;
}

function removeBoxCut(i) {
  const kfs = sortedKfs();
  const k = kfs[i];
  if (!k || !k.gap) return;
  const rest = kfs[i + 1], prev = kfs[i - 1];
  state.box = state.box.filter((x) => x !== k);   // drop gap → crop restores
  // Drop a now-redundant restore kf (its box == the kf before it → a no-op marker).
  if (rest && !rest.gap && prev && !prev.gap && _boxesEqual(rest, prev)) {
    state.box = state.box.filter((x) => x !== rest);
  }
  setStatus($('#tr-status'), 'Crop cut removed — box restored here', 'ok');
  refreshKfUI();
}

$('#btn-cut-1').addEventListener('click', addBoxCut);
$('#btn-split-1') && $('#btn-split-1').addEventListener('click', addBoxSplit);
// Top/bottom split selector — redraw the guide/preview, then persist per job.
$('#top-eighths') && $('#top-eighths').addEventListener('change', (e) => {
  applyTopEighths(e.target.value);
  autosaveQueue();
});
$('#cap-pos') && $('#cap-pos').addEventListener('change', () => drawPreview($('#preview')));
$('#cuts-1').addEventListener('mousedown', onCutBarDown);
$('#cuts-1').addEventListener('dblclick', onCutBarDblClick);
window.addEventListener('mousemove', onCutDragMove);
window.addEventListener('mouseup', () => { if (state.cutDrag) { state.cutDrag = null; refreshKfUI(); } });

// Trim (keep-windows)
$('#btn-trim-a') && $('#btn-trim-a').addEventListener('click', setKeepA);
$('#btn-trim-b') && $('#btn-trim-b').addEventListener('click', setKeepB);
$('#btn-trim-here') && $('#btn-trim-here').addEventListener('click', addKeepHere);
$('#btn-trim-clear') && $('#btn-trim-clear').addEventListener('click', clearKeep);
$('#trim-track') && $('#trim-track').addEventListener('mousedown', onTrimTrackDown);
$('#trim-track') && $('#trim-track').addEventListener('dblclick', onTrimDblClick);
window.addEventListener('mousemove', onTrimDragMove);
window.addEventListener('mouseup', () => { if (state.keepDrag) { state.keepDrag = null; syncActiveSub(); renderTrimList(); } });
// Trim page: pick which sub-clip to trim → load it (its keep = state.keep)
$('#ill-active-sub') && $('#ill-active-sub').addEventListener('change', (e) => {
  selectSub(e.target.value === '' ? null : +e.target.value);
  renderSegments();
  initIllPreview();
});
$('#ov-active-sub') && $('#ov-active-sub').addEventListener('change', (e) => {
  selectSub(e.target.value === '' ? null : +e.target.value);
  renderOverlayTrack(); renderOverlayList(); renderStickerTrack(); renderStickerList();
});
$('#trim-subclip') && $('#trim-subclip').addEventListener('change', (e) => {
  selectSub(e.target.value === '' ? null : +e.target.value);
  applyTrimWindow();   // scrubber min/max + seek into the sub-clip's window
  renderTrimTrack();
});
// Trim page video + scrubber (own player, same source)
if (trimVideo) {
  $('#trim-play') && $('#trim-play').addEventListener('click', () => { if (trimVideo.paused) trimVideo.play(); else trimVideo.pause(); });
  trimVideo.addEventListener('play', () => { const b = $('#trim-play'); if (b) b.textContent = '❚❚'; });
  trimVideo.addEventListener('pause', () => { const b = $('#trim-play'); if (b) b.textContent = '▶'; });
  trimVideo.addEventListener('loadedmetadata', () => {
    applyTrimWindow();
    renderTrimTrack();
  });
  trimVideo.addEventListener('timeupdate', () => {
    const { hi } = trimWindow();
    if (!trimVideo.paused && trimVideo.currentTime > hi) { trimVideo.pause(); trimVideo.currentTime = hi; }
    const t = $('#trim-time'); if (t) t.textContent = fmtTime(trimVideo.currentTime || 0);
    const scr = $('#trim-scrubber'); if (scr && document.activeElement !== scr) scr.value = trimVideo.currentTime || 0;
    updateTrimCursor();
  });
  $('#trim-scrubber') && $('#trim-scrubber').addEventListener('input', (e) => { if (trimVideo.duration) trimVideo.currentTime = +e.target.value; });
}

// Caption position windows (top/middle/bottom over time)
$('#btn-cappos-a') && $('#btn-cappos-a').addEventListener('click', setCapPosA);
$('#btn-cappos-b') && $('#btn-cappos-b').addEventListener('click', setCapPosB);
$('#btn-cappos-clear') && $('#btn-cappos-clear').addEventListener('click', clearCapPos);
$('#cap-pos-track') && $('#cap-pos-track').addEventListener('mousedown', onCapPosDown);
$('#cap-pos-track') && $('#cap-pos-track').addEventListener('dblclick', onCapPosDbl);
window.addEventListener('mousemove', onCapPosMove);
window.addEventListener('mouseup', () => { if (state.capPosDrag) state.capPosDrag = null; });

// Text overlays
$('#btn-ovl-add') && $('#btn-ovl-add').addEventListener('click', addOverlay);
$('#btn-ovl-clear') && $('#btn-ovl-clear').addEventListener('click', () => { state.overlays = []; state.ovlSel = null; renderOverlayTrack(); renderOverlayList(); setStatus($('#ovl-status'), 'Cleared.', 'ok'); });
$('#ovl-text') && $('#ovl-text').addEventListener('input', () => editSelectedOverlay({ text: $('#ovl-text').value }));
$('#ovl-font') && $('#ovl-font').addEventListener('change', () => editSelectedOverlay({ font: $('#ovl-font').value }));
$('#ovl-size') && $('#ovl-size').addEventListener('input', () => editSelectedOverlay({ size: Math.min(200, Math.max(12, +$('#ovl-size').value || 56)) }));
$('#ovl-color') && $('#ovl-color').addEventListener('input', () => editSelectedOverlay({ color: $('#ovl-color').value }));
$('#ovl-grid') && $('#ovl-grid').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  editSelectedOverlay({ x_frac: +b.dataset.x, y_frac: +b.dataset.y });
  syncOverlayControls();
});
$('#btn-ovl-a') && $('#btn-ovl-a').addEventListener('click', () => {
  const o = state.overlays[state.ovlSel]; if (!o) { setStatus($('#ovl-status'), 'Select or add an overlay first.', 'err'); return; }
  const s = +ovPlayhead().toFixed(2);
  editSelectedOverlay({ start: s, end: Math.max(o.end, s + 0.2) });
  setStatus($('#ovl-status'), 'Start set to playhead.', 'ok');
});
$('#btn-ovl-b') && $('#btn-ovl-b').addEventListener('click', () => {
  const o = state.overlays[state.ovlSel]; if (!o) { setStatus($('#ovl-status'), 'Select or add an overlay first.', 'err'); return; }
  const e = +ovPlayhead().toFixed(2);
  editSelectedOverlay({ end: e, start: Math.min(o.start, e - 0.2) });
  setStatus($('#ovl-status'), 'End set to playhead.', 'ok');
});
$('#ovl-track') && $('#ovl-track').addEventListener('mousedown', onOvlDown);
$('#ovl-track') && $('#ovl-track').addEventListener('dblclick', onOvlDbl);
window.addEventListener('mousemove', onOvlMove);
window.addEventListener('mouseup', () => { if (state.ovlDrag) state.ovlDrag = null; });

// PNG stickers
$('#btn-stk-upload') && $('#btn-stk-upload').addEventListener('click', uploadSticker);
$('#stk-file') && $('#stk-file').addEventListener('change', onStickerFile);
$('#btn-stk-clear') && $('#btn-stk-clear').addEventListener('click', () => { state.stickers = []; state.stkSel = null; renderStickerTrack(); renderStickerList(); if ($('#stk-thumb')) $('#stk-thumb').src = ''; setStatus($('#stk-status'), 'Cleared.', 'ok'); });
$('#stk-scale') && $('#stk-scale').addEventListener('input', () => editSelectedSticker({ scale: +$('#stk-scale').value }));
$('#stk-opacity') && $('#stk-opacity').addEventListener('input', () => editSelectedSticker({ opacity: +$('#stk-opacity').value }));
$('#stk-grid') && $('#stk-grid').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  editSelectedSticker({ x_frac: +b.dataset.x, y_frac: +b.dataset.y });
  syncStickerControls();
});
$('#btn-stk-a') && $('#btn-stk-a').addEventListener('click', () => {
  const s = state.stickers[state.stkSel]; if (!s) { setStatus($('#stk-status'), 'Upload or select a sticker first.', 'err'); return; }
  const v = +ovPlayhead().toFixed(2);
  editSelectedSticker({ start: v, end: Math.max(s.end, v + 0.2) });
  setStatus($('#stk-status'), 'Start set to playhead.', 'ok');
});
$('#btn-stk-b') && $('#btn-stk-b').addEventListener('click', () => {
  const s = state.stickers[state.stkSel]; if (!s) { setStatus($('#stk-status'), 'Upload or select a sticker first.', 'err'); return; }
  const v = +ovPlayhead().toFixed(2);
  editSelectedSticker({ end: v, start: Math.min(s.start, v - 0.2) });
  setStatus($('#stk-status'), 'End set to playhead.', 'ok');
});
$('#stk-track') && $('#stk-track').addEventListener('mousedown', onStkDown);
$('#stk-track') && $('#stk-track').addEventListener('dblclick', onStkDbl);
window.addEventListener('mousemove', onStkMove);
window.addEventListener('mouseup', () => { if (state.stkDrag) state.stkDrag = null; });

// Sub-clips (dedicated page, step 7) — own synced video + timeline
$('#btn-sub-add') && $('#btn-sub-add').addEventListener('click', addSubclip);
$('#btn-sub-update') && $('#btn-sub-update').addEventListener('click', updateSubclip);
$('#btn-sub-render') && $('#btn-sub-render').addEventListener('click', renderAllSubclips);
$('#btn-sub-render-sel') && $('#btn-sub-render-sel').addEventListener('click', renderSelectedSubclips);
$('#btn-render-subs-sel') && $('#btn-render-subs-sel').addEventListener('click', renderSelectedSubclips);
$('#btn-render-subs-all') && $('#btn-render-subs-all').addEventListener('click', renderAllSubclips);
$('#btn-sub-start') && $('#btn-sub-start').addEventListener('click', () => setSubRange('start', subPlayhead()));
$('#btn-sub-end') && $('#btn-sub-end').addEventListener('click', () => setSubRange('end', subPlayhead()));
$('#subclip-track') && $('#subclip-track').addEventListener('mousedown', onSubTrackDown);
window.addEventListener('mousemove', onSubDragMove);
window.addEventListener('mouseup', () => {
  const d = state.subDrag; if (!d) return;
  state.subDrag = null;
  if (!d.moved) selectSub(d.i); else renderSubclips();
});
// per-sub-clip Trim lane
$('#btn-subtrim-a') && $('#btn-subtrim-a').addEventListener('click', subTrimSetA);
$('#btn-subtrim-b') && $('#btn-subtrim-b').addEventListener('click', subTrimSetB);
$('#btn-subtrim-clear') && $('#btn-subtrim-clear').addEventListener('click', subTrimClear);
$('#sub-trim-track') && $('#sub-trim-track').addEventListener('mousedown', onSubTrimDown);
$('#sub-trim-track') && $('#sub-trim-track').addEventListener('dblclick', onSubTrimDbl);
window.addEventListener('mousemove', onSubTrimMove);
window.addEventListener('mouseup', () => { if (state.subTrimDrag) { state.subTrimDrag = null; syncActiveSub(); renderTrimTrack(); renderSubclips(); } });
// Auto-save the editor buffer into the active sub-clip (every 1.5s while one is active).
setInterval(syncActiveSub, 1500);
// sub-clips page video + scrubber (its own player, same source)
if (subVideo) {
  $('#sub-play') && $('#sub-play').addEventListener('click', () => { if (subVideo.paused) subVideo.play(); else subVideo.pause(); });
  subVideo.addEventListener('play', () => { const b = $('#sub-play'); if (b) b.textContent = '❚❚'; });
  subVideo.addEventListener('pause', () => { const b = $('#sub-play'); if (b) b.textContent = '▶'; });
  subVideo.addEventListener('loadedmetadata', () => {
    const scr = $('#sub-scrubber'); if (scr) scr.max = subVideo.duration || 0;
    const d = $('#sub-dur'); if (d) d.textContent = fmtTime(subVideo.duration || 0);
    renderSubclips();
  });
  subVideo.addEventListener('timeupdate', () => {
    const t = $('#sub-time'); if (t) t.textContent = fmtTime(subVideo.currentTime || 0);
    const scr = $('#sub-scrubber'); if (scr && document.activeElement !== scr) scr.value = subVideo.currentTime || 0;
    updateSubCursor();
  });
  $('#sub-scrubber') && $('#sub-scrubber').addEventListener('input', (e) => { if (subVideo.duration) subVideo.currentTime = +e.target.value; });
}
// Text & Stickers page (step 8) video + scrubber (own player, same source)
if (ovVideo) {
  $('#ov-play') && $('#ov-play').addEventListener('click', () => { if (ovVideo.paused) ovVideo.play(); else ovVideo.pause(); });
  ovVideo.addEventListener('play', () => { const b = $('#ov-play'); if (b) b.textContent = '❚❚'; });
  ovVideo.addEventListener('pause', () => { const b = $('#ov-play'); if (b) b.textContent = '▶'; });
  ovVideo.addEventListener('loadedmetadata', () => {
    const scr = $('#ov-scrubber'); if (scr) scr.max = ovVideo.duration || 0;
    const d = $('#ov-dur'); if (d) d.textContent = fmtTime(ovVideo.duration || 0);
    renderOverlayTrack(); renderStickerTrack(); updateOvCursor();
  });
  ovVideo.addEventListener('timeupdate', () => {
    const t = $('#ov-time'); if (t) t.textContent = fmtTime(ovVideo.currentTime || 0);
    const scr = $('#ov-scrubber'); if (scr && document.activeElement !== scr) scr.value = ovVideo.currentTime || 0;
    updateOvCursor();
  });
  $('#ov-scrubber') && $('#ov-scrubber').addEventListener('input', (e) => { if (ovVideo.duration) ovVideo.currentTime = +e.target.value; });
}
function updateOvCursor() {
  const cur = $('#ov-cursor'), track = $('#ovl-track'), wrap = $('#ov-tl-wrap');
  if (!cur || !track || !wrap) return;
  const dur = ovDur() || 1;
  const wR = wrap.getBoundingClientRect(), tR = track.getBoundingClientRect();
  if (!tR.width) return;
  cur.style.left = ((tR.left - wR.left) + (ovPlayhead() / dur) * tR.width) + 'px';
}
function initOvStep() {
  if (ovVideo && video) {
    const src = video.currentSrc || video.src;
    if (src && ovVideo.src !== src) ovVideo.src = src;
  }
  const scr = $('#ov-scrubber');
  if (scr && ovVideo && ovVideo.duration) scr.max = ovVideo.duration;
  populateActiveSubPicker($('#ov-active-sub'));
  renderOverlayTrack(); renderOverlayList();
  renderStickerTrack(); renderStickerList();
  requestAnimationFrame(updateOvCursor);
}
// Generic "which sub-clip am I editing" picker for the Illustration & Text/Sticker
// steps (Trim & Thumbnail have their own). Reflects/sets the active sub-clip.
function populateActiveSubPicker(sel) {
  if (!sel) return;
  if (!state.subclips.length) { sel.innerHTML = '<option value="">Whole clip</option>'; sel.value = ''; return; }
  sel.innerHTML = '<option value="">Whole clip</option>' +
    state.subclips.map((sc, i) => `<option value="${i}">${(sc.name || ('sub ' + (i + 1))).replace(/</g, '')}</option>`).join('');
  sel.value = state.activeSub != null ? String(state.activeSub) : '';
}

// ───────────────────────── undo / redo (editing history) ─────────────────────
// Snapshots the crop keyframes (incl. cuts) + SFX placements; restores on Ctrl+Z /
// Ctrl+Y (+ ↶ ↷ buttons). Debounced off mouseup/keyup + a backstop. Text fields
// keep native undo (we never intercept Ctrl+Z while a field is focused).
const hist = { undo: [], redo: [], baseline: null, restoring: false };
const HIST_MAX = 80;
function histLoaded() { return !!state.jobId; }
// Capture the WHOLE editor buffer (crop · illustrations · trim · caption-pos ·
// text · stickers · sfx · split) + the sub-clips list + which one is active, so
// undo/redo works on every page, not just the crop keyframes.
function histSig() {
  return JSON.stringify({ buf: subSnapshot(), subclips: state.subclips, activeSub: state.activeSub });
}
function histReset() {
  hist.undo = []; hist.redo = [];
  hist.baseline = histLoaded() ? histSig() : null;
  updateUndoButtons();
}
function histCapture() {
  if (hist.restoring || !histLoaded()) return;
  syncActiveSub();                 // fold the live buffer into the active sub-clip first
  const s = histSig();
  if (s === hist.baseline) return;
  if (hist.baseline !== null) {
    hist.undo.push(hist.baseline);
    if (hist.undo.length > HIST_MAX) hist.undo.shift();
    hist.redo = [];
  }
  hist.baseline = s;
  updateUndoButtons();
}
function histApply(s) {
  const o = JSON.parse(s);
  hist.restoring = true;
  state.subclips = JSON.parse(JSON.stringify(o.subclips || []));
  state.activeSub = (o.activeSub != null && state.subclips[o.activeSub]) ? o.activeSub : null;
  subApply(o.buf || {});           // restores box/segments/trim/capPos/overlays/stickers/sfx/caption
  try { renderSfxList(); } catch (e) { /* sound UI not ready */ }
  try { renderSubclips(); updateSubRangeLbl(); } catch (e) { /* sub-clip UI not ready */ }
  hist.restoring = false;
}
function histUndo() {
  if (!hist.undo.length) return;
  hist.redo.push(hist.baseline);
  hist.baseline = hist.undo.pop();
  histApply(hist.baseline);
  updateUndoButtons();
}
function histRedo() {
  if (!hist.redo.length) return;
  hist.undo.push(hist.baseline);
  hist.baseline = hist.redo.pop();
  histApply(hist.baseline);
  updateUndoButtons();
}
function updateUndoButtons() {
  const u = document.getElementById('btn-undo'), r = document.getElementById('btn-redo');
  if (u) u.disabled = !hist.undo.length;
  if (r) r.disabled = !hist.redo.length;
}
function renderEverything() {
  try { refreshKfUI(); } catch (e) { /* crop UI not ready */ }
  try { renderSfxList(); } catch (e) { /* sound UI not ready */ }
  try { renderTrimTrack(); } catch (e) { /* trim UI not ready */ }
  try { renderCapPosTrack(); } catch (e) { /* cap-pos UI not ready */ }
  try { renderOverlayTrack(); renderOverlayList(); } catch (e) { /* overlay UI not ready */ }
  try { renderStickerTrack(); renderStickerList(); } catch (e) { /* sticker UI not ready */ }
  try { renderSubclips(); updateSubRangeLbl(); } catch (e) { /* sub-clip UI not ready */ }
}
const _undoBtn = document.getElementById('btn-undo');
const _redoBtn = document.getElementById('btn-redo');
if (_undoBtn) _undoBtn.addEventListener('click', histUndo);
if (_redoBtn) _redoBtn.addEventListener('click', histRedo);
window.addEventListener('mouseup', () => setTimeout(histCapture, 0));
window.addEventListener('keyup', () => setTimeout(histCapture, 0));
setInterval(histCapture, 1200);
window.addEventListener('keydown', (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) return;
  const k = e.key.toLowerCase();
  if (k === 'z' && !e.shiftKey) { e.preventDefault(); histUndo(); }
  else if ((k === 'z' && e.shiftKey) || k === 'y') { e.preventDefault(); histRedo(); }
});

// ───────────────────────── arm / draw ─────────────────────────
$('#pill-1').addEventListener('click', (e) => {
  if (e.target.dataset.clear) return;
  toggleArm(1);
});
function toggleArm(n) { state.activeBox = state.activeBox === n ? null : n; refreshArmUI(); }
function refreshArmUI() {
  const armed = state.activeBox === 1;
  $('#pill-1').classList.toggle('armed', armed);
  $('#source-stage').classList.toggle('armed', armed);
  overlay.style.pointerEvents = armed ? 'auto' : 'none';
}

const HANDLE = 12;
let drag = null;

function rectOverlay(t) {
  const b = boxAt(t);
  if (!b) return null;
  const tl = sourceToOverlay(b.x, b.y);
  const br = sourceToOverlay(b.x + b.w, b.y + b.h);
  return { x: tl.x, y: tl.y, w: br.x - tl.x, h: br.y - tl.y };
}

function hitCorner(r, mx, my) {
  const corners = [['nw', r.x, r.y], ['ne', r.x + r.w, r.y], ['sw', r.x, r.y + r.h], ['se', r.x + r.w, r.y + r.h]];
  for (const [name, cx, cy] of corners) {
    if (Math.hypot(mx - cx, my - cy) <= HANDLE) return name;
  }
  return null;
}

overlay.addEventListener('mousedown', (e) => {
  if (state.activeBox !== 1) return;
  const mx = e.offsetX, my = e.offsetY;
  const r = rectOverlay(state.currentTime);
  if (r) {
    const corner = hitCorner(r, mx, my);
    if (corner) { drag = { mode: 'resize', corner, start: { mx, my }, orig: r }; return; }
    if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) {
      drag = { mode: 'move', start: { mx, my }, orig: r }; return;
    }
  }
  drag = { mode: 'draw', start: { mx, my }, cur: { mx, my } };
});
overlay.addEventListener('mousemove', (e) => {
  const mx = e.offsetX, my = e.offsetY;
  // cursor feedback
  if (!drag && state.activeBox === 1) {
    const r = rectOverlay(state.currentTime);
    let c = 'crosshair';
    if (r) {
      const corner = hitCorner(r, mx, my);
      if (corner) c = (corner === 'nw' || corner === 'se') ? 'nwse-resize' : 'nesw-resize';
      else if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) c = 'move';
    }
    overlay.style.cursor = c;
  }
  if (!drag) return;
  drag.cur = { mx, my };
  drawOverlay(dragRect());
});
window.addEventListener('mouseup', () => {
  if (!drag) return;
  const r = dragRect();
  drag = null;
  if (!r || r.w < 4 || r.h < 4) { drawOverlay(); return; }
  const a = overlayToSource(r.x, r.y);
  const b = overlayToSource(r.x + r.w, r.y + r.h);
  // Free-form box, just clamped into the frame (round-then-cap so x+w<=srcW)
  // so an edge-drawn box never makes the backend crop crash.
  const c = clampToSource({ x: a.x, y: a.y, w: b.x - a.x, h: b.y - a.y }, state.srcW, state.srcH);
  const ix = Math.round(c.x), iy = Math.round(c.y);
  upsertKf({
    x: ix, y: iy,
    w: Math.min(Math.round(c.w), state.srcW - ix),
    h: Math.min(Math.round(c.h), state.srcH - iy),
  });
});

function dragRect() {
  if (!drag) return null;
  if (drag.mode === 'draw') {
    const x = Math.min(drag.start.mx, drag.cur.mx), y = Math.min(drag.start.my, drag.cur.my);
    return { x, y, w: Math.abs(drag.cur.mx - drag.start.mx), h: Math.abs(drag.cur.my - drag.start.my) };
  }
  if (drag.mode === 'move') {
    const dx = drag.cur.mx - drag.start.mx, dy = drag.cur.my - drag.start.my;
    return { x: drag.orig.x + dx, y: drag.orig.y + dy, w: drag.orig.w, h: drag.orig.h };
  }
  if (drag.mode === 'resize') {
    let { x, y, w, h } = drag.orig;
    const dx = drag.cur.mx - drag.start.mx, dy = drag.cur.my - drag.start.my;
    if (drag.corner.includes('e')) w += dx;
    if (drag.corner.includes('s')) h += dy;
    if (drag.corner.includes('w')) { x += dx; w -= dx; }
    if (drag.corner.includes('n')) { y += dy; h -= dy; }
    if (w < 0) { x += w; w = -w; }
    if (h < 0) { y += h; h = -h; }
    return { x, y, w, h };
  }
  return null;
}

// ───────────────────────── overlay draw ─────────────────────────
function drawOverlay(tempRect) {
  octx.clearRect(0, 0, overlay.width, overlay.height);
  const r = tempRect || rectOverlay(state.currentTime);
  if (!r) return;
  octx.strokeStyle = 'var(--box-1)';
  octx.strokeStyle = '#5ee0ff';
  octx.lineWidth = 2;
  octx.strokeRect(r.x, r.y, r.w, r.h);
  octx.fillStyle = 'rgba(94,224,255,0.12)';
  octx.fillRect(r.x, r.y, r.w, r.h);

  // Render-area guide: in COVER, dim the cropped margins + outline the centered
  // 3:2 sub-rect that actually renders. BLUR_PAD shows the whole box (no crop).
  const fit = tempRect ? 'cover' : ((boxAt(state.currentTime) || {}).fit || 'cover');
  if (fit !== 'blur_pad' && r.w > 1 && r.h > 1) {
    const sa = overlayToSource(r.x, r.y), sb = overlayToSource(r.x + r.w, r.y + r.h);
    const keep = coverKeepRect({ x: sa.x, y: sa.y, w: sb.x - sa.x, h: sb.y - sa.y }, boxAR());
    const ka = sourceToOverlay(keep.x, keep.y), kb = sourceToOverlay(keep.x + keep.w, keep.y + keep.h);
    octx.fillStyle = 'rgba(0,0,0,0.5)';
    if (ka.x > r.x) octx.fillRect(r.x, r.y, ka.x - r.x, r.h);
    if (kb.x < r.x + r.w) octx.fillRect(kb.x, r.y, r.x + r.w - kb.x, r.h);
    if (ka.y > r.y) octx.fillRect(r.x, r.y, r.w, ka.y - r.y);
    if (kb.y < r.y + r.h) octx.fillRect(r.x, kb.y, r.w, r.y + r.h - kb.y);
    octx.strokeStyle = '#fff';
    octx.lineWidth = 1.5;
    octx.setLineDash([5, 3]);
    octx.strokeRect(ka.x, ka.y, kb.x - ka.x, kb.y - ka.y);
    octx.setLineDash([]);
  }

  // corner handles
  octx.fillStyle = '#fff';
  for (const [cx, cy] of [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]]) {
    octx.fillRect(cx - 5, cy - 5, 10, 10);
  }
}

// ───────────────────────── timeline ─────────────────────────
function renderTimelineDots() {
  const host = $('#dots-1');
  host.innerHTML = '';
  const dur = video.duration || state.duration || 1;
  for (const k of sortedKfs()) {
    const d = document.createElement('span');
    d.className = 'timeline-dot' + (k.gap ? ' gap' : '') + (k.dynamic ? ' dynamic' : '');
    d.style.left = (k.t / dur * 100) + '%';
    d.title = k.dynamic ? `kf @ ${k.t.toFixed(2)}s · DYNAMIC (draw manually)` : `kf @ ${k.t.toFixed(2)}s`;
    d.addEventListener('click', () => { video.currentTime = k.t; });
    host.appendChild(d);
  }
  updateTimelineCursor();
}
function updateTimelineCursor() {
  const cur = $('#timeline-cursor'), track = $('#dots-1'), tl = $('#timeline');
  if (!cur || !track || !tl) return;
  const dur = video.duration || state.duration || 1;
  // Position the playhead in PX relative to the dots track (not as a % of the
  // padded .timeline) so it lines up EXACTLY with the dots — and, since the cut
  // lanes share the same track geometry (see .cut-lanes padding in style.css),
  // with the bars too. Mirrors clipper's updateTimelineCursor.
  const tlR = tl.getBoundingClientRect(), tr = track.getBoundingClientRect();
  if (!tr.width) return;   // panel hidden / not laid out yet
  const x = (tr.left - tlR.left - tl.clientLeft) + (state.currentTime / dur) * tr.width;
  cur.style.left = x + 'px';
}

// ───────────────────────── keyframe list ─────────────────────────
function renderKfList() {
  const ol = $('#kf-items-1');
  ol.innerHTML = '';
  const kfs = sortedKfs();
  if (!kfs.length) { ol.innerHTML = '<li class="muted">No keyframes yet.</li>'; return; }
  kfs.forEach((k, i) => {
    const next = kfs[i + 1];
    const cur = state.currentTime >= k.t && (!next || state.currentTime < next.t);
    const li = document.createElement('li');
    const isDyn = !!k.dynamic;
    const isMov = !!k.moving;   // panning track (director / dynamic) → keep linear
    li.className = 'kf-row' + (cur ? ' current' : '') + (isDyn ? ' dynamic' : '') + (isMov ? ' moving' : '');
    // Editable start/end — click a time to lengthen/shorten the segment
    // (start = this kf's t, end = the NEXT kf's t; clip end isn't editable).
    const timeHtml = `
      <span class="kf-seg">[<span class="kf-tedit" data-i="${i}" data-which="start"
        title="Click to edit when this segment STARTS (lengthen/shorten)">${k.t.toFixed(2)}s</span> → ${
        next ? `<span class="kf-tedit" data-i="${i}" data-which="end"
        title="Click to edit when this segment ENDS (moves the next keyframe)">${next.t.toFixed(2)}s</span>` : 'end'}]</span>
      <span class="kf-onscreen" title="this segment is what's on screen at the playhead right now">▶ ON SCREEN</span>`;
    if (isDyn) {
      // auto-box gave up here (too unstable) → user draws this one by hand.
      li.innerHTML = `${timeHtml}
        <span class="kf-dim kf-seg-dyn"><span class="dyn-tag">DYNAMIC</span> draw this manually</span>
        <button class="kf-tag" data-i="${i}" data-act="seek">seek</button>
        <button class="kf-tag danger" data-i="${i}" data-act="del">×</button>`;
    } else {
      li.innerHTML = `${timeHtml}
        <span class="kf-dim">${Math.round(k.w)}×${Math.round(k.h)} @(${Math.round(k.x)},${Math.round(k.y)})</span>${isMov ? '<span class="mov-tag" title="tracked: size locked, center pans the moving subject">TRACKED</span>' : ''}
        <button class="kf-tag" data-i="${i}" data-act="interp">${(k.interp || 'hold') === 'linear' ? 'PAN→' : 'HOLD'}</button>
        <button class="kf-tag" data-i="${i}" data-act="fit">${k.fit === 'blur_pad' ? 'BLUR' : 'COVER'}</button>
        <button class="kf-tag" data-i="${i}" data-act="seek">seek</button>
        <button class="kf-tag danger" data-i="${i}" data-act="del">×</button>`;
    }
    ol.appendChild(li);
  });
  ol.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    const k = sortedKfs()[Number(b.dataset.i)];
    const act = b.dataset.act;
    if (act === 'interp') k.interp = (k.interp || 'hold') === 'linear' ? 'hold' : 'linear';
    else if (act === 'fit') k.fit = k.fit === 'blur_pad' ? 'cover' : 'blur_pad';
    else if (act === 'seek') { video.currentTime = k.t; return; }
    else if (act === 'del') { state.box = state.box.filter((x) => x !== k); }
    refreshKfUI();
  }));
  ol.querySelectorAll('.kf-tedit').forEach((sp) => sp.addEventListener('click', () => startKfTimeEdit(sp)));
}

// Live "which segment is on screen" highlight while the video plays — class
// toggles only (no HTML rebuild, so an in-progress time edit is never destroyed).
function updateKfListHighlight() {
  const ol = $('#kf-items-1');
  if (!ol) return;
  const kfs = sortedKfs();
  ol.querySelectorAll('li.kf-row').forEach((li, i) => {
    const k = kfs[i];
    if (!k) return;
    const next = kfs[i + 1];
    li.classList.toggle('current', state.currentTime >= k.t && (!next || state.currentTime < next.t));
  });
}

// A segment runs [kf[i].t, kf[i+1].t). Editing its START moves this kf's t;
// editing its END moves the NEXT kf's t — clamped between neighbors so order
// (and every other segment) stays intact. This is how a segment is
// lengthened/shortened from the list.
function startKfTimeEdit(span) {
  const i = +span.dataset.i, which = span.dataset.which;
  const kfs = sortedKfs();
  const target = which === 'start' ? kfs[i] : kfs[i + 1];
  if (!target) return;
  const dur = video.duration || state.duration || Infinity;
  const EPS = 0.05;
  let lo, hi;
  if (which === 'start') {
    lo = i > 0 ? kfs[i - 1].t + EPS : 0;
    hi = (i + 1 < kfs.length ? kfs[i + 1].t : dur) - EPS;
  } else {
    lo = kfs[i].t + EPS;
    hi = (i + 2 < kfs.length ? kfs[i + 2].t : dur) - EPS;
  }
  hi = Math.max(lo, hi);
  const input = document.createElement('input');
  input.type = 'number';
  input.step = '0.1';
  input.min = lo.toFixed(2);
  input.max = hi.toFixed(2);
  input.value = target.t.toFixed(2);
  input.className = 'kf-tedit-input';
  span.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = () => {
    if (done) return;
    done = true;
    const v = parseFloat(input.value);
    if (!isNaN(v)) target.t = Math.min(hi, Math.max(lo, v));
    refreshKfUI();
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') commit();
    else if (ev.key === 'Escape') { done = true; refreshKfUI(); }
  });
  input.addEventListener('blur', commit);
}

// ───────────────────────── preview canvas ─────────────────────────
function drawPreview(canvas) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const tph = topPH();   // slot boundary — moves with the chosen 3/8 · 3.5/8 · 4/8 split
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, PREVIEW_W, PREVIEW_H);

  // Full-screen window → video fills the WHOLE 9:16 frame (no split). Either the
  // user toggled it OFF (🎬) or the segment simply has no picked image → we never
  // leave the bottom black, the video just fills the frame.
  const offSeg = state.segments.find((s) => (s.off || !s.picked) && state.currentTime >= s.t_start && state.currentTime < s.t_end);
  if (offSeg) {
    const bf = boxAt(state.currentTime);
    if (bf && !bf.gap && video.videoWidth) {
      drawCover(ctx, video, bf.x, bf.y, bf.w, bf.h, 0, 0, PREVIEW_W, PREVIEW_H, bf.fit === 'blur_pad');
    }
    return;
  }

  // top slot: cropped video (a gap kf → leave the slot black: box is "off" here)
  const b = boxAt(state.currentTime);
  if (b && !b.gap && video.videoWidth) {
    drawCover(ctx, video, b.x, b.y, b.w, b.h, 0, 0, PREVIEW_W, tph, b.fit === 'blur_pad');
  }
  // bottom slot: picked illustration for current time
  const seg = state.segments.find((s) => state.currentTime >= s.t_start && state.currentTime < s.t_end && s.picked);
  if (seg && seg._img && seg._img.complete) {
    // Animate the Ken Burns motion (zoom/pan) at the current time, matching the
    // renderer, so the preview shows the actual effect — not a static frame.
    const k = Math.max(0, Math.min(1, (state.currentTime - seg.t_start) / Math.max(0.001, seg.t_end - seg.t_start)));
    drawIllMotion(ctx, seg._img, 0, tph, PREVIEW_W, PREVIEW_H - tph, illEffectiveMotion(seg), k);
  } else {
    ctx.fillStyle = '#1d1d24';
    ctx.fillRect(0, tph, PREVIEW_W, PREVIEW_H - tph);
    ctx.fillStyle = '#5a5a68';
    ctx.font = '10px JetBrains Mono';
    ctx.textAlign = 'center';
    ctx.fillText('illustration', PREVIEW_W / 2, tph + (PREVIEW_H - tph) / 2);
    ctx.textAlign = 'left';
  }
  // slot divider line
  ctx.strokeStyle = 'rgba(232,255,58,0.4)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, tph); ctx.lineTo(PREVIEW_W, tph); ctx.stroke();

  // caption position marker (top / middle=boundary / bottom)
  const cp = ($('#cap-pos') && $('#cap-pos').value) || 'middle';
  const capY = cp === 'top' ? PREVIEW_H * 0.13 : cp === 'bottom' ? PREVIEW_H * 0.87 : tph;
  ctx.fillStyle = 'rgba(232,255,58,0.9)';
  ctx.font = 'bold 11px Anton, sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.strokeStyle = 'rgba(0,0,0,0.7)'; ctx.lineWidth = 3;
  ctx.strokeText('CAPTION', PREVIEW_W / 2, capY);
  ctx.fillText('CAPTION', PREVIEW_W / 2, capY);
  ctx.textAlign = 'left';
}

// scale-cover (or contain+blur) src region into dest rect
function drawCover(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh, blur) {
  if (sw <= 0 || sh <= 0) return;
  ctx.save();
  ctx.beginPath(); ctx.rect(dx, dy, dw, dh); ctx.clip();
  const srcAR = sw / sh, dstAR = dw / dh;
  if (blur) {
    // blurred cover background + contained foreground
    ctx.filter = 'blur(6px) brightness(0.85)';
    coverDraw(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh);
    ctx.filter = 'none';
    let fw = dw, fh = dw / srcAR;
    if (fh > dh) { fh = dh; fw = dh * srcAR; }
    try { ctx.drawImage(src, sx, sy, sw, sh, dx + (dw - fw) / 2, dy + (dh - fh) / 2, fw, fh); } catch (e) {}
  } else {
    coverDraw(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh);
  }
  ctx.restore();
}
function coverDraw(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh) {
  const srcAR = sw / sh, dstAR = dw / dh;
  let cw = sw, ch = sh, cx = sx, cy = sy;
  if (srcAR > dstAR) { cw = sh * dstAR; cx = sx + (sw - cw) / 2; }
  else { ch = sw / dstAR; cy = sy + (sh - ch) / 2; }
  try { ctx.drawImage(src, cx, cy, cw, ch, dx, dy, dw, dh); } catch (e) {}
}

// ── Ken Burns motion preview (mirrors renderer._ill_motion_chain, 12%) ──────
const ILL_MOTION_AMT = 0.12;
// The cover-crop region of an image for a destination aspect ratio.
function _coverCrop(iw, ih, dstAR) {
  if (iw / ih > dstAR) { const cw = ih * dstAR; return { cx: (iw - cw) / 2, cy: 0, cw, ch: ih }; }
  const ch = iw / dstAR; return { cx: 0, cy: (ih - ch) / 2, cw: iw, ch };
}
// Draw `img` into the slot with the given motion at progress k (0→1), staying
// strictly inside the frame — zoom = centre-crop of a cover fit; pan = a slot-AR
// window sliding across a slightly-wider cover fit.
function drawIllMotion(ctx, img, dx, dy, dw, dh, motion, k) {
  const iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height;
  if (!iw || !ih || dw <= 0 || dh <= 0) return;
  const slotAR = dw / dh, A = ILL_MOTION_AMT;
  let sx, sy, sw, sh;
  if (motion === 'zoom_in' || motion === 'zoom_out') {
    const c = _coverCrop(iw, ih, slotAR);
    const z = motion === 'zoom_in' ? (1 + A * k) : (1 + A * (1 - k));
    sw = c.cw / z; sh = c.ch / z;
    sx = c.cx + (c.cw - sw) / 2; sy = c.cy + (c.ch - sh) / 2;
  } else if (motion === 'pan_left' || motion === 'pan_right') {
    const c = _coverCrop(iw, ih, slotAR * (1 + A));
    sw = c.cw / (1 + A); sh = c.ch;
    const travel = c.cw - sw;
    sx = c.cx + (motion === 'pan_right' ? travel * k : travel * (1 - k));
    sy = c.cy;
  } else {  // none / static
    const c = _coverCrop(iw, ih, slotAR);
    sx = c.cx; sy = c.cy; sw = c.cw; sh = c.ch;
  }
  ctx.save();
  ctx.beginPath(); ctx.rect(dx, dy, dw, dh); ctx.clip();
  try { ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh); } catch (_) {}
  ctx.restore();
}
// The motion that actually applies to a segment — explicit per-illustration wins,
// else the global default; 'auto' cycles over the picked segments (matches render).
function illEffectiveMotion(seg) {
  if (seg.motion) return seg.motion;
  const mode = ($('#ill-motion') && $('#ill-motion').value) || 'auto';
  if (mode !== 'auto') return mode;
  const picks = state.segments.filter((s) => s.picked && !s.off && !segTrimInfo(s).fullyCut);
  const idx = picks.indexOf(seg);
  return idx >= 0 ? MOTION_CYCLE[idx % MOTION_CYCLE.length] : 'none';
}

// ───────────────────────── step 2 → transcribe ─────────────────────────
$('#btn-continue').addEventListener('click', async () => {
  if (!state.box.length) { setStatus($('#tr-status'), 'Draw at least 1 crop box first', 'err'); return; }
  setStatus($('#tr-status'), 'Transcribing (Whisper)… first run is slow (loading model)');
  $('#btn-continue').disabled = true;
  try {
    const r = await api('transcribe', { job_id: state.jobId });
    state.words = r.words || [];
    setStatus($('#tr-status'), `OK · ${state.words.length} words`, 'ok');
    showStep(3);
  } catch (e) {
    setStatus($('#tr-status'), e.message, 'err');
  } finally {
    $('#btn-continue').disabled = false;
  }
});

// ───────────────────────── step 3: illustration plan ─────────────────────────
$('#btn-plan').addEventListener('click', async () => {
  state.segSeconds = Number($('#seg-seconds').value) || 5;
  // Scope planning to the ACTIVE sub-clip's range (so a long source isn't
  // segmented end-to-end). No active sub-clip → whole clip.
  const asc = state.subclips[state.activeSub];
  const scoped = asc ? ` (sub-clip "${asc.name}" ${asc.start.toFixed(0)}–${asc.end.toFixed(0)}s)` : '';
  $('#btn-plan').disabled = true;
  try {
    // No transcript yet → transcribe first, else every query falls back to the title.
    if (!state.words.length) {
      setStatus($('#plan-status'), 'Transcribing first (so queries match what is said)…');
      try { const tr = await api('transcribe', { job_id: state.jobId }); state.words = tr.words || []; } catch (e) { /* proceed; queries fall back to title */ }
    }
    setStatus($('#plan-status'), `AI is finding an image for each segment${scoped}…`);
    const r = await api('plan', {
      job_id: state.jobId,
      words: state.words,
      segment_seconds: state.segSeconds,
      duration: video.duration || state.duration,
      // overall video topic → LLM anchors every query to it (not literal words)
      title: $('#f-title').value.trim() || 'clip',
      description: $('#f-desc').value.trim(),
      t_start: asc ? asc.start : 0,
      t_end: asc ? asc.end : null,
      source: ($('#ill-source') && $('#ill-source').value) || 'all',
    });
    state.segments = (r.segments || []).map((s) => ({ ...s, picked: null, _img: null, off: false }));
    // auto-pick first candidate per segment (so you can render right away)
    for (const s of state.segments) {
      if (s.candidates && s.candidates.length) pickCandidate(s, s.candidates[0], false);
    }
    renderSegments();
    setStatus($('#plan-status'), `OK · ${state.segments.length} segments`, 'ok');
  } catch (e) {
    setStatus($('#plan-status'), e.message, 'err');
  } finally {
    $('#btn-plan').disabled = false;
  }
});

function pickCandidate(seg, cand, rerender) {
  seg.picked = cand.full;
  const img = new Image();
  img.src = cand.thumb; // preview uses the lightweight thumb (no crossOrigin: display only)
  img.onload = () => { try { drawPreview($('#preview')); const ip = $('#preview-ill'); if (ip) drawPreview(ip); } catch (e) {} };
  seg._img = img;
  if (rerender) renderSegments();
  const ip = $('#preview-ill'); if (ip) drawPreview(ip);
}
// Short-preview on the Illustration step: scope the scrubber to the active
// sub-clip's window, seek the (shared) video into it, draw the 9:16 composite.
function initIllPreview() {
  const sc = $('#ill-scrub'); if (!sc) return;
  const asc = state.subclips[state.activeSub];
  const lo = asc ? asc.start : 0;
  const hi = asc ? asc.end : (video.duration || state.duration || 1);
  sc.min = lo; sc.max = hi;
  if (video.duration && (video.currentTime < lo - 0.05 || video.currentTime > hi + 0.05)) video.currentTime = lo;
  sc.value = video.currentTime || lo;
  renderIllCuts();
  drawPreview($('#preview-ill'));
}

// Hatched bands over the preview scrubber marking the TRIMMED-OUT parts (the gaps
// between the keep windows) so it's visible what play + render will skip.
function renderIllCuts() {
  const host = $('#ill-cuts'); if (!host) return;
  const { lo, hi } = _illRange();
  const span = Math.max(0.01, hi - lo);
  const ks = _illKeeps();   // clamped keep windows; whole range when no trim set
  if (ks.length === 1 && ks[0].a <= lo + 0.02 && ks[0].b >= hi - 0.02) { host.innerHTML = ''; return; }
  const cuts = [];
  let cursor = lo;
  for (const w of ks) {
    if (w.a > cursor + 0.02) cuts.push([cursor, w.a]);
    cursor = Math.max(cursor, w.b);
  }
  if (cursor < hi - 0.02) cuts.push([cursor, hi]);
  host.innerHTML = cuts.map(([a, b]) =>
    `<span class="ill-cut-band" style="left:${((a - lo) / span) * 100}%;width:${((b - a) / span) * 100}%"></span>`
  ).join('');
}

// ── illustration timing bars ───────────────────────────────────────────
// Each illustration owns a [t_start,t_end] window; together they tile [0,dur].
// The lane shows one draggable bar per image — drag an EDGE to move the shared
// boundary (one image gets more time, its neighbour less), or drag the BODY to
// slide the whole window (eats the next, feeds the previous). Stays contiguous:
// no black gaps, no overlaps. First start is locked to 0, last end to the clip.
function renderIllBars() {
  const host = $('#ill-bars');
  if (!host) return;
  const dur = cutDur();
  if (!state.segments.length) {
    host.innerHTML = '<span class="cut-lane-empty">— generate illustrations first —</span>';
    return;
  }
  host.innerHTML = state.segments.map((s, i) => {
    const left = (s.t_start / dur) * 100;
    const width = Math.max(0.8, ((s.t_end - s.t_start) / dur) * 100);
    const secs = (s.t_end - s.t_start).toFixed(1);
    // Full-screen (🎬) = video fills the frame: either toggled OFF, or no image
    // picked (auto-fallback). Only a picked image (🖼) gets the split layout.
    const isFull = s.off || !s.picked;
    const cls = 'cut-bar ' + (isFull ? 'vfull' : 'on');
    const lbl = isFull ? `🎬 #${i + 1} · ${secs}s` : `🖼 #${i + 1} · ${secs}s`;
    const tip = isFull
      ? `Window #${i + 1}: ${s.t_start.toFixed(1)}–${s.t_end.toFixed(1)}s — FULL-SCREEN video${s.off ? ' (toggled off)' : ' (no image → full)'}`
      : `Image #${i + 1}: ${s.t_start.toFixed(1)}–${s.t_end.toFixed(1)}s`;
    return `<div class="${cls}" data-i="${i}" style="left:${left}%;width:${width}%" `
      + `title="${tip} — drag to move, edges to resize">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${lbl}</span>`
      + `<span class="cut-bar-h r"></span></div>`;
  }).join('');
}

function onIllBarDown(e) {
  const bar = e.target.closest('.cut-bar');
  if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i;
  const segs = state.segments;
  const s = segs[i];
  if (!s) return;
  const dur = cutDur();
  const rect = $('#ill-bars').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const last = segs.length - 1;
  let mode = isH && e.target.classList.contains('l') ? 'resize-l'
           : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  // the clip's outer edges are fixed: can't pull the first image's start off 0
  // or the last image's end off the clip end → fall back to moving the body.
  if (mode === 'resize-l' && i === 0) mode = 'move';
  if (mode === 'resize-r' && i === last) mode = 'move';
  state.illDrag = {
    i, mode, startX: e.clientX, trackW: rect.width, dur,
    a0: s.t_start, b0: s.t_end,
    prevStart: i > 0 ? segs[i - 1].t_start : 0,      // outer clamp for the left neighbour
    nextEnd: i < last ? segs[i + 1].t_end : dur,     // outer clamp for the right neighbour
  };
}

function onIllDragMove(e) {
  const d = state.illDrag;
  if (!d) return;
  const segs = state.segments;
  const s = segs[d.i];
  if (!s) { state.illDrag = null; return; }
  const last = segs.length - 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * d.dur;
  if (d.mode === 'resize-l') {
    const ns = Math.max(d.prevStart + CUT_MIN, Math.min(d.a0 + dt, s.t_end - CUT_MIN));
    s.t_start = +ns.toFixed(2);
    if (d.i > 0) segs[d.i - 1].t_end = s.t_start;         // share the boundary
  } else if (d.mode === 'resize-r') {
    const ne = Math.max(s.t_start + CUT_MIN, Math.min(d.b0 + dt, d.nextEnd - CUT_MIN));
    s.t_end = +ne.toFixed(2);
    if (d.i < last) segs[d.i + 1].t_start = s.t_end;      // share the boundary
  } else { // move — slide the whole window, neighbours follow to stay contiguous
    const len = d.b0 - d.a0;
    const lo = (d.i > 0 ? d.prevStart + CUT_MIN : 0);
    const hi = (d.i < last ? d.nextEnd - CUT_MIN : d.dur) - len;
    const ns = Math.max(lo, Math.min(d.a0 + dt, hi));
    s.t_start = +ns.toFixed(2);
    s.t_end = +(ns + len).toFixed(2);
    if (d.i > 0) segs[d.i - 1].t_end = s.t_start;
    if (d.i < last) segs[d.i + 1].t_start = s.t_end;
  }
  renderIllBars();
}

// ── Trim (keep-windows) — mark windows to KEEP; the rest is cut at render ──
const TRIM_MIN = 0.2;
// Dedicated Trim page (step 9): own video + scrubber; trims the editor buffer's
// keep-windows (state.keep) = the SELECTED sub-clip's trim (auto-saved). Pick a
// sub-clip via #trim-subclip → that one is loaded + trimmed here.
function trimDur() { return (trimVideo && trimVideo.duration) || cutDur() || 1; }
function trimPlayhead() { return trimVideo ? (trimVideo.currentTime || 0) : (state.currentTime || 0); }
// The trim timeline spans ONLY the active sub-clip's [start,end] (the whole clip
// if none active), so the playhead + Keep bars cover just that window.
function trimWindow() {
  const sc = state.subclips[state.activeSub];
  if (sc) return { lo: sc.start, hi: sc.end };
  return { lo: 0, hi: (trimVideo && trimVideo.duration) || cutDur() || 1 };
}
// Clamp the scrubber + playback to the active window.
function applyTrimWindow() {
  const { lo, hi } = trimWindow();
  const scr = $('#trim-scrubber'); if (scr) { scr.min = lo; scr.max = hi; }
  const d = $('#trim-dur'); if (d) d.textContent = fmtTime(hi);
  if (trimVideo && trimVideo.duration && (trimVideo.currentTime < lo - 0.05 || trimVideo.currentTime > hi + 0.05)) {
    trimVideo.currentTime = lo;
  }
}
function renderTrimTrack() {
  const host = $('#trim-track'); if (!host) { renderTrimList(); return; }
  const { lo, hi } = trimWindow(); const span = Math.max(0.01, hi - lo);
  if (!state.keep.length) { host.innerHTML = '<span class="cut-lane-empty">— whole sub-clip kept (Set A → Set B to cut the rest) —</span>'; }
  else host.innerHTML = state.keep.map((w, i) => {
    const left = ((w.start - lo) / span) * 100;
    const width = Math.max(0.8, ((w.end - w.start) / span) * 100);
    return `<div class="cut-bar keep" data-i="${i}" style="left:${left}%;width:${width}%;background:rgba(70,211,105,.20);border-color:#46d369" title="Keep ${w.start.toFixed(1)}–${w.end.toFixed(1)}s — drag to move, edges to resize, double-click to remove">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${(w.end - w.start).toFixed(1)}s</span><span class="cut-bar-h r"></span></div>`;
  }).join('');
  renderTrimList(); updateTrimCursor();
}
function renderTrimList() {
  const ol = $('#trim-list'); if (!ol) return;
  if (!state.keep.length) { ol.innerHTML = '<li class="empty">No windows — whole clip kept. Set A→B to cut dead air.</li>'; return; }
  ol.innerHTML = state.keep.map((w, i) => `
    <li><span class="o-name">Keep ${i + 1}</span>
      <span class="o-when">${w.start.toFixed(1)} → ${w.end.toFixed(1)}s · ${(w.end - w.start).toFixed(1)}s</span>
      <button data-seek="${i}" title="seek">▶</button>
      <button class="danger" data-del="${i}" title="remove">×</button></li>`).join('');
  ol.querySelectorAll('[data-seek]').forEach((b) => b.addEventListener('click', () => { const w = state.keep[+b.dataset.seek]; if (w && trimVideo) trimVideo.currentTime = w.start; }));
  ol.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => { state.keep.splice(+b.dataset.del, 1); syncActiveSub(); renderTrimTrack(); }));
}
function updateTrimCursor() {
  const cur = $('#trim-cursor'), track = $('#trim-track'), wrap = $('#trim-tl-wrap');
  if (!cur || !track || !wrap) return;
  const { lo, hi } = trimWindow(); const span = Math.max(0.01, hi - lo);
  const wR = wrap.getBoundingClientRect(), tR = track.getBoundingClientRect();
  if (!tR.width) return;
  const frac = Math.max(0, Math.min(1, (trimPlayhead() - lo) / span));
  cur.style.left = ((tR.left - wR.left) + frac * tR.width) + 'px';
}
function addKeep(a, b) {
  const { lo, hi } = trimWindow();
  a = Math.max(lo, Math.min(a, b)); b = Math.min(hi, Math.max(a, b));
  if (b - a < TRIM_MIN) { setStatus($('#trim-status'), 'Window too short.', 'err'); return false; }
  state.keep.push({ start: +a.toFixed(2), end: +b.toFixed(2) });
  state.keep.sort((x, y) => x.start - y.start);
  syncActiveSub(); renderTrimTrack();
  setStatus($('#trim-status'), `Keep ${a.toFixed(1)}–${b.toFixed(1)}s added (${state.keep.length} window${state.keep.length > 1 ? 's' : ''}).`, 'ok');
  return true;
}
function setKeepA() { state.keepA = trimPlayhead(); setStatus($('#trim-status'), `A @ ${state.keepA.toFixed(2)}s — scrub to the end, then Set B.`, 'ok'); }
function setKeepB() {
  if (state.keepA == null) { setStatus($('#trim-status'), 'Set A first.', 'err'); return; }
  if (addKeep(state.keepA, trimPlayhead())) state.keepA = null;
}
function addKeepHere() { const { lo, hi } = trimWindow(); const t = Math.max(lo, Math.min(trimPlayhead(), hi)); addKeep(t, Math.min(t + 3, hi)); }
function clearKeep() { state.keep = []; state.keepA = null; syncActiveSub(); renderTrimTrack(); setStatus($('#trim-status'), 'Cleared — whole clip kept.', 'ok'); }
function onTrimTrackDown(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i; const w = state.keep[i]; if (!w) return;
  const rect = $('#trim-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.keepDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: w.start, b: w.end };
}
function onTrimDragMove(e) {
  const d = state.keepDrag; if (!d) return;
  const { lo, hi } = trimWindow(); const span = Math.max(0.01, hi - lo);
  const dt = ((e.clientX - d.startX) / d.trackW) * span;
  const w = state.keep[d.i]; if (!w) return;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(lo, Math.min(d.a + dt, hi - len)); w.start = +ns.toFixed(2); w.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { w.start = +Math.max(lo, Math.min(d.a + dt, w.end - TRIM_MIN)).toFixed(2); }
  else { w.end = +Math.max(w.start + TRIM_MIN, Math.min(d.b + dt, hi)).toFixed(2); }
  renderTrimTrack();
}
function onTrimDblClick(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  state.keep.splice(+bar.dataset.i, 1); syncActiveSub(); renderTrimTrack();
}
function populateTrimSubclips() {
  const sel = $('#trim-subclip'); if (!sel) return;
  if (!state.subclips.length) { sel.innerHTML = '<option value="">Whole clip</option>'; return; }
  const prev = state.activeSub != null ? String(state.activeSub) : '';
  sel.innerHTML = '<option value="">Whole clip</option>' +
    state.subclips.map((sc, i) => `<option value="${i}">${(sc.name || ('sub ' + (i + 1))).replace(/</g, '')}</option>`).join('');
  sel.value = prev && +prev < state.subclips.length ? prev : '';
}
function initTrimStep() {
  if (trimVideo && video) { const src = video.currentSrc || video.src; if (src && trimVideo.src !== src) trimVideo.src = src; }
  populateTrimSubclips();
  applyTrimWindow();
  renderTrimTrack();
  requestAnimationFrame(updateTrimCursor);
}

// ── Caption position windows (top/middle/bottom over time) ──
function _capColor(pos) { return pos === 'top' ? '#4FB0FF' : pos === 'bottom' ? '#46D369' : '#E8FF3A'; }
function renderCapPosTrack() {
  const host = $('#cap-pos-track'); if (!host) return;
  const dur = cutDur() || 1;
  if (!state.capPos.length) { host.innerHTML = '<span class="cut-lane-empty">— caption uses the default position —</span>'; return; }
  host.innerHTML = state.capPos.map((w, i) => {
    const left = (w.start / dur) * 100;
    const width = Math.max(0.8, ((w.end - w.start) / dur) * 100);
    return `<div class="cut-bar cappos" data-i="${i}" style="left:${left}%;width:${width}%;border-color:${_capColor(w.pos)}" title="Caption ${w.pos} ${w.start.toFixed(1)}–${w.end.toFixed(1)}s — drag move/resize, double-click remove">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${w.pos} · ${(w.end - w.start).toFixed(1)}s</span><span class="cut-bar-h r"></span></div>`;
  }).join('');
}
function addCapPos(a, b, pos) {
  a = Math.max(0, Math.min(a, b)); b = Math.max(a, b);
  if (b - a < TRIM_MIN) { setStatus($('#cappos-status'), 'Window too short.', 'err'); return false; }
  state.capPos.push({ start: +a.toFixed(2), end: +b.toFixed(2), pos: pos || 'bottom' });
  state.capPos.sort((x, y) => x.start - y.start);
  renderCapPosTrack();
  setStatus($('#cappos-status'), `Caption ${pos} ${a.toFixed(1)}–${b.toFixed(1)}s added.`, 'ok');
  return true;
}
function setCapPosA() { state.capPosA = state.currentTime || 0; setStatus($('#cappos-status'), `A @ ${state.capPosA.toFixed(2)}s — scrub, then Set B.`, 'ok'); }
function setCapPosB() {
  if (state.capPosA == null) { setStatus($('#cappos-status'), 'Set A first.', 'err'); return; }
  const pos = ($('#cap-pos-new') && $('#cap-pos-new').value) || 'bottom';
  if (addCapPos(state.capPosA, state.currentTime || 0, pos)) state.capPosA = null;
}
function clearCapPos() { state.capPos = []; state.capPosA = null; renderCapPosTrack(); setStatus($('#cappos-status'), 'Cleared — caption uses the default.', 'ok'); }
function onCapPosDown(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i; const w = state.capPos[i]; if (!w) return;
  const rect = $('#cap-pos-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.capPosDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: w.start, b: w.end };
}
function onCapPosMove(e) {
  const d = state.capPosDrag; if (!d) return;
  const dur = cutDur() || 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  const w = state.capPos[d.i]; if (!w) return;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(0, Math.min(d.a + dt, dur - len)); w.start = +ns.toFixed(2); w.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { w.start = +Math.max(0, Math.min(d.a + dt, w.end - TRIM_MIN)).toFixed(2); }
  else { w.end = +Math.max(w.start + TRIM_MIN, Math.min(d.b + dt, dur)).toFixed(2); }
  renderCapPosTrack();
}
function onCapPosDbl(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  state.capPos.splice(+bar.dataset.i, 1); renderCapPosTrack();
}

// ── Text overlays (styled text labels burned over time windows) ──
function addOverlay() {
  const dur = ovDur() || 0;
  const t = ovPlayhead();
  const end = dur ? Math.min(dur, t + 3) : t + 3;
  const o = {
    text: ($('#ovl-text') && $('#ovl-text').value) || 'TEXT',
    start: +t.toFixed(2), end: +end.toFixed(2),
    x_frac: 0.5, y_frac: 0.12,
    size: +($('#ovl-size') && $('#ovl-size').value) || 56,
    font: ($('#ovl-font') && $('#ovl-font').value) || 'Anton',
    color: ($('#ovl-color') && $('#ovl-color').value) || '#ffffff',
  };
  state.overlays.push(o);
  state.overlays.sort((a, b) => a.start - b.start);
  state.ovlSel = state.overlays.indexOf(o);
  syncOverlayControls();
  renderOverlayTrack(); renderOverlayList();
  setStatus($('#ovl-status'), 'Overlay added — type text + set style/position.', 'ok');
}

function selectOverlay(i) {
  if (i < 0 || i >= state.overlays.length) { state.ovlSel = null; return; }
  state.ovlSel = i;
  syncOverlayControls();
  renderOverlayTrack(); renderOverlayList();
}

function syncOverlayControls() {
  const o = state.overlays[state.ovlSel];
  if (!o) return;
  if ($('#ovl-text')) $('#ovl-text').value = o.text;
  if ($('#ovl-font')) $('#ovl-font').value = o.font;
  if ($('#ovl-size')) $('#ovl-size').value = o.size;
  if ($('#ovl-color')) $('#ovl-color').value = o.color;
  const grid = $('#ovl-grid');
  if (grid) grid.querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', +b.dataset.x === o.x_frac && +b.dataset.y === o.y_frac));
}

function editSelectedOverlay(patch) {
  const o = state.overlays[state.ovlSel];
  if (!o) { setStatus($('#ovl-status'), 'Select or add an overlay first.', 'err'); return; }
  Object.assign(o, patch);
  renderOverlayTrack(); renderOverlayList();
}

function renderOverlayTrack() {
  const host = $('#ovl-track');
  if (!host) return;
  const dur = cutDur() || 1;
  if (!state.overlays.length) {
    host.innerHTML = '<span class="cut-lane-empty">— no text overlays —</span>';
    renderOverlayList();
    return;
  }
  host.innerHTML = state.overlays.map((o, i) => {
    const left = (o.start / dur) * 100;
    const width = Math.max(0.8, ((o.end - o.start) / dur) * 100);
    const lbl = (o.text || '').split('\n')[0].slice(0, 12) || 'text';
    return `<div class="cut-bar ovl${i === state.ovlSel ? ' sel' : ''}" data-i="${i}" style="left:${left}%;width:${width}%;border-color:${o.color}" title="${o.start.toFixed(1)}–${o.end.toFixed(1)}s — click to edit, drag move/resize, double-click remove">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${lbl}</span><span class="cut-bar-h r"></span></div>`;
  }).join('');
}

function onOvlDown(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i; const o = state.overlays[i]; if (!o) return;
  selectOverlay(i);
  const rect = $('#ovl-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.ovlDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: o.start, b: o.end };
}

function onOvlMove(e) {
  const d = state.ovlDrag; if (!d) return;
  const dur = cutDur() || 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  const o = state.overlays[d.i]; if (!o) return;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(0, Math.min(d.a + dt, dur - len)); o.start = +ns.toFixed(2); o.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { o.start = +Math.max(0, Math.min(d.a + dt, o.end - TRIM_MIN)).toFixed(2); }
  else { o.end = +Math.max(o.start + TRIM_MIN, Math.min(d.b + dt, dur)).toFixed(2); }
  renderOverlayTrack();
}

function onOvlDbl(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  const i = +bar.dataset.i;
  state.overlays.splice(i, 1);
  if (state.ovlSel === i) state.ovlSel = null;
  else if (state.ovlSel > i) state.ovlSel--;
  renderOverlayTrack(); renderOverlayList();
}

function renderOverlayList() {
  const ol = $('#ovl-list');
  if (!ol) return;
  if (!state.overlays.length) {
    ol.innerHTML = '<li class="empty">No overlays — add one to burn styled text over a time window.</li>';
    return;
  }
  ol.innerHTML = state.overlays.map((o, i) => `
    <li class="${i === state.ovlSel ? 'current' : ''}">
      <span class="o-name">“${((o.text || '').split('\n')[0] || 'text').slice(0, 18)}”</span>
      <span class="o-when">${o.start.toFixed(1)}–${o.end.toFixed(1)}s · ${o.font} ${o.size}px</span>
      <button data-edit="${i}" title="edit">✎</button>
      <button class="danger" data-del="${i}" title="remove">×</button>
    </li>`).join('');
  ol.querySelectorAll('button[data-edit]').forEach(b => b.addEventListener('click', () => {
    selectOverlay(+b.dataset.edit);
  }));
  ol.querySelectorAll('button[data-del]').forEach(b => b.addEventListener('click', () => {
    const i = +b.dataset.del;
    state.overlays.splice(i, 1);
    if (state.ovlSel === i) state.ovlSel = null;
    else if (state.ovlSel > i) state.ovlSel--;
    renderOverlayTrack(); renderOverlayList();
  }));
}

// ── PNG stickers (image overlaid over a time window, alpha kept) ──
function uploadSticker() {
  if (!state.jobId) { setStatus($('#stk-status'), 'Load a clip first.', 'err'); return; }
  if ($('#stk-file')) $('#stk-file').click();
}
async function onStickerFile(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  e.target.value = '';
  setStatus($('#stk-status'), 'Uploading…');
  try {
    const r = await fetch('/api/image-upload?filename=' + encodeURIComponent(file.name), { method: 'POST', body: file });
    if (!r.ok) throw new Error(await r.text());
    const { url, thumb } = await r.json();
    const dur = ovDur() || 0;
    const t = ovPlayhead();
    const end = dur ? Math.min(dur, t + 3) : t + 3;
    const s = { url, thumb: thumb || url, start: +t.toFixed(2), end: +end.toFixed(2),
                x_frac: 0.5, y_frac: 0.5, scale: 0.3, opacity: 1.0 };
    state.stickers.push(s);
    state.stickers.sort((a, b) => a.start - b.start);
    state.stkSel = state.stickers.indexOf(s);
    syncStickerControls();
    renderStickerTrack(); renderStickerList();
    setStatus($('#stk-status'), 'Sticker added — set size/position/window.', 'ok');
  } catch (err) {
    setStatus($('#stk-status'), 'Upload failed: ' + err.message, 'err');
  }
}
function selectSticker(i) {
  if (i < 0 || i >= state.stickers.length) { state.stkSel = null; return; }
  state.stkSel = i;
  syncStickerControls();
  renderStickerTrack(); renderStickerList();
}
function syncStickerControls() {
  const s = state.stickers[state.stkSel];
  if ($('#stk-thumb')) $('#stk-thumb').src = s ? (s.thumb || s.url) : '';
  if (!s) return;
  if ($('#stk-scale')) $('#stk-scale').value = s.scale;
  if ($('#stk-opacity')) $('#stk-opacity').value = s.opacity;
  const grid = $('#stk-grid');
  if (grid) grid.querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', +b.dataset.x === s.x_frac && +b.dataset.y === s.y_frac));
}
function editSelectedSticker(patch) {
  const s = state.stickers[state.stkSel];
  if (!s) { setStatus($('#stk-status'), 'Upload or select a sticker first.', 'err'); return; }
  Object.assign(s, patch);
  renderStickerTrack(); renderStickerList();
}
function renderStickerTrack() {
  const host = $('#stk-track');
  if (!host) return;
  const dur = cutDur() || 1;
  if (!state.stickers.length) {
    host.innerHTML = '<span class="cut-lane-empty">— no stickers —</span>';
    renderStickerList();
    return;
  }
  host.innerHTML = state.stickers.map((s, i) => {
    const left = (s.start / dur) * 100;
    const width = Math.max(0.8, ((s.end - s.start) / dur) * 100);
    return `<div class="cut-bar stk${i === state.stkSel ? ' sel' : ''}" data-i="${i}" style="left:${left}%;width:${width}%" title="${s.start.toFixed(1)}–${s.end.toFixed(1)}s — click to edit, drag move/resize, double-click remove">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">PNG</span><span class="cut-bar-h r"></span></div>`;
  }).join('');
}
function onStkDown(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i; const s = state.stickers[i]; if (!s) return;
  selectSticker(i);
  const rect = $('#stk-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.stkDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: s.start, b: s.end };
}
function onStkMove(e) {
  const d = state.stkDrag; if (!d) return;
  const dur = cutDur() || 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  const s = state.stickers[d.i]; if (!s) return;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(0, Math.min(d.a + dt, dur - len)); s.start = +ns.toFixed(2); s.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { s.start = +Math.max(0, Math.min(d.a + dt, s.end - TRIM_MIN)).toFixed(2); }
  else { s.end = +Math.max(s.start + TRIM_MIN, Math.min(d.b + dt, dur)).toFixed(2); }
  renderStickerTrack();
}
function onStkDbl(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar) return;
  const i = +bar.dataset.i;
  state.stickers.splice(i, 1);
  if (state.stkSel === i) state.stkSel = null;
  else if (state.stkSel > i) state.stkSel--;
  renderStickerTrack(); renderStickerList();
}
function renderStickerList() {
  const ol = $('#stk-list');
  if (!ol) return;
  if (!state.stickers.length) {
    ol.innerHTML = '<li class="empty">No stickers — upload a PNG to overlay it over a time window.</li>';
    return;
  }
  ol.innerHTML = state.stickers.map((s, i) => `
    <li class="${i === state.stkSel ? 'current' : ''}">
      <img class="stk-thumb" src="${s.thumb || s.url}" alt="">
      <span class="o-when">${s.start.toFixed(1)}–${s.end.toFixed(1)}s · ${Math.round(s.scale * 100)}% · ${Math.round(s.opacity * 100)}%α</span>
      <button data-edit="${i}" title="edit">✎</button>
      <button class="danger" data-del="${i}" title="remove">×</button>
    </li>`).join('');
  ol.querySelectorAll('button[data-edit]').forEach(b => b.addEventListener('click', () => selectSticker(+b.dataset.edit)));
  ol.querySelectorAll('button[data-del]').forEach(b => b.addEventListener('click', () => {
    const i = +b.dataset.del;
    state.stickers.splice(i, 1);
    if (state.stkSel === i) state.stkSel = null;
    else if (state.stkSel > i) state.stkSel--;
    renderStickerTrack(); renderStickerList();
  }));
}

// ═══════════════════ Sub-clips: one source → many exports ═══════════════════
// Each sub-clip is an independent SNAPSHOT of the whole editor (crop box, the
// picked illustrations + on/off, split, motion, caption, sfx, trim) + a
// [start,end] export range. "Render all" renders each to its own file.
function subRange() {
  return { start: (state.subA == null ? null : state.subA), end: (state.subB == null ? null : state.subB) };
}
function subDur() { return (subVideo && subVideo.duration) || cutDur() || 1; }
function subPlayhead() { return subVideo ? (subVideo.currentTime || 0) : 0; }
function subSnapshot() {
  return JSON.parse(JSON.stringify({
    box: state.box || [],
    segments: (state.segments || []).map((s) => ({
      t_start: s.t_start, t_end: s.t_end, picked: s.picked || null,
      off: !!s.off, query: s.query || '', candidates: s.candidates || [],
      motion: s.motion || '',
    })),
    topEighths: state.topEighths || 3,
    motion: ($('#ill-motion') && $('#ill-motion').value) || 'auto',
    caption: {
      font: ($('#cap-font') && $('#cap-font').value) || 'Anton',
      size: Number($('#cap-size') && $('#cap-size').value) || 64,
      pos: ($('#cap-pos') && $('#cap-pos').value) || 'middle',
    },
    sfx: state.sfx || [],
    keep: state.keep || [],
    capPos: state.capPos || [],
    overlays: state.overlays || [],
    stickers: state.stickers || [],
  }));
}
function subApply(snap) {
  if (!snap) return;
  const c = (v) => JSON.parse(JSON.stringify(v));
  state.box = c(snap.box || []);
  state.segments = (snap.segments || []).map((s) => {
    const seg = { ...s, _img: null };
    if (seg.picked) { const im = new Image(); im.src = seg.picked; seg._img = im; }   // reload for preview
    return seg;
  });
  state.sfx = c(snap.sfx || []);
  state.keep = c(snap.keep || []);
  state.capPos = c(snap.capPos || []);
  state.overlays = c(snap.overlays || []); state.ovlSel = null;
  state.stickers = c(snap.stickers || []); state.stkSel = null;
  applyTopEighths(snap.topEighths || 3);
  if ($('#ill-motion') && snap.motion) $('#ill-motion').value = snap.motion;
  if ($('#cap-font') && snap.caption) $('#cap-font').value = snap.caption.font;
  if ($('#cap-size') && snap.caption) $('#cap-size').value = snap.caption.size;
  if ($('#cap-pos') && snap.caption && snap.caption.pos) $('#cap-pos').value = snap.caption.pos;
  refreshKfUI();
  renderSegments();
  renderTrimTrack();
  renderCapPosTrack();
  renderOverlayTrack(); renderOverlayList();
  renderStickerTrack(); renderStickerList();
  drawPreview($('#preview'));
}

function addSubclip() {
  if (!state.jobId) { setStatus($('#sub-status'), 'Load a clip first.', 'err'); return; }
  syncActiveSub();   // persist whatever sub-clip was active before adding a new one
  const dur = subDur();
  const r = subRange();
  let start = r.start, end = r.end;
  // No explicit Start set → begin at the PLAYHEAD (where you are now), not 0.
  if (start == null) start = Math.max(0, Math.min(subPlayhead(), dur - 0.2));
  if (end == null) end = Math.min(dur, start + 30);
  const base = ($('#f-title') && $('#f-title').value || 'clip').trim() || 'clip';
  // new sub-clip inherits the current editor buffer (the base box etc.)
  state.subclips.push({ name: `${base}_${state.subclips.length + 1}`, start: +start.toFixed(2), end: +end.toFixed(2), selected: true, snap: subSnapshot() });
  state.activeSub = state.subclips.length - 1;
  state.subA = null; state.subB = null;   // consume the pending range
  renderSubclips(); updateSubRangeLbl();
  setStatus($('#sub-status'), `Sub-clip ${state.subclips.length} added (${start.toFixed(1)}–${end.toFixed(1)}s) & active. Go to Illustration / Text / Crop — edits auto-save into it.`, 'ok');
}
function updateSubclip() {            // explicit save (redundant with auto-save) + apply pending range
  const i = state.activeSub;
  if (i == null || !state.subclips[i]) { addSubclip(); return; }
  syncActiveSub();
  const r = subRange();
  if (r.start != null) state.subclips[i].start = +r.start.toFixed(2);
  if (r.end != null) state.subclips[i].end = +r.end.toFixed(2);
  renderSubclips();
  setStatus($('#sub-status'), `Saved into "${state.subclips[i].name}".`, 'ok');
}
// Auto-save: the editor buffer (box/illustrations/text/stickers/trim/caption) IS
// the ACTIVE sub-clip's content — snapshot it back whenever it changes.
function syncActiveSub() {
  if (state.activeSub != null && state.subclips[state.activeSub]) {
    state.subclips[state.activeSub].snap = subSnapshot();
  }
}
function selectSub(i) {            // make a sub-clip active = load its setup into the editor
  if (typeof illPreviewStop === 'function') illPreviewStop();   // stop preview audio of the old sub-clip
  syncActiveSub();                 // persist the one we're leaving first
  state.activeSub = (i == null || !state.subclips[i]) ? null : i;
  state.subTrimA = null;
  if (state.activeSub != null) subApply(state.subclips[state.activeSub].snap);
  renderSubclips();
}
function loadSubclip(i) {           // select it + jump to the editor to tweak
  const sc = state.subclips[i]; if (!sc) return;
  selectSub(i);                    // loads its setup into the editor buffer (auto-saved)
  if (video && video.duration) video.currentTime = Math.min(sc.start, video.duration - 0.05);
  showStep(2);
  setStatus($('#sub-status'), `Editing "${sc.name}". Crop/Illustration/Text changes auto-save into it.`, 'ok');
}
function deleteSubclip(i) {
  state.subclips.splice(i, 1);
  if (state.activeSub === i) state.activeSub = null;
  else if (state.activeSub != null && state.activeSub > i) state.activeSub--;
  renderSubclips();
}
function renderSubclips() { renderSubBar(); renderSubTrimTrack(); renderSubList(); updateSubCursor(); }
function renderSubBar() {
  const track = $('#subclip-track'); if (!track) return;
  const dur = subDur() || 1;
  track.innerHTML = state.subclips.map((sc, i) => {
    const left = (sc.start / dur) * 100;
    const width = Math.max(2, ((sc.end - sc.start) / dur) * 100);
    const active = i === state.activeSub ? ' active' : '';
    const dim = sc.selected === false ? ' dim' : '';
    return `<div class="cut-bar sub${active}${dim}" data-i="${i}" style="left:${left}%;width:${width}%" title="${sc.name}: ${sc.start.toFixed(1)}–${sc.end.toFixed(1)}s — click to select (edit its Trim), drag to move, edges to resize">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${i + 1}. ${sc.name}</span><span class="cut-bar-h r"></span></div>`;
  }).join('') || '<span class="cut-lane-empty">— set up a moment, set a range, then + Add sub-clip —</span>';
}
// Trim lane shows the SELECTED sub-clip's keep-windows (the parts kept; the rest
// is cut — dead air / white noise removed). Edits write into sc.snap.keep.
// The active sub-clip is loaded into the editor buffer, so its trim = state.keep
// (same array the Crop step's Trim lane edits). Edits here sync straight back.
function renderSubTrimTrack() {
  const track = $('#sub-trim-track'); const ctrls = $('#sub-trim-ctrls');
  const active = state.activeSub != null && state.subclips[state.activeSub];
  if (ctrls) ctrls.hidden = !active;
  if (!track) return;
  if (!active) { track.innerHTML = '<span class="cut-lane-empty">— select a sub-clip above to trim it —</span>'; return; }
  const dur = subDur() || 1;
  track.innerHTML = (state.keep || []).map((w, i) => {
    const left = (w.start / dur) * 100;
    const width = Math.max(0.8, ((w.end - w.start) / dur) * 100);
    return `<div class="cut-bar keep" data-i="${i}" style="left:${left}%;width:${width}%;background:rgba(70,211,105,.20);border-color:#46d369" title="keep ${w.start.toFixed(1)}–${w.end.toFixed(1)}s — drag move/resize, double-click remove">`
      + `<span class="cut-bar-h l"></span><span class="cut-bar-lbl">${(w.end - w.start).toFixed(1)}s</span><span class="cut-bar-h r"></span></div>`;
  }).join('') || '<span class="cut-lane-empty">— whole sub-clip kept · Set A → Set B to cut dead air —</span>';
}
function subTrimSetA() {
  if (state.activeSub == null) { setStatus($('#sub-status'), 'Select a sub-clip first.', 'err'); return; }
  state.subTrimA = subPlayhead();
  setStatus($('#sub-status'), `Trim A @ ${state.subTrimA.toFixed(2)}s — scrub, then Set B.`, 'ok');
}
function subTrimSetB() {
  if (state.activeSub == null) { setStatus($('#sub-status'), 'Select a sub-clip first.', 'err'); return; }
  if (state.subTrimA == null) { setStatus($('#sub-status'), 'Set A first.', 'err'); return; }
  let a = state.subTrimA, b = subPlayhead();
  a = Math.max(0, Math.min(a, b)); b = Math.max(a, b);
  const dur = subDur() || 0; if (dur) b = Math.min(b, dur);
  if (b - a < 0.2) { setStatus($('#sub-status'), 'Window too short.', 'err'); return; }
  state.keep = state.keep || [];
  state.keep.push({ start: +a.toFixed(2), end: +b.toFixed(2) });
  state.keep.sort((x, y) => x.start - y.start);
  state.subTrimA = null;
  syncActiveSub(); renderTrimTrack(); renderSubclips();
  setStatus($('#sub-status'), `Kept ${a.toFixed(1)}–${b.toFixed(1)}s.`, 'ok');
}
function subTrimClear() {
  if (state.activeSub == null) return;
  state.keep = []; state.subTrimA = null;
  syncActiveSub(); renderTrimTrack(); renderSubclips();
  setStatus($('#sub-status'), 'Trim cleared — whole sub-clip kept.', 'ok');
}
function onSubTrimDown(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar || state.activeSub == null) return;
  e.preventDefault();
  const i = +bar.dataset.i; const w = (state.keep || [])[i]; if (!w) return;
  const rect = $('#sub-trim-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.subTrimDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: w.start, b: w.end };
}
function onSubTrimMove(e) {
  const d = state.subTrimDrag; if (!d) return;
  const w = (state.keep || [])[d.i]; if (!w) return;
  const dur = subDur() || 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(0, Math.min(d.a + dt, dur - len)); w.start = +ns.toFixed(2); w.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { w.start = +Math.max(0, Math.min(d.a + dt, w.end - 0.2)).toFixed(2); }
  else { w.end = +Math.max(w.start + 0.2, Math.min(d.b + dt, dur)).toFixed(2); }
  renderSubTrimTrack();
}
function onSubTrimDbl(e) {
  const bar = e.target.closest('.cut-bar'); if (!bar || !state.keep) return;
  state.keep.splice(+bar.dataset.i, 1); syncActiveSub(); renderTrimTrack(); renderSubclips();
}
function renderSubList() {
  const ol = $('#subclip-list'); if (!ol) return;
  if (!state.subclips.length) { ol.innerHTML = '<li class="empty">No sub-clips yet — mark a range + Add sub-clip.</li>'; return; }
  ol.innerHTML = state.subclips.map((sc, i) => {
    const nKeep = (i === state.activeSub ? (state.keep || []) : (sc.snap && sc.snap.keep || [])).length;
    const trim = nKeep ? `<span class="sc-trim" title="keeps ${nKeep} window(s), rest cut">✓ trim ${nKeep}</span>` : '';
    return `<li class="${i === state.activeSub ? 'current' : ''}" data-row="${i}">
      <input type="checkbox" data-sel="${i}" ${sc.selected === false ? '' : 'checked'} title="include in Render selected">
      <input class="sc-name subclip-name" data-i="${i}" value="${(sc.name || '').replace(/"/g, '')}" title="output filename">
      <span class="sc-when">${sc.start.toFixed(1)}–${sc.end.toFixed(1)}s</span>
      ${trim}
      <button class="sub-load" data-load="${i}" title="load into the editor to tweak crop/illustration/caption">✎</button>
      <button class="sub-del danger" data-del="${i}" title="delete">×</button>
    </li>`;
  }).join('');
  ol.querySelectorAll('input[type=checkbox][data-sel]').forEach((cb) => cb.addEventListener('change', () => {
    const sc = state.subclips[+cb.dataset.sel]; if (sc) { sc.selected = cb.checked; renderSubBar(); }
  }));
  ol.querySelectorAll('.subclip-name').forEach((inp) => {
    inp.addEventListener('change', () => { const sc = state.subclips[+inp.dataset.i]; if (sc) { sc.name = inp.value.trim() || sc.name; renderSubBar(); } });
    inp.addEventListener('click', (e) => e.stopPropagation());
  });
  ol.querySelectorAll('[data-load]').forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); loadSubclip(+b.dataset.load); }));
  ol.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); deleteSubclip(+b.dataset.del); }));
  ol.querySelectorAll('li[data-row]').forEach((li) => li.addEventListener('click', () => selectSub(+li.dataset.row)));
}
function setSubRange(which, t) {
  t = +(+t).toFixed(2);
  if (which === 'start') state.subA = t; else state.subB = t;
  updateSubRangeLbl();
  setStatus($('#sub-status'), `Range ${which} = ${t}s. Set both, then + Add sub-clip.`, 'ok');
}
function updateSubRangeLbl() {
  const el = $('#subclip-range-lbl'); if (!el) return;
  const r = subRange();
  el.textContent = (r.start == null && r.end == null) ? 'range: full clip'
    : `range: ${r.start == null ? '0' : r.start.toFixed(1)}–${r.end == null ? 'end' : r.end.toFixed(1)}s`;
}
function updateSubCursor() {
  const cur = $('#sub-cursor'), track = $('#subclip-track'), wrap = $('#sub-tl-wrap');
  if (!cur || !track || !wrap) return;
  const dur = subDur() || 1;
  const wR = wrap.getBoundingClientRect(), tR = track.getBoundingClientRect();
  if (!tR.width) return;
  cur.style.left = ((tR.left - wR.left) + (subPlayhead() / dur) * tR.width) + 'px';
}
function onSubTrackDown(e) {
  const bar = e.target.closest('.cut-bar');
  if (!bar) return;
  e.preventDefault();
  const i = +bar.dataset.i; const sc = state.subclips[i]; if (!sc) return;
  const rect = $('#subclip-track').getBoundingClientRect();
  const isH = e.target.classList.contains('cut-bar-h');
  const mode = isH && e.target.classList.contains('l') ? 'resize-l'
             : isH && e.target.classList.contains('r') ? 'resize-r' : 'move';
  state.subDrag = { i, mode, startX: e.clientX, trackW: rect.width, a: sc.start, b: sc.end, moved: false };
}
function onSubDragMove(e) {
  const d = state.subDrag; if (!d) return;
  if (Math.abs(e.clientX - d.startX) > 3) d.moved = true;
  const dur = subDur() || 1;
  const dt = ((e.clientX - d.startX) / d.trackW) * dur;
  const sc = state.subclips[d.i]; if (!sc) return;
  if (d.mode === 'move') { const len = d.b - d.a; const ns = Math.max(0, Math.min(d.a + dt, dur - len)); sc.start = +ns.toFixed(2); sc.end = +(ns + len).toFixed(2); }
  else if (d.mode === 'resize-l') { sc.start = +Math.max(0, Math.min(d.a + dt, sc.end - 0.2)).toFixed(2); }
  else { sc.end = +Math.max(sc.start + 0.2, Math.min(d.b + dt, dur)).toFixed(2); }
  renderSubBar();
}
const SUB_MOTION_CYCLE = ['zoom_in', 'pan_right', 'zoom_out', 'pan_left'];
function buildSubRenderBody(sc) {
  const s = sc.snap;
  const mode = s.motion || 'auto';
  // Drop segments fully trimmed out of THIS sub-clip (its own keep + range).
  const kept = (seg) => !segTrimInfo(seg, s.keep || [], sc.start, sc.end).fullyCut;
  const picks = (s.segments || []).filter((seg) => seg.picked && !seg.off && kept(seg));
  const illustrations = picks.map((seg, i) => ({
    t_start: seg.t_start, t_end: seg.t_end, url: seg.picked,
    motion: seg.motion ? seg.motion : (mode === 'auto' ? SUB_MOTION_CYCLE[i % SUB_MOTION_CYCLE.length] : mode),
  }));
  // OFF or no picked image → full-screen video (never a black bottom slot).
  const fullscreen = (s.segments || []).filter((seg) => (seg.off || !seg.picked) && kept(seg)).map((seg) => ({ t_start: seg.t_start, t_end: seg.t_end }));
  return {
    job_id: state.jobId,
    title: sc.name,
    box: s.box || [],
    illustrations,
    fullscreen_windows: fullscreen,
    keep_segments: (s.keep || []).map((w) => ({ start: w.start, end: w.end })),
    words: state.words || [],
    caption_font: (s.caption && s.caption.font) || 'Anton',
    caption_size: (s.caption && s.caption.size) || 64,
    caption_pos: (s.caption && s.caption.pos) || 'middle',
    caption_pos_ranges: (s.capPos || []).map((w) => ({ start: w.start, end: w.end, pos: w.pos })),
    text_overlays: (s.overlays || []).map((o) => ({ text: o.text, start: o.start, end: o.end, x_frac: o.x_frac, y_frac: o.y_frac, size: o.size, font: o.font, color: o.color })),
    stickers: (s.stickers || []).map((k) => ({ url: k.url, start: k.start, end: k.end, x_frac: k.x_frac, y_frac: k.y_frac, scale: k.scale, opacity: k.opacity })),
    cleanup: false,
    render_start: sc.start,
    render_end: sc.end,
    sfx: s.sfx || [],
    top_eighths: s.topEighths || 3,
  };
}
// status to BOTH the Sub-clips page and the Render page (whichever is visible)
function subStatus(msg, kind) {
  const a = $('#sub-status'); if (a) setStatus(a, msg, kind);
  const b = $('#render-subs-status'); if (b) setStatus(b, msg, kind);
}
// Per-sub-clip render outcomes, keyed by sub-clip index → {filename,output_path}
// | {error} | {rendering:true, pct} while in-flight. Reset when a job is opened.
let _subRenderRes = {};
// Update just one row's result cell (cheap — avoids rebuilding the whole list on
// every progress tick, which would drop focus / re-bind listeners).
// Label for an in-flight render. Two phases: 'uploading' = sending the source to a
// GPU box (the slow part for a big clip) → "uploading N%"; 'rendering' = ffmpeg,
// where ≥98 = encode tail + transfer back → "finishing…" (never a frozen "99%").
function pctLabel(pct, phase) {
  if (phase === 'uploading') return pct > 0 ? `uploading ${Math.round(pct)}%` : 'uploading…';
  if (!(pct > 0)) return 'rendering…';
  if (pct >= 98) return 'finishing…';
  return Math.round(pct) + '%';
}
function updateSubRowPct(i, pct, phase) {
  const el = document.querySelector(`#render-subs-list li[data-i="${i}"] .rs-res`);
  if (el) el.innerHTML = `<span class="rs-pct">${pctLabel(pct, phase)}</span>`;
}
// Optionally make a thumbnail PNG for a sub-clip (the "also make thumbnail" toggle).
// Serialized via a lock because it drives the single #thumb-video element.
let _thumbGenLock = Promise.resolve();
function generateThumbForSub(sc) {
  const run = _thumbGenLock.then(() => _doGenThumb(sc), () => _doGenThumb(sc));
  _thumbGenLock = run.catch(() => {});
  return run;
}
async function _doGenThumb(sc) {
  const v = thumbVideo;
  if (!v) return null;
  const path = video.getAttribute('src');
  if (path && v.dataset.path !== path) { v.dataset.path = path; v.src = path; }
  if (!v.videoWidth) await new Promise((r) => { v.addEventListener('loadeddata', r, { once: true }); setTimeout(r, 6000); });
  const t = Math.max(0, Math.min((sc.start || 0) + 0.2, (v.duration || 1) - 0.05));
  await _seekThumb(v, t);
  try { await document.fonts.load(`bold ${thumb.size}px "${thumb.font}"`); } catch (_) {}
  const off = document.createElement('canvas'); off.width = THUMB_OUT_W; off.height = THUMB_OUT_H;
  drawThumbnailInto(off.getContext('2d'), THUMB_OUT_W, THUMB_OUT_H);
  const blob = await new Promise((res) => off.toBlob(res, 'image/png'));
  if (!blob) return null;
  const fname = ((sc.name || 'clip').trim() || 'clip').replace(/[^\w.-]+/g, '_') + '_thumbnail.png';
  const r = await fetch('/api/save-output-image?filename=' + encodeURIComponent(fname), { method: 'POST', body: blob });
  if (!r.ok) return null;
  return await r.json();   // {output_path, filename}
}

// Render one sub-clip into slot `gi`, polling /api/render-progress for a live %.
async function _renderSubInto(sc, gi) {
  const token = 'r' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
  if (gi >= 0) { _subRenderRes[gi] = { rendering: true, pct: null }; refreshRenderSubs(); }
  let poll = setInterval(async () => {
    try {
      const p = await (await fetch('/api/render-progress?id=' + token)).json();
      const r = gi >= 0 ? _subRenderRes[gi] : null;
      if (r && r.rendering && p && p.pct != null) { r.pct = p.pct; r.phase = p.phase; updateSubRowPct(gi, p.pct, p.phase); }
    } catch (_) { /* best-effort */ }
  }, 600);
  const stopPoll = () => { if (poll) { clearInterval(poll); poll = null; } };
  try {
    const body = buildSubRenderBody(sc); body.progress_id = token;
    const r = await api('render', body);
    stopPoll();   // video is done → stop the % poll and flip the row to the link NOW
    if (gi >= 0) { _subRenderRes[gi] = { filename: r.filename, output_path: r.output_path }; refreshRenderSubs(); }
    // "also make thumbnail" → export a PNG per sub-clip in the BACKGROUND (don't
    // block the row on it, and don't hold up the next sub-clip's video render —
    // the thumbnail 🖼 link just appears when it's ready).
    if (gi >= 0 && $('#render-thumbs') && $('#render-thumbs').checked) {
      generateThumbForSub(sc)
        .then((th) => { if (th && _subRenderRes[gi] && !_subRenderRes[gi].error) { _subRenderRes[gi].thumb = th; refreshRenderSubs(); } })
        .catch(() => { /* thumb is best-effort */ });
    }
  } catch (e) {
    if (gi >= 0) _subRenderRes[gi] = { error: e.message };
  } finally { stopPoll(); }
}
async function renderSubs(list, btnId) {
  syncActiveSub();   // flush the active sub-clip's latest edits before rendering
  if (!list.length) { subStatus('No sub-clips selected.', 'err'); return; }
  const names = list.map((s) => s.name);
  if (new Set(names).size !== names.length) { subStatus('Sub-clip names must be unique (they become filenames).', 'err'); return; }
  const btn = $(btnId); if (btn) btn.disabled = true;
  // Dispatch up to one render per GPU box concurrently (the farm gives each a
  // different box). Local render (no farm) → 1 at a time to avoid GPU contention.
  const conc = Math.min(list.length, Math.max(1, state.renderBoxes || 1));
  const total = list.length;
  const queue = list.slice();
  let done = 0;
  async function worker() {
    while (queue.length) {
      const sc = queue.shift();
      await _renderSubInto(sc, state.subclips.indexOf(sc));
      done++;
      subStatus(conc > 1 ? `Rendering across ${conc} GPUs… ${done}/${total} done` : `Rendering ${done}/${total}…`);
      refreshRenderSubs();
    }
  }
  try {
    await Promise.all(Array.from({ length: conc }, worker));
    const outs = list.map((sc) => _subRenderRes[state.subclips.indexOf(sc)]).filter(Boolean);
    const ok = outs.filter((o) => !o.error).length;
    subStatus(`Done — ${ok}/${total} sub-clip(s) rendered${conc > 1 ? ` (×${conc} GPUs)` : ''}.`, ok ? 'ok' : 'err');
  } finally { if (btn) btn.disabled = false; }
}
// Render ONE sub-clip (its own per-row 🎬 button).
async function renderOneSub(i) {
  const sc = state.subclips[i]; if (!sc) return;
  syncActiveSub();
  subStatus(`Rendering ${sc.name}…`);
  await _renderSubInto(sc, i);
  const o = _subRenderRes[i];
  subStatus(o && !o.error ? `Rendered ${sc.name} ✓` : `Failed ${sc.name}: ${o && o.error}`, o && !o.error ? 'ok' : 'err');
  refreshRenderSubs();
}
function renderSelectedSubclips() {
  const list = state.subclips.filter((s) => s.selected !== false);
  if (!list.length) { subStatus('Tick at least one sub-clip first.', 'err'); return; }
  renderSubs(list, '#btn-render-subs-sel');
}
function renderAllSubclips() { renderSubs(state.subclips, '#btn-render-subs-all'); }

// On the Render step, list every sub-clip with its status + its own render button
// + result, so each can be rendered & downloaded individually (not just in bulk).
function refreshRenderSubs() {
  const wrap = $('#render-subs'); if (!wrap) return;
  const n = state.subclips.length;
  wrap.hidden = n === 0;
  const nEl = $('#render-subs-n'); if (nEl) nEl.textContent = n;
  const note = $('#render-single-note');
  if (note) note.textContent = n ? '· (the single button below renders just the current view)' : '';
  const farm = $('#render-farm-note');
  if (farm) farm.innerHTML = (state.renderBoxes > 0)
    ? `⚡ GPU farm: <b>${state.renderBoxes}</b> box(es) — bulk renders run in parallel.`
    : 'Rendering locally (1 GPU). <span class="muted">Set ILLUSTRATOR_RENDER_REMOTE_URLS to use the farm.</span>';
  // Mirror outcomes onto the Sub-clips page list too (#subclip-results).
  const legacy = $('#subclip-results');
  if (legacy) legacy.innerHTML = state.subclips.map((sc, i) => {
    const o = _subRenderRes[i];
    if (!o) return '';
    return o.error ? `<li class="err">${sc.name}: ${o.error}</li>` : `<li>${sc.name} — <a href="${o.output_path}" download>↓ ${o.filename}</a></li>`;
  }).filter(Boolean).join('');
  const list = $('#render-subs-list'); if (!list) return;
  if (!n) { list.innerHTML = ''; return; }
  list.innerHTML = state.subclips.map((sc, i) => {
    const active = i === state.activeSub;
    const segs = active ? state.segments : ((sc.snap && sc.snap.segments) || []);
    const keep = active ? (state.keep || []) : ((sc.snap && sc.snap.keep) || []);
    const pics = segs.filter((g) => g.picked && !g.off && !segTrimInfo(g, keep, sc.start, sc.end).fullyCut).length;
    const fulls = segs.filter((g) => (g.off || !g.picked) && !segTrimInfo(g, keep, sc.start, sc.end).fullyCut).length;
    const r = _subRenderRes[i];
    const busy = !!(r && r.rendering);
    const res = busy ? `<span class="rs-pct">${pctLabel(r.pct, r.phase)}</span>`
      : r ? (r.error ? `<span class="err" title="${(r.error || '').replace(/"/g, '')}">failed</span>`
                     : `<a href="${r.output_path}" download>↓ ${r.filename}</a>${r.thumb ? ` <a href="${r.thumb.output_path}" download title="thumbnail PNG">🖼</a>` : ''}`)
          : '';
    return `<li class="rs-row${active ? ' current' : ''}" data-i="${i}">
      <input type="checkbox" data-rsel="${i}" ${sc.selected === false ? '' : 'checked'} title="include in Render selected">
      <span class="rs-name" title="${(sc.name || '').replace(/"/g, '')}">${(sc.name || ('sub ' + (i + 1))).replace(/</g, '')}</span>
      <span class="rs-when">${sc.start.toFixed(1)}–${sc.end.toFixed(1)}s</span>
      <span class="rs-meta">${pics} 🖼${fulls ? ' · ' + fulls + ' 🎬' : ''}${keep.length ? ' · ✂' + keep.length : ''}</span>
      <button class="rs-go" data-rgo="${i}" ${busy ? 'disabled' : ''}>${busy ? '⏳ …' : '🎬 Render'}</button>
      <span class="rs-res">${res}</span>
    </li>`;
  }).join('');
  list.querySelectorAll('[data-rgo]').forEach((b) => b.addEventListener('click', () => renderOneSub(+b.dataset.rgo)));
  list.querySelectorAll('[data-rsel]').forEach((c) => c.addEventListener('change', () => {
    const sc = state.subclips[+c.dataset.rsel]; if (sc) sc.selected = c.checked;
  }));
}

function renderSegments() {
  const host = $('#ill-segments');
  if (!state.segments.length) { host.innerHTML = '<div class="muted">Not generated yet.</div>'; renderIllBars(); return; }
  host.innerHTML = '';
  state.segments.forEach((seg) => {
    const card = document.createElement('div');
    card.className = 'seg-card';
    const ti = segTrimInfo(seg);
    // Fully trimmed out → a dimmed, minimal card (no picking needed, auto-skipped).
    if (ti.fullyCut) {
      card.classList.add('seg-cut');
      card.innerHTML = `
        <div class="seg-head">
          <span class="seg-time">${seg.t_start.toFixed(0)}–${seg.t_end.toFixed(0)}s <span class="seg-cut-badge">✂ trimmed out</span></span>
          <span class="seg-text">"${(seg.text || '').slice(0, 80) || '(no speech)'}"</span>
        </div>
        <div class="seg-candidates"><span class="seg-cut-note">Bagian ini kepotong di Trim (step 04) — gak usah pilih gambar, otomatis di-skip pas render. Ubah trim kalau mau dipakai lagi.</span></div>`;
      host.appendChild(card);
      return;
    }
    const timeBadge = ti.partial ? ` <span class="seg-cut-badge partial" title="sebagian kepotong di Trim">✂ ${ti.kept.toFixed(1)}s kept</span>` : '';
    const cands = (seg.candidates || []).map((c) => `
      <div class="cand ${seg.picked === c.full ? 'picked' : ''}" data-id="${c.id}">
        <img src="${c.thumb}" alt="${(c.alt || '').replace(/"/g, '')}" loading="lazy">
        <span class="cand-by">${c.photographer || ''}</span>
      </div>`).join('');
    if (seg.off) card.classList.add('off');
    const body = seg.off
      ? '<div class="seg-candidates"><span class="seg-fullscreen">🎬 Full-screen video — the clip fills the whole frame here, no illustration.</span></div>'
      : `<div class="seg-candidates">${cands || '<span class="seg-none">No results (check PEXELS_API_KEY / edit the keyword).</span>'}</div>`;
    card.innerHTML = `
      <div class="seg-head">
        <span class="seg-time">${seg.t_start.toFixed(0)}–${seg.t_end.toFixed(0)}s${timeBadge}</span>
        <span class="seg-text">"${(seg.text || '').slice(0, 80) || '(no speech)'}"</span>
        <button class="seg-mode ghost" title="Toggle: show an illustration here, or let the video fill the whole 9:16 frame (full bbox, no illustration)">${seg.off ? '🎬 Full-screen video' : '🖼 Illustration'}</button>
        <select class="seg-motion" ${seg.off ? 'disabled' : ''} title="Motion for THIS illustration (auto = follow the global cycle)">
          <option value="">↕ auto</option>
          <option value="none"${seg.motion === 'none' ? ' selected' : ''}>no motion</option>
          <option value="zoom_in"${seg.motion === 'zoom_in' ? ' selected' : ''}>zoom in</option>
          <option value="zoom_out"${seg.motion === 'zoom_out' ? ' selected' : ''}>zoom out</option>
          <option value="pan_right"${seg.motion === 'pan_right' ? ' selected' : ''}>pan →</option>
          <option value="pan_left"${seg.motion === 'pan_left' ? ' selected' : ''}>pan ←</option>
        </select>
        <input class="seg-query" value="${(seg.query || '').replace(/"/g, '')}" ${seg.off ? 'disabled' : ''}>
        <button class="seg-research ghost" ${seg.off ? 'disabled' : ''}>↻ search again</button>
        <button class="seg-upload ghost" ${seg.off ? 'disabled' : ''} title="use your own image for this segment">📁 Upload</button>
      </div>
      ${body}`;
    // per-illustration motion
    const motSel = card.querySelector('.seg-motion');
    if (motSel) { motSel.value = seg.motion || ''; motSel.addEventListener('change', () => { seg.motion = motSel.value; drawPreview($('#preview')); const ip = $('#preview-ill'); if (ip) drawPreview(ip); }); }

    // upload your own image for this segment
    card.querySelector('.seg-upload').addEventListener('click', () => {
      const inp = document.createElement('input');
      inp.type = 'file'; inp.accept = 'image/*';
      inp.addEventListener('change', async () => {
        const file = inp.files && inp.files[0];
        if (!file) return;
        const btn = card.querySelector('.seg-upload');
        btn.disabled = true; btn.textContent = '…';
        try {
          const r = await fetch('/api/image-upload?filename=' + encodeURIComponent(file.name), { method: 'POST', body: file });
          if (!r.ok) throw new Error(await r.text());
          const { url, thumb } = await r.json();
          const c = { id: 'up_' + Math.abs(url.split('').reduce((a, ch) => (a * 31 + ch.charCodeAt(0)) | 0, 7)), thumb: thumb || url, full: url, alt: '(uploaded)', photographer: 'you' };
          seg.candidates = [c, ...(seg.candidates || [])];
          pickCandidate(seg, c, true);
          renderSegments();
        } catch (e) {
          btn.disabled = false; btn.textContent = '📁 Upload';
        }
      });
      inp.click();
    });

    // toggle illustration ↔ full-screen video for this window
    card.querySelector('.seg-mode').addEventListener('click', () => {
      seg.off = !seg.off;
      renderSegments();
      drawPreview($('#preview'));
      const ip = $('#preview-ill'); if (ip) drawPreview(ip);
    });
    // candidate click → pick
    card.querySelectorAll('.cand').forEach((el) => el.addEventListener('click', () => {
      const c = seg.candidates.find((x) => x.id === el.dataset.id);
      if (c) pickCandidate(seg, c, true);
    }));
    // re-search with edited query
    card.querySelector('.seg-research').addEventListener('click', async () => {
      const q = card.querySelector('.seg-query').value.trim();
      seg.query = q;
      const btn = card.querySelector('.seg-research');
      btn.disabled = true; btn.textContent = '…';
      try {
        const source = ($('#ill-source') && $('#ill-source').value) || undefined;
        const r = await api('search', { query: q, source });
        seg.candidates = r.candidates || [];
        seg.picked = seg.candidates.length ? seg.candidates[0].full : null;
        if (seg.candidates.length) pickCandidate(seg, seg.candidates[0], false);
        renderSegments();
      } catch (e) {
        btn.disabled = false; btn.textContent = '↻ search again';
      }
    });
    host.appendChild(card);
  });
  renderIllBars();
}

$('#ill-bars').addEventListener('mousedown', onIllBarDown);
window.addEventListener('mousemove', onIllDragMove);
window.addEventListener('mouseup', () => {
  if (state.illDrag) { state.illDrag = null; renderSegments(); }
});

$('#btn-to-render').addEventListener('click', () => showStep(4));

// ───────────────────────── step 4: render ─────────────────────────
$('#rd-clear').addEventListener('click', () => { $('#rd-start').value = ''; $('#rd-end').value = ''; });

// "Auto" cycles through these so consecutive images alternate zoom/pan → variety.
const MOTION_CYCLE = ['zoom_in', 'pan_right', 'zoom_out', 'pan_left'];
function buildIllustrations() {
  const mode = ($('#ill-motion') && $('#ill-motion').value) || 'auto';
  return state.segments
    .filter((s) => s.picked && !s.off && !segTrimInfo(s).fullyCut)   // OFF / trimmed-out windows show no image
    .map((s, i) => {
      // per-illustration motion wins; empty = follow the global cycle/mode
      const motion = s.motion ? s.motion : (mode === 'auto' ? MOTION_CYCLE[i % MOTION_CYCLE.length] : mode);
      return { t_start: s.t_start, t_end: s.t_end, url: s.picked, motion };
    });
}

// Full-screen windows → the video fills the whole 9:16 frame (no illustration
// split). A window is full-screen when toggled OFF (🎬) OR when it has no picked
// image — so a missing illustration falls back to full video, never a black slot.
function buildFullscreen() {
  return state.segments
    .filter((s) => (s.off || !s.picked) && !segTrimInfo(s).fullyCut)   // trimmed-out windows need no fullscreen marker (cut anyway)
    .map((s) => ({ t_start: s.t_start, t_end: s.t_end }));
}

async function doRender(withCaption) {
  if (!state.box.length) { setStatus($('#rd-status'), 'No crop box yet', 'err'); return; }
  const rs = $('#rd-start').value, re = $('#rd-end').value;
  setStatus($('#rd-status'), withCaption ? 'Rendering + caption…' : 'Rendering (no caption)…');
  $('#btn-render').disabled = $('#btn-render-nocap').disabled = true;
  try {
    const r = await api('render', {
      job_id: state.jobId,
      title: $('#f-title').value.trim() || 'clip',
      box: state.box,
      illustrations: buildIllustrations(),
      fullscreen_windows: buildFullscreen(),
      keep_segments: state.keep.map((w) => ({ start: w.start, end: w.end })),
      words: withCaption ? state.words : [],
      caption_font: $('#cap-font').value,
      caption_size: Number($('#cap-size').value) || 64,
      caption_pos: ($('#cap-pos') && $('#cap-pos').value) || 'middle',
      caption_pos_ranges: (state.capPos || []).map((w) => ({ start: w.start, end: w.end, pos: w.pos })),
      text_overlays: (state.overlays || []).map((o) => ({ text: o.text, start: o.start, end: o.end, x_frac: o.x_frac, y_frac: o.y_frac, size: o.size, font: o.font, color: o.color })),
      stickers: (state.stickers || []).map((s) => ({ url: s.url, start: s.start, end: s.end, x_frac: s.x_frac, y_frac: s.y_frac, scale: s.scale, opacity: s.opacity })),
      cleanup: false,
      render_start: rs === '' ? null : Number(rs),
      render_end: re === '' ? null : Number(re),
      sfx: state.sfx,
      top_eighths: state.topEighths || 3,
    });
    setStatus($('#rd-status'), 'OK', 'ok');
    showResult(r);
  } catch (e) {
    setStatus($('#rd-status'), e.message, 'err');
  } finally {
    $('#btn-render').disabled = $('#btn-render-nocap').disabled = false;
  }
}
$('#btn-render').addEventListener('click', () => doRender(true));
$('#btn-render-nocap').addEventListener('click', () => doRender(false));

function showResult(r) {
  const box = $('#render-result');
  box.classList.remove('hidden');
  const card = $('#result-card');
  card.querySelector('video').src = r.output_path;
  const dl = card.querySelector('.result-dl');
  dl.href = r.output_path; dl.setAttribute('download', r.filename);
  $('#result-card-title').textContent = r.filename;
}

$('#btn-done').addEventListener('click', async () => {
  if (!state.jobId) return;
  try { await api('cleanup', { job_id: state.jobId }); } catch (e) {}
  $('#btn-done').textContent = 'Cleaned ✓';
  $('#btn-done').disabled = true;
});

// ───────────────────────── AI auto-box ─────────────────────────
// A single pair of draggable handles sets [start,end]; "Generate" asks the vision
// model for the prompted subject's box across that range and drops the resulting
// track into the crop box (state.box), replacing keyframes inside the range.
const AB_EPS = 0.15;

function clampAutoRange() {
  const dur = state.duration || video.duration || 0;
  let start = state.autoRange.start ?? 0;
  let end = state.autoRange.end == null ? dur : state.autoRange.end;
  start = Math.max(0, Math.min(start, dur));
  end = Math.max(0, Math.min(end, dur));
  const MIN = 0.2;
  if (end < start + MIN) {
    if (state.abDrag === 'start') start = Math.max(0, end - MIN);
    else end = Math.min(dur, start + MIN);
  }
  state.autoRange.start = start;
  state.autoRange.end = end;
}

function renderAutoRange() {
  const rangeEl = $('#ab-range');
  if (!rangeEl) return;
  const dur = state.duration || video.duration || 1;
  clampAutoRange();
  const s = state.autoRange.start;
  const e = state.autoRange.end == null ? dur : state.autoRange.end;
  const sp = (s / dur) * 100, ep = (e / dur) * 100;
  $('#ab-h-start').style.left = sp + '%';
  $('#ab-h-end').style.left = ep + '%';
  const band = $('#ab-band');
  band.style.left = sp + '%';
  band.style.width = Math.max(0, ep - sp) + '%';
  $('#ab-start-lbl').textContent = s.toFixed(1) + 's';
  $('#ab-end-lbl').textContent = e.toFixed(1) + 's';
}

function startAbDrag(e, which) { e.preventDefault(); state.abDrag = which; }

function onAbDragMove(e) {
  if (!state.abDrag) return;
  const rangeEl = $('#ab-range');
  const dur = state.duration || video.duration || 0;
  if (!rangeEl || !dur) return;
  const r = rangeEl.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  const t = frac * dur;
  if (state.abDrag === 'start') state.autoRange.start = t;
  else state.autoRange.end = t;
  renderAutoRange();
}

async function doAutoBox() {
  const raw = ($('#ab-prompt').value || '').trim();
  if (!raw) { setStatus($('#ab-status'), 'Type what to track first', 'err'); return; }
  if (!state.jobId) return;
  // Merge [model observation] + [shared layout context] + [box instruction],
  // matching the batch worker. The context carries the {layout}/{side}
  // placeholders, which the backend resolves per detected segment.
  const obs = (state.autoObs || '').trim();
  const ctx = ($('#ab-context') ? $('#ab-context').value : '').trim();
  const prompt = [obs, ctx, raw].filter(Boolean).join(' ');
  const dur = state.duration || video.duration || 0;
  const t0 = Math.max(0, state.autoRange.start || 0);
  const t1 = state.autoRange.end == null ? dur : state.autoRange.end;
  const step = Number($('#ab-density').value) || 0.4;
  const btn = $('#ab-generate');
  btn.disabled = true;
  setStatus($('#ab-status'),
    `Predicting boxes for "${prompt}" over ${t0.toFixed(1)}–${t1.toFixed(1)}s… (AI scanning frames)`);
  try {
    const r = await api('autobox', {
      job_id: state.jobId, prompt, t_start: t0, t_end: t1, box: 1, step_seconds: step,
      lock_size: $('#ab-lock') ? $('#ab-lock').checked : true,
      director: $('#ab-director') ? $('#ab-director').checked : false,
      diarization: $('#ab-diarize') ? $('#ab-diarize').checked : false,
    });
    const kfs = r.keyframes || [];
    if (!kfs.length) {
      setStatus($('#ab-status'), r.message || 'Nothing detected — try a different prompt or range', 'err');
      return;
    }
    // Keep manual keyframes OUTSIDE the predicted range; replace inside it.
    const keep = state.box.filter((k) => k.t < t0 - AB_EPS || k.t > t1 + AB_EPS);
    state.box = [...keep, ...kfs].sort((a, b) => a.t - b.t);
    const dnote = r.director_note ? ` · ${r.director_note}` : '';
    setStatus($('#ab-status'),
      `${r.message} Added ${kfs.length} keyframes — drag / resize / delete below.${dnote}`, 'ok');
    refreshKfUI();
  } catch (e) {
    setStatus($('#ab-status'), e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

$('#ab-generate').addEventListener('click', doAutoBox);
$('#ab-h-start').addEventListener('mousedown', (e) => startAbDrag(e, 'start'));
$('#ab-h-end').addEventListener('mousedown', (e) => startAbDrag(e, 'end'));
window.addEventListener('mousemove', onAbDragMove);
window.addEventListener('mouseup', () => { state.abDrag = null; });

// Disable the AI auto-box UI when the backend has no vision model configured,
// and the thumbnail "Generate ideas" button when there's no text model.
(async () => {
  try {
    const res = await fetch('/api/capabilities');
    if (!res.ok) return;
    const caps = await res.json();
    if (!caps.vision) {
      $('#ab-generate').disabled = true;
      $('#ab-prompt').disabled = true;
      setStatus($('#ab-status'), 'AI auto-box is off — no vision model configured (set VISION_BASE_URL / VISION_MODEL in .env).', 'err');
    }
    if (!caps.thumbnail) {
      const g = $('#btn-thumb-gen');
      if (g) g.disabled = true;
      setStatus($('#thumb-gen-status'), 'AI ideas are off — no text model configured. You can still type your own headline.', 'err');
    }
    if (!caps.diarize) {
      const d = $('#ab-diarize');
      if (d) { d.checked = false; d.disabled = true;
               d.closest('label').title = 'Diarization off — no pyannote/HF token. The director still decides the box visually.'; }
    }
    // GPU render farm → drives parallel per-sub-clip dispatch (0 boxes = local).
    const rr = caps.render_remote || {};
    state.renderBoxes = rr.enabled ? (rr.boxes || 0) : 0;
    refreshRenderSubs();
    const avail = caps.image_sources || { pexels: true };
    const labels = { pexels: 'Pexels', openverse: 'Openverse (CC)', wikimedia: 'Wikimedia Commons', unsplash: 'Unsplash', pixabay: 'Pixabay' };
    const sel = $('#ill-source');
    if (sel) {
      const single = Object.keys(labels).filter(k => avail[k]);
      // default = ALL sources mixed (one clip can pull from every source at once)
      sel.innerHTML = '<option value="all">All sources (mixed)</option>'
        + single.map(k => `<option value="${k}">${labels[k]}</option>`).join('');
      sel.value = 'all';
    }
  } catch (e) { /* best-effort */ }
})();

// ───────────────────────── thumbnail generator ─────────────────────────
// A dedicated 9:16 cover maker. Pick a frame on its own scrubber, generate an
// eye-catching headline (LLM) or type your own, then export a 1080×1920 PNG.
// Everything (frame capture, compositing, export) is client-side canvas — the
// only backend call is /api/thumbnail-text for the suggested wording.
const THUMB_OUT_W = 1080, THUMB_OUT_H = 1920;
const thumbVideo = $('#thumb-video');
const thumbCanvas = $('#thumb-canvas');
const thumb = {
  text: '', font: 'Anton', size: 130, color: '#ffffff', stroke: '#000000',
  pos: 'bottom', upper: true, shade: true, panX: 0.5, panY: 0.5,
  layout: 'full',                        // 'full' (1 box) | 'two' (top video crop + bottom illustration)
  effect: 'none',                        // finish over the whole thumbnail (crumple/grain/vignette)
  slots: [newThumbSlot(), newThumbSlot()],  // 0 = top/full ; 1 = bottom
};
// A background slot: a captured video frame ('scene', auto-falls-back to the live
// frame when none is captured) OR a picked stock image ('ill'). `fit` cover/blur.
function newThumbSlot() {
  return {
    kind: 'scene', fit: 'cover',
    snap: null, snapBox: null, snapTime: null,   // scene capture (off-screen canvas + source crop rect)
    illImg: null, illUrl: null, illThumb: null,  // picked stock image
  };
}

// Serializable thumbnail settings (for auto-save into editor_state). Canvas snaps
// aren't serializable, so scene slots keep only their capture time + crop rect
// (re-grabbed from the video on reopen); ill slots keep their image URL (reloaded).
function thumbSettings() {
  return {
    text: thumb.text, font: thumb.font, size: thumb.size, color: thumb.color,
    stroke: thumb.stroke, pos: thumb.pos, upper: thumb.upper, shade: thumb.shade,
    panX: thumb.panX, panY: thumb.panY, layout: thumb.layout, effect: thumb.effect,
    slots: (thumb.slots || []).map((s) => ({
      kind: s.kind, fit: s.fit, snapTime: s.snapTime, snapBox: s.snapBox,
      illUrl: s.illUrl, illThumb: s.illThumb,
    })),
  };
}
// Apply saved settings back onto the thumb state + the UI inputs, then redraw.
function applyThumbSettings(s) {
  if (!s) return;
  for (const k of ['text', 'font', 'size', 'color', 'stroke', 'pos', 'upper', 'shade', 'panX', 'panY', 'layout', 'effect']) {
    if (s[k] !== undefined && s[k] !== null) thumb[k] = s[k];
  }
  const set = (id, v) => { const e = $(id); if (e && v != null) e.value = v; };
  set('#thumb-text', thumb.text); set('#thumb-font', thumb.font); set('#thumb-size', thumb.size);
  set('#thumb-color', thumb.color); set('#thumb-stroke', thumb.stroke);
  set('#thumb-pos', thumb.pos); set('#thumb-effect', thumb.effect);
  const up = $('#thumb-upper'); if (up) up.checked = !!thumb.upper;
  const sh = $('#thumb-shade'); if (sh) sh.checked = !!thumb.shade;
  const pn = $('#thumb-pan'); if (pn) pn.value = Math.round((thumb.panX == null ? 0.5 : thumb.panX) * 100);
  const py = $('#thumb-pany'); if (py) py.value = Math.round((thumb.panY == null ? 0.5 : thumb.panY) * 100);
  if (Array.isArray(s.slots) && s.slots.length) {
    thumb.slots = s.slots.map((x) => ({
      kind: x.kind || 'scene', fit: x.fit || 'cover',
      snap: null, snapBox: x.snapBox || null, snapTime: (x.snapTime != null ? x.snapTime : null),
      illImg: null, illUrl: x.illUrl || null, illThumb: x.illThumb || null,
    }));
    thumb.slots.forEach((sl) => {
      if (sl.kind === 'ill' && sl.illUrl) {
        const img = new Image(); img.onload = drawThumb;
        img.src = sl.illUrl.startsWith('/temp/') ? sl.illUrl : '/api/img?url=' + encodeURIComponent(sl.illUrl);
        sl.illImg = img;
      }
    });
  }
  if (typeof renderThumbLayoutToggle === 'function') renderThumbLayoutToggle();
  if (typeof renderThumbSlots === 'function') renderThumbSlots();
  if (typeof restoreSceneCaptures === 'function') restoreSceneCaptures();
  drawThumb();
}

function thumbEscape(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function initThumbStep() {
  if (!state.jobId || !thumbVideo) return;
  const path = video.getAttribute('src');
  if (path && thumbVideo.dataset.path !== path) {
    thumbVideo.dataset.path = path;
    thumbVideo.src = path;
  }
  const ta = $('#thumb-text');
  if (ta && !ta.value) {
    const t = ($('#f-title').value || '').trim();
    if (t && t.toLowerCase() !== 'clip') { ta.value = t; thumb.text = t; }
  }
  populateThumbSubclips();
  renderThumbLayoutToggle();
  renderThumbSlots();
  drawThumb();
}
// Per-sub-clip thumbnail: pick a sub-clip → the time bar jumps into its range and
// the exported PNG is named after it (make one cover per sub-clip).
function populateThumbSubclips() {
  const wrap = $('#thumb-subpick'), sel = $('#thumb-subclip');
  if (!wrap || !sel) return;
  wrap.hidden = state.subclips.length === 0;
  if (!state.subclips.length) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Whole clip</option>' +
    state.subclips.map((sc, i) => `<option value="${i}">${(sc.name || ('sub ' + (i + 1))).replace(/</g, '')} (${sc.start.toFixed(0)}–${sc.end.toFixed(0)}s)</option>`).join('');
  sel.value = prev && +prev < state.subclips.length ? prev : '';
}

function drawThumb() {
  if (!thumbCanvas) return;
  drawThumbnailInto(thumbCanvas.getContext('2d'), thumbCanvas.width, thumbCanvas.height);
}

// Parametric so the same code renders the preview AND the 1080×1920 export.
function drawThumbnailInto(ctx, W, H) {
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W, H);
  thumbSlotRects(W, H).forEach((r, i) => drawThumbSlot(ctx, thumb.slots[i], r[0], r[1], r[2], r[3], i));
  if (thumb.shade) drawThumbShade(ctx, W, H);
  drawThumbText(ctx, W, H);
  applyThumbEffect(ctx, W, H);
}

// 'full' = one background; 'two' = top video crop (matches the render's top split)
// + bottom illustration. The split tracks the clip's top_eighths like the renderer.
function thumbSlotRects(W, H) {
  if (thumb.layout === 'full') return [[0, 0, W, H]];
  const top = H * topFrac();
  return [[0, 0, W, top], [0, top, W, H - top]];
}

// Draw one slot's background. A 'scene' slot uses its captured frame; if nothing
// was captured it falls back to the LIVE video frame (so the default flow — just
// scrub — still works). In 'two' mode the TOP slot crops to the crop box (so the
// cover preview matches the rendered top), the BOTTOM slot uses the whole frame.
function drawThumbSlot(ctx, slot, dx, dy, dw, dh, idx) {
  let src = null, sx = 0, sy = 0, sw = 0, sh = 0, live = false;
  if (slot.kind === 'ill' && slot.illImg && slot.illImg.complete && slot.illImg.naturalWidth) {
    src = slot.illImg; sw = src.naturalWidth; sh = src.naturalHeight;
  } else if (slot.kind === 'scene' && slot.snap) {
    src = slot.snap;
    if (slot.snapBox) { sx = slot.snapBox.x; sy = slot.snapBox.y; sw = slot.snapBox.w; sh = slot.snapBox.h; }
    else { sw = src.width; sh = src.height; }
  } else if (slot.kind === 'scene' && thumbVideo && thumbVideo.videoWidth) {
    src = thumbVideo; sw = thumbVideo.videoWidth; sh = thumbVideo.videoHeight; live = true;
    if (idx === 0 && thumb.layout === 'two') {
      const b = boxAt(thumbVideo.currentTime || 0);
      if (b && !b.gap) { sx = b.x; sy = b.y; sw = b.w; sh = b.h; }
    }
  }
  if (!src) {  // placeholder
    ctx.save();
    ctx.fillStyle = '#15151a'; ctx.fillRect(dx, dy, dw, dh);
    ctx.fillStyle = '#555'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = `${Math.max(9, dw * 0.045)}px JetBrains Mono, monospace`;
    ctx.fillText(slot.kind === 'ill' ? 'pick an image' : 'capture a frame', dx + dw / 2, dy + dh / 2);
    ctx.restore();
    return;
  }
  // Only the whole-frame live scene honours the Focus sliders; cropped/captured
  // /illustration sources are centred (the crop box / image already frames them).
  const panned = live && !(idx === 0 && thumb.layout === 'two');
  const px = panned ? thumb.panX : 0.5, py = panned ? thumb.panY : 0.5;
  ctx.save();
  ctx.beginPath(); ctx.rect(dx, dy, dw, dh); ctx.clip();
  if (slot.fit === 'blur') {
    ctx.filter = 'blur(10px) brightness(0.9)';
    coverRegion(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh, px, py);
    ctx.filter = 'none';
    containRegion(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh);
  } else {
    coverRegion(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh, px, py);
  }
  ctx.restore();
}

// cover / contain a SOURCE REGION (sx,sy,sw,sh) of `src` into a dest rect.
// px/py (0–1, default centre) bias which part of the source survives the crop.
function coverRegion(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh, px, py) {
  px = px == null ? 0.5 : px; py = py == null ? 0.5 : py;
  const rAR = sw / sh, dAR = dw / dh;
  let cw, ch;
  if (rAR > dAR) { ch = sh; cw = sh * dAR; } else { cw = sw; ch = sw / dAR; }
  const cx = sx + (sw - cw) * px, cy = sy + (sh - ch) * py;
  try { ctx.drawImage(src, cx, cy, cw, ch, dx, dy, dw, dh); } catch (_) {}
}
function containRegion(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh) {
  const rAR = sw / sh, dAR = dw / dh;
  let ow, oh;
  if (rAR > dAR) { ow = dw; oh = dw / rAR; } else { oh = dh; ow = dh * rAR; }
  try { ctx.drawImage(src, sx, sy, sw, sh, dx + (dw - ow) / 2, dy + (dh - oh) / 2, ow, oh); } catch (_) {}
}

// ── thumbnail finish effects (crumpled paper / grain / vignette) ──
// Procedural, deterministic (seeded RNG) and cached per (effect, size) so the
// preview doesn't shimmer on every redraw. Drawn OVER everything.
const _fxCache = {};

function _seededRand(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

function _crumpleLayer(W, H) {
  const key = `crumple_${W}x${H}`;
  if (_fxCache[key]) return _fxCache[key];
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const x = c.getContext('2d');
  const rnd = _seededRand(1306619051);
  x.fillStyle = 'rgb(128,128,128)';            // neutral for 'overlay' blending
  x.fillRect(0, 0, W, H);
  for (let i = 0; i < 220; i++) {              // faceted shading
    const cx = rnd() * W, cy = rnd() * H, r = (0.04 + rnd() * 0.16) * Math.max(W, H);
    const a0 = rnd() * Math.PI * 2, a1 = a0 + 0.6 + rnd() * 1.6, a2 = a1 + 0.6 + rnd() * 1.6;
    const tone = 128 + (rnd() - 0.5) * 56;
    x.fillStyle = `rgba(${tone | 0},${tone | 0},${tone | 0},0.5)`;
    x.beginPath();
    x.moveTo(cx + Math.cos(a0) * r, cy + Math.sin(a0) * r);
    x.lineTo(cx + Math.cos(a1) * r, cy + Math.sin(a1) * r);
    x.lineTo(cx + Math.cos(a2) * r, cy + Math.sin(a2) * r);
    x.closePath(); x.fill();
  }
  for (let i = 0; i < 26; i++) {               // sharp creases
    const x0 = rnd() * W, y0 = rnd() * H;
    const ang = rnd() * Math.PI * 2, len = (0.3 + rnd() * 0.7) * Math.max(W, H);
    const x1 = x0 + Math.cos(ang) * len, y1 = y0 + Math.sin(ang) * len;
    const nx = -Math.sin(ang), ny = Math.cos(ang);
    const lw = Math.max(1, W / 360);
    x.lineWidth = lw;
    x.strokeStyle = 'rgba(235,235,235,0.55)';
    x.beginPath(); x.moveTo(x0, y0); x.lineTo(x1, y1); x.stroke();
    x.strokeStyle = 'rgba(30,30,30,0.45)';
    x.beginPath(); x.moveTo(x0 + nx * lw, y0 + ny * lw); x.lineTo(x1 + nx * lw, y1 + ny * lw); x.stroke();
  }
  const soft = document.createElement('canvas');
  soft.width = W; soft.height = H;
  const sx = soft.getContext('2d');
  sx.filter = `blur(${Math.max(1, W / 540)}px)`;
  sx.drawImage(c, 0, 0);
  _fxCache[key] = soft;
  return soft;
}

function _grainLayer(W, H) {
  const key = `grain_${W}x${H}`;
  if (_fxCache[key]) return _fxCache[key];
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const x = c.getContext('2d');
  const img = x.createImageData(W, H);
  const rnd = _seededRand(20260611);
  for (let i = 0; i < img.data.length; i += 4) {
    const v = 96 + rnd() * 64;
    img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
    img.data[i + 3] = 255;
  }
  x.putImageData(img, 0, 0);
  _fxCache[key] = c;
  return c;
}

function applyThumbEffect(ctx, W, H) {
  const fx = thumb.effect || 'none';
  if (fx === 'none') return;
  if (fx.includes('crumple')) {
    ctx.save();
    ctx.globalCompositeOperation = 'overlay';
    ctx.globalAlpha = 0.85;
    ctx.drawImage(_crumpleLayer(W, H), 0, 0);
    ctx.restore();
  }
  if (fx.includes('grain')) {
    ctx.save();
    ctx.globalCompositeOperation = 'overlay';
    ctx.globalAlpha = 0.5;
    ctx.drawImage(_grainLayer(W, H), 0, 0);
    ctx.restore();
  }
  if (fx.includes('vignette')) {
    ctx.save();
    const g = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.45,
                                       W / 2, H / 2, Math.hypot(W, H) / 2);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(1, 'rgba(0,0,0,0.55)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }
}

// ── background slot UI (Scene capture / stock illustration, per slot) ──
function thumbSlotCount() { return thumb.layout === 'full' ? 1 : 2; }
function thumbSlotLabel(i) {
  if (thumb.layout === 'full') return 'Background';
  return i === 0 ? 'Top (video crop)' : 'Bottom (illustration)';
}
function renderThumbLayoutToggle() {
  document.querySelectorAll('#thumb-layout-toggle .seg')
    .forEach((b) => b.classList.toggle('active', b.dataset.layout === thumb.layout));
}

function renderThumbSlots() {
  const box = $('#thumb-slots');
  if (!box) return;
  let html = '';
  for (let i = 0; i < thumbSlotCount(); i++) {
    const s = thumb.slots[i];
    const stat = s.kind === 'scene'
      ? (s.snap ? `captured @ ${(s.snapTime || 0).toFixed(1)}s ✓` : 'using live frame')
      : (s.illThumb ? 'image picked ✓' : 'no image yet');
    html += `<div class="thumb-slot" data-i="${i}">
      <div class="thumb-slot-head">${thumbSlotLabel(i)}</div>
      <div class="thumb-row">
        <div class="thumb-seg thumb-src">
          <button class="seg ${s.kind === 'scene' ? 'active' : ''}" data-src="scene" data-i="${i}">Scene</button>
          <button class="seg ${s.kind === 'ill' ? 'active' : ''}" data-src="ill" data-i="${i}">Illustration</button>
        </div>
        <select class="thumb-fit" data-i="${i}" title="fit"><option value="cover"${s.fit === 'cover' ? ' selected' : ''}>Cover</option><option value="blur"${s.fit === 'blur' ? ' selected' : ''}>Blur</option></select>
      </div>
      ${s.kind === 'scene' ? `
        <div class="thumb-scene">
          <button class="thumb-capture" data-i="${i}">📸 Capture current frame</button>
          <span class="status thumb-snap ${s.snap ? 'ok' : ''}">${stat}</span>
        </div>` : `
        <div class="thumb-ill">
          <div class="thumb-ill-search"><input class="thumb-ill-q" data-i="${i}" placeholder="search images"><button class="thumb-ill-go" data-i="${i}">Search</button></div>
          <div class="thumb-ill-cands" data-i="${i}"></div>
          <span class="status thumb-ill-status ${s.illThumb ? 'ok' : ''}" data-i="${i}">${stat}</span>
        </div>`}
    </div>`;
  }
  box.innerHTML = html;
  box.querySelectorAll('.thumb-src .seg').forEach((b) => b.addEventListener('click', () => {
    thumb.slots[+b.dataset.i].kind = b.dataset.src; renderThumbSlots(); drawThumb();
  }));
  box.querySelectorAll('.thumb-fit').forEach((s) => s.addEventListener('change', (e) => {
    thumb.slots[+e.target.dataset.i].fit = e.target.value; drawThumb();
  }));
  box.querySelectorAll('.thumb-capture').forEach((b) => b.addEventListener('click', () => captureSlot(+b.dataset.i)));
  box.querySelectorAll('.thumb-ill-go').forEach((b) => b.addEventListener('click', () => {
    const i = +b.dataset.i;
    thumbIllSearch(i, box.querySelector(`.thumb-ill-q[data-i="${i}"]`).value.trim());
  }));
  box.querySelectorAll('.thumb-ill-q').forEach((inp) => inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') thumbIllSearch(+e.target.dataset.i, e.target.value.trim());
  }));
}

// Snapshot the current video frame for a Scene slot. In 'two' mode the TOP slot
// records the crop box at that time (so the cover crop matches the render).
function captureSlot(i) {
  const v = thumbVideo;
  if (!v || !v.videoWidth) { setStatus($('#thumb-status'), 'Video not ready yet.', 'err'); return; }
  const off = document.createElement('canvas');
  off.width = v.videoWidth; off.height = v.videoHeight;
  off.getContext('2d').drawImage(v, 0, 0);
  const s = thumb.slots[i];
  s.snap = off; s.snapTime = v.currentTime || 0;
  if (i === 0 && thumb.layout === 'two') {
    const b = boxAt(s.snapTime);
    s.snapBox = (b && !b.gap) ? { x: b.x, y: b.y, w: b.w, h: b.h } : null;
  } else {
    s.snapBox = null;
  }
  renderThumbSlots(); drawThumb();
}

async function thumbIllSearch(i, q) {
  const box = $('#thumb-slots');
  const cont = box && box.querySelector(`.thumb-ill-cands[data-i="${i}"]`);
  const st = box && box.querySelector(`.thumb-ill-status[data-i="${i}"]`);
  if (!q) { if (st) { st.textContent = 'Type a query.'; st.className = 'status thumb-ill-status err'; } return; }
  if (st) { st.textContent = 'Searching…'; st.className = 'status thumb-ill-status'; }
  try {
    const source = ($('#ill-source') && $('#ill-source').value) || 'all';
    const res = await api('search', { query: q, source });
    const cands = res.candidates || [];
    if (cont) {
      cont.innerHTML = cands.map((c) => `<button class="thumb-cand" data-url="${thumbEscape(c.full)}" data-thumb="${thumbEscape(c.thumb)}"><img src="${thumbEscape(c.thumb)}" loading="lazy" alt=""></button>`).join('');
      cont.querySelectorAll('.thumb-cand').forEach((b) => b.addEventListener('click', () => pickThumbIll(i, b.dataset.url, b.dataset.thumb)));
    }
    if (st) { st.textContent = cands.length ? 'Click an image.' : 'No results.'; st.className = 'status thumb-ill-status ' + (cands.length ? 'ok' : 'err'); }
  } catch (e) {
    if (st) { st.textContent = 'Failed: ' + e.message; st.className = 'status thumb-ill-status err'; }
  }
}

function pickThumbIll(i, fullUrl, thumbUrl) {
  const s = thumb.slots[i];
  const img = new Image();
  img.onload = drawThumb;
  // /temp/ uploads are same-origin already; remote URLs go via the proxy so the
  // canvas isn't tainted (export would otherwise throw).
  img.src = fullUrl.startsWith('/temp/') ? fullUrl : '/api/img?url=' + encodeURIComponent(fullUrl);
  s.illImg = img; s.illUrl = fullUrl; s.illThumb = thumbUrl;
  renderThumbSlots(); drawThumb();
}

function _seekThumb(v, t) {
  return new Promise((res) => {
    const h = () => { v.removeEventListener('seeked', h); res(); };
    v.addEventListener('seeked', h);
    try { v.currentTime = t; } catch (_) { res(); }
    setTimeout(res, 2000);
  });
}

// Re-grab scene captures from the thumb video at their saved time (runs once the
// video is ready). No-op when no scene slot has a pending snapTime.
async function restoreSceneCaptures() {
  const v = thumbVideo;
  if (!v) return;
  const pend = thumb.slots.filter((s) => s.kind === 'scene' && s.snapTime != null && !s.snap);
  if (!pend.length || !v.videoWidth) return;
  const origT = v.currentTime || 0;
  for (const s of pend) {
    await _seekThumb(v, s.snapTime);
    if (!v.videoWidth) break;
    const off = document.createElement('canvas');
    off.width = v.videoWidth; off.height = v.videoHeight;
    off.getContext('2d').drawImage(v, 0, 0);
    s.snap = off;
  }
  try { v.currentTime = origT; } catch (_) {}
  renderThumbSlots(); drawThumb();
}

function drawThumbShade(ctx, W, H) {
  ctx.save();
  let g;
  if (thumb.pos === 'top') {
    g = ctx.createLinearGradient(0, 0, 0, H * 0.5);
    g.addColorStop(0, 'rgba(0,0,0,0.7)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H * 0.5);
  } else if (thumb.pos === 'bottom') {
    g = ctx.createLinearGradient(0, H, 0, H * 0.5);
    g.addColorStop(0, 'rgba(0,0,0,0.7)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, H * 0.5, W, H * 0.5);
  } else {
    g = ctx.createLinearGradient(0, H * 0.28, 0, H * 0.72);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(0.5, 'rgba(0,0,0,0.6)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, H * 0.28, W, H * 0.44);
  }
  ctx.restore();
}

function wrapThumbLines(ctx, text, maxW) {
  const out = [];
  for (const para of text.split('\n')) {
    const words = para.split(/\s+/).filter(Boolean);
    if (!words.length) continue;
    let cur = words[0];
    for (let i = 1; i < words.length; i++) {
      const test = cur + ' ' + words[i];
      if (ctx.measureText(test).width > maxW) { out.push(cur); cur = words[i]; }
      else cur = test;
    }
    out.push(cur);
  }
  return out;
}

function drawThumbText(ctx, W, H) {
  let text = (thumb.text || '').trim();
  if (!text) return;
  if (thumb.upper) text = text.toUpperCase();
  const scale = W / THUMB_OUT_W;
  const fontPx = Math.max(8, thumb.size * scale);
  const lineH = fontPx * 1.08;
  const maxW = W * 0.9;

  ctx.save();
  ctx.font = `bold ${fontPx}px "${thumb.font}", Impact, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineJoin = 'round';

  const lines = wrapThumbLines(ctx, text, maxW);
  const blockH = lines.length * lineH;
  const margin = H * 0.06;
  let cy;
  if (thumb.pos === 'top') cy = margin + lineH / 2;
  else if (thumb.pos === 'bottom') cy = H - margin - blockH + lineH / 2;
  else cy = H / 2 - blockH / 2 + lineH / 2;

  ctx.lineWidth = Math.max(2, fontPx * 0.14);
  ctx.strokeStyle = thumb.stroke;
  ctx.fillStyle = thumb.color;
  for (const line of lines) {
    ctx.strokeText(line, W / 2, cy);
    ctx.fillText(line, W / 2, cy);
    cy += lineH;
  }
  ctx.restore();
}

async function doThumbGen() {
  const btn = $('#btn-thumb-gen');
  const context = [
    ($('#f-title').value || '').trim(),
    ($('#f-desc').value || '').trim(),
    (state.words || []).map((w) => w.word).join(' ').trim(),
  ].filter(Boolean).join('\n');
  if (btn) btn.disabled = true;
  setStatus($('#thumb-gen-status'), 'Generating headline ideas… (first call may warm the model)');
  try {
    const tone = $('#thumb-tone') ? $('#thumb-tone').value : '';
    const res = await api('thumbnail-text', { context, n: 6, tone });
    const titles = res.titles || [];
    renderThumbIdeas(titles);
    setStatus($('#thumb-gen-status'),
      titles.length ? `${titles.length} ideas — click one to use it, then tweak` : 'No ideas returned',
      titles.length ? 'ok' : 'err');
  } catch (e) {
    setStatus($('#thumb-gen-status'), 'Failed: ' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderThumbIdeas(titles) {
  const box = $('#thumb-ideas');
  if (!box) return;
  box.innerHTML = titles
    .map((t) => `<button class="thumb-idea" type="button">${thumbEscape(t)}</button>`)
    .join('');
  box.querySelectorAll('.thumb-idea').forEach((b) => b.addEventListener('click', () => {
    const ta = $('#thumb-text');
    if (ta) ta.value = b.textContent;
    thumb.text = b.textContent;
    drawThumb();
  }));
}

async function downloadThumb() {
  try { await document.fonts.load(`bold ${thumb.size}px "${thumb.font}"`); } catch (_) {}
  const off = document.createElement('canvas');
  off.width = THUMB_OUT_W; off.height = THUMB_OUT_H;
  drawThumbnailInto(off.getContext('2d'), THUMB_OUT_W, THUMB_OUT_H);
  off.toBlob((blob) => {
    if (!blob) { setStatus($('#thumb-status'), 'Export failed', 'err'); return; }
    const a = document.createElement('a');
    const sel = $('#thumb-subclip');
    const sc = (sel && sel.value !== '' && state.subclips[+sel.value]) ? state.subclips[+sel.value] : null;
    const base = sc ? sc.name : (($('#f-title').value || 'clip').trim() || 'clip');
    const title = base.replace(/[^\w.-]+/g, '_');
    a.href = URL.createObjectURL(blob);
    a.download = `${title}_thumbnail.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    setStatus($('#thumb-status'), `Saved ${a.download}`, 'ok');
  }, 'image/png');
}

$('#thumb-subclip') && $('#thumb-subclip').addEventListener('change', (e) => {
  const sc = e.target.value !== '' ? state.subclips[+e.target.value] : null;
  if (sc && thumbVideo && thumbVideo.duration) {
    thumbVideo.currentTime = Math.min(sc.start + 0.1, thumbVideo.duration - 0.05);
  }
});
if (thumbVideo && thumbCanvas) {
  thumbVideo.addEventListener('loadedmetadata', () => {
    const dur = thumbVideo.duration || 0;
    const scr = $('#thumb-scrubber');
    if (scr) scr.max = dur;
    const d = $('#thumb-dur'); if (d) d.textContent = fmtTime(dur);
    drawThumb();
  });
  thumbVideo.addEventListener('loadeddata', () => { drawThumb(); restoreSceneCaptures(); });
  thumbVideo.addEventListener('seeked', drawThumb);
  thumbVideo.addEventListener('timeupdate', () => {
    const t = $('#thumb-time'); if (t) t.textContent = fmtTime(thumbVideo.currentTime || 0);
  });
  $('#thumb-scrubber').addEventListener('input', (e) => {
    if (!thumbVideo.duration) return;
    thumbVideo.currentTime = +e.target.value;
    const t = $('#thumb-time'); if (t) t.textContent = fmtTime(+e.target.value);
  });
  $('#thumb-text').addEventListener('input', (e) => { thumb.text = e.target.value; drawThumb(); });
  $('#thumb-font').addEventListener('change', (e) => {
    thumb.font = e.target.value;
    document.fonts.load(`bold 120px "${thumb.font}"`).then(drawThumb).catch(drawThumb);
  });
  $('#thumb-size').addEventListener('input', (e) => { thumb.size = +e.target.value || 130; drawThumb(); });
  $('#thumb-color').addEventListener('input', (e) => { thumb.color = e.target.value; drawThumb(); });
  $('#thumb-stroke').addEventListener('input', (e) => { thumb.stroke = e.target.value; drawThumb(); });
  $('#thumb-pos').addEventListener('change', (e) => { thumb.pos = e.target.value; drawThumb(); });
  $('#thumb-upper').addEventListener('change', (e) => { thumb.upper = e.target.checked; drawThumb(); });
  $('#thumb-shade').addEventListener('change', (e) => { thumb.shade = e.target.checked; drawThumb(); });
  $('#thumb-pan').addEventListener('input', (e) => { thumb.panX = (+e.target.value) / 100; drawThumb(); });
  $('#thumb-pany').addEventListener('input', (e) => { thumb.panY = (+e.target.value) / 100; drawThumb(); });
  const teff = $('#thumb-effect');
  if (teff) teff.addEventListener('change', (e) => { thumb.effect = e.target.value; drawThumb(); });
  // background layout toggle (Full / 2 boxes)
  document.querySelectorAll('#thumb-layout-toggle .seg').forEach((b) => b.addEventListener('click', () => {
    thumb.layout = b.dataset.layout;
    renderThumbLayoutToggle(); renderThumbSlots(); drawThumb();
  }));
  $('#btn-thumb-gen').addEventListener('click', doThumbGen);
  $('#btn-thumb-dl').addEventListener('click', downloadThumb);
}

// ───────────────────────── batch queue (sidebar) ─────────────────────────
// Upload a JSON of clips → the backend worker downloads + auto-boxes each one in
// the background (persisted across restarts). Open a ready job to fine-tune the
// single crop box (auto-saved back), delete when done.
function qStatusBadge(s) {
  const label = { pending: 'queued', downloading: 'downloading', downloaded: 'boxing queued', predicting: 'boxing', ready: 'ready', error: 'error' }[s] || s;
  return `<span class="q-badge ${s}">${label}</span>`;
}

async function refreshQueue() {
  try {
    const r = await fetch('/api/queue');
    if (!r.ok) return;
    const data = await r.json();
    renderQueueList(data.jobs || [], data.box_eta);
  } catch (e) { /* best-effort */ }
}

function fmtEta(s) {
  if (s == null || !isFinite(s) || s <= 0) return '';
  const m = Math.round(s / 60);
  if (m < 1) return '<1 min left';
  if (m < 60) return `~${m} min left`;
  return `~${(m / 60).toFixed(1)} h left`;
}

function renderQueueList(jobs, boxEta) {
  const ul = $('#queue-list'); const meta = $('#queue-meta'); const prog = $('#queue-progress');
  if (!ul) return;
  if (!jobs.length) {
    ul.innerHTML = ''; if (meta) meta.textContent = 'No jobs queued.';
    if (prog) prog.classList.add('hidden'); return;
  }
  const c = jobs.reduce((a, j) => { a[j.status] = (a[j.status] || 0) + 1; return a; }, {});
  const working = (c.pending || 0) + (c.downloading || 0) + (c.downloaded || 0) + (c.predicting || 0);
  // Room filter: a selected room shows only its clips; "All rooms" shows all
  // (with a per-clip room chip). Counts/progress stay GLOBAL.
  const rsel = $('#room-select');
  const roomFilter = (rsel && rsel.value) ? +rsel.value : null;
  const roomName = {};
  (state.rooms || []).forEach((rm) => { roomName[rm.id] = rm.name; });
  const shown = roomFilter !== null ? jobs.filter((j) => j.room_id === roomFilter) : jobs;
  if (meta) meta.textContent = roomFilter !== null
    ? `${shown.length} clip(s) in this room · ${jobs.length} total`
    : `${jobs.length} job(s) · ${c.ready || 0} ready · ${working} working${c.error ? ` · ${c.error} error` : ''}`;
  const stopBtn = $('#btn-queue-stop-box');
  if (stopBtn) stopBtn.classList.toggle('hidden', working === 0);
  if (prog) {
    prog.classList.remove('hidden');
    const settled = (c.ready || 0) + (c.error || 0);
    const pct = Math.round((settled / jobs.length) * 100);
    const fill = $('#queue-progress-fill'); const txt = $('#queue-progress-txt');
    if (fill) fill.style.width = pct + '%';
    if (txt) {
      const phase = c.downloading ? 'downloading' : (c.predicting ? 'boxing' : null);
      const eta = (c.predicting || c.downloaded) ? fmtEta(boxEta) : '';
      txt.textContent = `${settled}/${jobs.length} boxed · ${pct}%` + (phase ? ` · ${phase}…` : '') + (eta ? ` · ${eta}` : '');
    }
  }
  if (!shown.length) {
    ul.innerHTML = '<li class="queue-empty-room">No clips in this room yet — import into it.</li>';
    return;
  }
  ul.innerHTML = shown.map((j) => {
    const active = j.key === state.activeQueueKey ? ' active' : '';
    const canOpen = j.status === 'ready';
    const kf = canOpen ? ` · ${j.kf1} kf` : '';
    const roomChip = (roomFilter === null && j.room_id && roomName[j.room_id])
      ? `<span class="q-room">${thumbEscape(roomName[j.room_id])}</span>` : '';
    const retry = j.status === 'error' ? `<button class="q-retry" data-key="${j.key}" title="retry this job">↻</button>` : '';
    // A job waiting in the boxing queue can be pulled out: ready NOW, no box,
    // the user draws it manually instead of waiting for the AI stage.
    const skip = j.status === 'downloaded'
      ? `<button class="q-skip" data-key="${j.key}" title="Skip AI boxing — open this clip now and draw the crop manually">✎ manual</button>`
      : '';
    // A ready clip with NO box (boxing skipped/stopped, or nothing detected) can be
    // put back through AI boxing — recovers an accidental "Stop boxing".
    const rebox = (j.status === 'ready' && !j.kf1)
      ? `<button class="q-retry" data-key="${j.key}" title="Re-box: re-run AI boxing on this clip">↻ box</button>`
      : '';
    return `<li class="queue-item${active}" data-status="${j.status}">
      <button class="queue-open" data-key="${j.key}" ${canOpen ? '' : 'disabled'} title="${thumbEscape(j.message || '')}">
        <span class="q-id">${thumbEscape(j.id)}</span>
        <span class="q-title">${thumbEscape(j.title || '')}</span>
        <span class="q-sub">${qStatusBadge(j.status)}${kf}${roomChip}</span>
      </button>
      ${rebox}${skip}${retry}
      <button class="q-del" data-key="${j.key}" title="delete job + its downloaded clip">×</button>
    </li>`;
  }).join('');
  ul.querySelectorAll('.queue-open').forEach((b) => b.addEventListener('click', () => openQueueJob(b.dataset.key)));
  ul.querySelectorAll('.q-del').forEach((b) => b.addEventListener('click', () => deleteQueueJob(b.dataset.key)));
  ul.querySelectorAll('.q-retry').forEach((b) => b.addEventListener('click', () => retryQueueJob(b.dataset.key)));
  ul.querySelectorAll('.q-skip').forEach((b) => b.addEventListener('click', () => skipBoxQueueJob(b.dataset.key)));
}

async function skipBoxQueueJob(key) {
  try {
    await api(`queue/${key}/skip-box`, {});
    await openQueueJob(key);   // ready now — open it straight into the editor
    setStatus($('#queue-status'), 'Boxing skipped — draw the crop manually.', 'ok');
  } catch (e) {
    setStatus($('#queue-status'), 'Skip failed: ' + e.message, 'err');
    refreshQueue();
  }
}

async function openQueueJob(key) {
  await autosaveQueue();
  let job;
  try {
    const r = await fetch('/api/queue/' + key);
    if (!r.ok) throw new Error(await r.text());
    job = await r.json();
  } catch (e) { setStatus($('#queue-status'), 'Could not open job: ' + e.message, 'err'); return; }
  if (!job.job_id) { setStatus($('#queue-status'), 'Still processing — open it once it says "ready".', 'err'); return; }
  state.activeQueueKey = key;
  state.jobId = job.job_id;
  state.duration = job.duration;
  state.srcW = job.width; state.srcH = job.height;
  state.box = job.box1 || [];
  // Load the cached transcript (Whisper'd during boxing) so the Illustration step
  // generates a REAL per-segment query from what's actually said — not the title.
  state.words = (() => { try { return job.transcript ? JSON.parse(job.transcript) : []; } catch (_) { return []; } })();
  state.sfx = [];                  // placements are per-clip
  state.keep = []; state.keepA = null;            // trim windows are per-clip
  state.capPos = []; state.capPosA = null;        // caption-position windows are per-clip
  state.overlays = []; state.ovlSel = null;       // text overlays are per-clip
  state.stickers = []; state.stkSel = null;       // stickers are per-clip
  _subRenderRes = {};                             // per-sub-clip render outcomes
  state.subclips = []; state.activeSub = null;    // sub-clips are per-session
    state.subA = null; state.subB = null; state.subTrimA = null;
  state.currentTime = 0;
  state.autoRange = { start: 0, end: job.duration };
  $('#f-url').value = job.url || '';
  $('#f-title').value = job.title || 'clip';
  $('#f-start').value = job.start || '00:00:00';
  $('#f-end').value = job.end || '';
  $('#f-desc').value = job.description || '';
  // Context (with {layout}/{side} placeholders) goes in its own editable field;
  // the box prompt holds only the short instruction. doAutoBox re-merges
  // [observation] + [context] + [prompt] at Generate time — like the batch worker.
  state.autoObs = (job.auto_context || '').trim();
  if ($('#ab-context')) $('#ab-context').value = job.context || '';
  if ($('#ab-prompt')) $('#ab-prompt').value = job.prompt1 || '';
  // Pre-fill the Illustration step's segment length if the JSON specified it,
  // so the user just picks images (render stays manual for illustrator).
  if (job.segment_seconds && $('#seg-seconds')) {
    $('#seg-seconds').value = job.segment_seconds;
    state.segSeconds = job.segment_seconds;
  }
  // Pre-fill the per-clip top/bottom split (3/8 default). Snaps + redraws.
  applyTopEighths(job.top_eighths || 3);
  video.src = job.video_path;
  $('#range-meta').textContent = `source: ${job.width}×${job.height} · ${(job.duration || 0).toFixed(1)}s`;
  // RESTORE the full auto-saved editor state (sub-clips + buffer: crop/illustrations/
  // text/stickers/trim/caption) so refresh / server restart loses nothing.
  try {
    const ed = job.editor_state ? JSON.parse(job.editor_state) : null;
    if (ed && (ed.subclips || ed.buf)) {
      state.subclips = ed.subclips || [];
      state.activeSub = (ed.activeSub != null && state.subclips[ed.activeSub]) ? ed.activeSub : null;
      if (ed.buf) subApply(ed.buf);
      renderSubclips();
    }
    if (ed && ed.thumb) applyThumbSettings(ed.thumb);   // restore the Thumbnail step
  } catch (e) { /* ignore a bad blob */ }
  state.queueSig = queueSig();
  histReset();                     // start edit history fresh for this job
  setStatus($('#queue-status'), `Editing "${job.id}" — everything auto-saves (sub-clips, crop, illustrations, text, stickers, trim).`, 'ok');
  showStep(2);
  refreshQueue();
}

function queueSig() {
  return JSON.stringify({
    t: ($('#f-title').value || ''),
    b: state.box,
    ctx: ($('#ab-context') ? $('#ab-context').value : ''),
    p1: ($('#ab-prompt') ? $('#ab-prompt').value : ''),
    te: state.topEighths,
    // everything else (every page) so a change anywhere triggers a save
    sub: state.subclips, act: state.activeSub, buf: subSnapshot(),
    thumb: thumbSettings(),   // headline + style + slots (Thumbnail step)
  });
}

async function autosaveQueue() {
  if (!state.activeQueueKey) return;
  if (state.cutDrag || state.subDrag || state.keepDrag || state.ovlDrag || state.stkDrag || state.capPosDrag || state.subTrimDrag) return;   // not mid-drag
  syncActiveSub();             // fold the live buffer into the active sub-clip first
  const sig = queueSig();
  if (sig === state.queueSig) return;
  state.queueSig = sig;
  try {
    await api(`queue/${state.activeQueueKey}/save`, {
      title: ($('#f-title').value || 'clip').trim() || 'clip',
      box1: state.box || [],
      context: $('#ab-context') ? $('#ab-context').value : '',
      prompt1: $('#ab-prompt') ? $('#ab-prompt').value : '',
      top_eighths: state.topEighths || 3,
      // EVERYTHING (every page): sub-clips + the full editor buffer + thumbnail
      editor_state: JSON.stringify({ subclips: state.subclips, activeSub: state.activeSub, buf: subSnapshot(), thumb: thumbSettings() }),
    });
    setStatus($('#queue-status'), 'Progress saved ✓', 'ok');
  } catch (e) { /* retry next tick */ }
}

async function deleteQueueJob(key) {
  if (!confirm('Delete this job from the queue? Its downloaded clip is removed too.')) return;
  try {
    await fetch('/api/queue/' + key, { method: 'DELETE' });
    if (state.activeQueueKey === key) state.activeQueueKey = null;
    refreshQueue();
  } catch (e) { setStatus($('#queue-status'), 'Delete failed: ' + e.message, 'err'); }
}

async function retryQueueJob(key) {
  try { await api(`queue/${key}/retry`, {}); refreshQueue(); }
  catch (e) { setStatus($('#queue-status'), 'Retry failed: ' + e.message, 'err'); }
}

(function wireQueue() {
  const btn = $('#btn-queue-import'); const file = $('#queue-file');
  if (!btn || !file) return;
  btn.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const f = file.files[0]; if (!f) return;
    const text = await f.text(); file.value = '';
    const sel = $('#room-select');
    const roomId = sel && sel.value ? +sel.value : null;
    setStatus($('#queue-status'), 'Importing…');
    try {
      const res = await api('queue/import', { content: text, room_id: roomId });
      const into = roomId && sel ? ` into room "${sel.options[sel.selectedIndex].text.replace(/ \(\d+\)$/, '')}"` : '';
      setStatus($('#queue-status'), `Added ${res.added}${into}, skipped ${res.skipped} (already queued). Working in the background…`, 'ok');
      loadRooms();
      refreshQueue();
    } catch (e) { setStatus($('#queue-status'), 'Import failed: ' + e.message, 'err'); }
  });
  const stopBtn = $('#btn-queue-stop-box');
  if (stopBtn) stopBtn.addEventListener('click', async () => {
    if (!confirm('Stop the AI boxing run? Clips still waiting become draw-manually.')) return;
    try {
      const r = await api('queue/stop-boxing', {});
      setStatus($('#queue-status'), `Boxing stopped — ${r.stopped} clip(s) set to draw-manually.`, 'ok');
      refreshQueue();
    } catch (e) { setStatus($('#queue-status'), 'Stop failed: ' + e.message, 'err'); }
  });
  const rsel = $('#room-select');
  if (rsel) rsel.addEventListener('change', refreshQueue);
  const rnew = $('#btn-room-new');
  if (rnew) rnew.addEventListener('click', createRoomPrompt);
  const rdel = $('#btn-room-del');
  if (rdel) rdel.addEventListener('click', deleteSelectedRoom);
  loadRooms();
  refreshQueue();
  setInterval(refreshQueue, 3000);
  setInterval(autosaveQueue, 5000);
})();

// ───────────────────────── rooms (queue grouping) ─────────────────────────
async function loadRooms() {
  try {
    const r = await fetch('/api/rooms');
    if (!r.ok) return;
    state.rooms = (await r.json()).rooms || [];
  } catch (e) { return; }
  const sel = $('#room-select');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">All rooms</option>'
    + state.rooms.map((rm) => `<option value="${rm.id}">${thumbEscape(rm.name)} (${rm.jobs})</option>`).join('');
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

async function createRoomPrompt() {
  const name = (prompt('New room name (e.g. teguh, rzl):') || '').trim();
  if (!name) return;
  try {
    const room = await api('rooms', { name });
    await loadRooms();
    const sel = $('#room-select');
    if (sel) { sel.value = String(room.id); refreshQueue(); }
    setStatus($('#queue-status'), `Room "${room.name}" ready — imports now go into it.`, 'ok');
  } catch (e) { setStatus($('#queue-status'), 'Create room failed: ' + e.message, 'err'); }
}

async function deleteSelectedRoom() {
  const sel = $('#room-select');
  if (!sel || !sel.value) { setStatus($('#queue-status'), 'Pick a room to delete (not "All rooms").', 'err'); return; }
  const label = sel.options[sel.selectedIndex].text;
  if (!confirm(`Delete room "${label}" AND all its clips + downloaded videos? This cannot be undone.`)) return;
  try {
    const r = await fetch('/api/rooms/' + sel.value, { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const res = await r.json();
    sel.value = '';
    await loadRooms();
    refreshQueue();
    setStatus($('#queue-status'), `Room deleted — ${res.deleted_jobs} clip(s) + videos removed.`, 'ok');
  } catch (e) { setStatus($('#queue-status'), 'Delete room failed: ' + e.message, 'err'); }
}

// ───────────────────────── sound effects (soundboard) ─────────────────────────
// Persistent library of imported sounds (server-side) + per-clip placements
// (one-shot at a time, or a layer over a range, each with a volume). Placements
// ride along in the render body → renderer mixes them into the audio.
const sfxVideo = $('#sfx-video');

function initSfxStep() {
  if (!state.jobId || !sfxVideo) return;
  const path = video.getAttribute('src');
  if (path && sfxVideo.dataset.path !== path) {
    sfxVideo.dataset.path = path;
    sfxVideo.src = path;
  }
  loadSounds();
  renderSfxList();
}

async function loadSounds() {
  try {
    const r = await fetch('/api/soundboard');
    if (!r.ok) return;
    const data = await r.json();
    state.sounds = data.sounds || [];
    renderBoard();
    renderRangeSoundSelect();
  } catch (e) { /* best-effort */ }
}

function fmtDur(s) {
  s = s || 0;
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function renderBoard() {
  const board = $('#sfx-board');
  if (!board) return;
  if (!state.sounds.length) {
    board.innerHTML = '<div class="muted">No sounds yet — Import an audio file (mp3 / wav / ogg / m4a…).</div>';
    return;
  }
  board.innerHTML = state.sounds.map((s) => `
    <div class="sfx-pad" data-id="${s.id}">
      <button class="sfx-pad-play" data-id="${s.id}" title="preview">▶</button>
      <span class="sfx-pad-name" title="${thumbEscape(s.name)}">${thumbEscape(s.name)}</span>
      <span class="sfx-pad-dur">${fmtDur(s.duration)}</span>
      <button class="sfx-pad-add" data-id="${s.id}" title="drop a one-shot at the current time">＋ here</button>
      <button class="sfx-pad-del danger" data-id="${s.id}" title="delete from library">×</button>
    </div>`).join('');
  board.querySelectorAll('.sfx-pad-play').forEach((b) => b.addEventListener('click', () => previewSound(b.dataset.id)));
  board.querySelectorAll('.sfx-pad-add').forEach((b) => b.addEventListener('click', () => addOneShot(b.dataset.id)));
  board.querySelectorAll('.sfx-pad-del').forEach((b) => b.addEventListener('click', () => deleteSound(b.dataset.id)));
}

function renderRangeSoundSelect() {
  const sel = $('#sfx-range-sound');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = state.sounds.map((s) => `<option value="${s.id}">${thumbEscape(s.name)}</option>`).join('');
  if (prev && state.sounds.some((s) => s.id === prev)) sel.value = prev;
}

function previewSound(id) {
  if (state.sfxPreview) { try { state.sfxPreview.pause(); } catch (e) {} }
  const a = new Audio('/api/soundboard/' + id + '/audio');
  state.sfxPreview = a;
  a.play().catch(() => {});
}

async function onSfxImport() {
  const file = $('#sfx-file').files[0];
  if (!file) return;
  $('#sfx-file').value = '';
  setStatus($('#sfx-status'), `Importing ${file.name}…`);
  try {
    const stem = file.name.replace(/\.[^.]+$/, '');
    const r = await fetch(`/api/soundboard?name=${encodeURIComponent(stem)}&filename=${encodeURIComponent(file.name)}`,
      { method: 'POST', body: file });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    await loadSounds();
    setStatus($('#sfx-status'), `Added "${stem}".`, 'ok');
  } catch (e) {
    setStatus($('#sfx-status'), 'Import failed: ' + e.message, 'err');
  }
}

async function deleteSound(id) {
  const s = state.sounds.find((x) => x.id === id);
  if (!confirm(`Delete "${s ? s.name : id}" from the soundboard? Its placements are removed too.`)) return;
  try {
    await fetch('/api/soundboard/' + id, { method: 'DELETE' });
    state.sfx = state.sfx.filter((p) => p.sound_id !== id);
    await loadSounds();
    renderSfxList();
  } catch (e) {
    setStatus($('#sfx-status'), 'Delete failed: ' + e.message, 'err');
  }
}

function addOneShot(id) {
  const t = sfxVideo ? (sfxVideo.currentTime || 0) : 0;
  state.sfx.push({ sound_id: id, kind: 'oneshot', t: +t.toFixed(2), volume: 1.0 });
  state.sfx.sort((a, b) => a.t - b.t);
  renderSfxList();
  setStatus($('#sfx-status'), `One-shot added @ ${t.toFixed(2)}s.`, 'ok');
}

function addSfxRange() {
  const sel = $('#sfx-range-sound');
  const id = sel ? sel.value : '';
  if (!id) { setStatus($('#sfx-status'), 'No sound selected — import one first.', 'err'); return; }
  const rs = parseFloat($('#sfx-rs').value);
  const reV = parseFloat($('#sfx-re').value);
  const t = isNaN(rs) ? 0 : Math.max(0, rs);
  const te = isNaN(reV) ? (sfxVideo && sfxVideo.duration ? sfxVideo.duration : t + 1) : reV;
  if (te <= t) { setStatus($('#sfx-status'), 'End must be after start.', 'err'); return; }
  state.sfx.push({
    sound_id: id, kind: 'range', t: +t.toFixed(2), t_end: +te.toFixed(2),
    volume: 1.0, loop: $('#sfx-loop') ? $('#sfx-loop').checked : true,
  });
  state.sfx.sort((a, b) => a.t - b.t);
  renderSfxList();
  setStatus($('#sfx-status'), `Layer added ${t.toFixed(1)}–${te.toFixed(1)}s.`, 'ok');
}

function soundName(id) {
  const s = state.sounds.find((x) => x.id === id);
  return s ? s.name : '(deleted sound)';
}

function renderSfxList() {
  const ol = $('#sfx-list');
  const count = $('#sfx-count');
  if (count) count.textContent = state.sfx.length;
  if (!ol) return;
  if (!state.sfx.length) {
    ol.innerHTML = '<li class="empty">No sounds placed yet — preview a pad, then ＋ here / Add layer.</li>';
    return;
  }
  ol.innerHTML = state.sfx.map((p, i) => {
    const when = p.kind === 'range'
      ? `layer ${(+p.t).toFixed(1)}–${(+p.t_end).toFixed(1)}s${p.loop ? ' · loop' : ''}`
      : `one-shot @ ${(+p.t).toFixed(2)}s`;
    const volPct = Math.round((p.volume == null ? 1 : p.volume) * 100);
    return `<li>
      <span class="sfx-it-name" title="${thumbEscape(soundName(p.sound_id))}">${thumbEscape(soundName(p.sound_id))}</span>
      <span class="sfx-it-when">${when}</span>
      <span class="sfx-it-vol"><input type="range" min="0" max="200" step="5" value="${volPct}" data-i="${i}" class="sfx-vol"><b data-volb="${i}">${volPct}%</b></span>
      <button class="sfx-it-seek" data-seek="${i}" title="seek to its start">▶</button>
      <button class="sfx-it-del danger" data-del="${i}" title="remove placement">×</button>
    </li>`;
  }).join('');
  ol.querySelectorAll('.sfx-vol').forEach((inp) => inp.addEventListener('input', (e) => {
    const i = +e.target.dataset.i;
    state.sfx[i].volume = (+e.target.value) / 100;
    const b = ol.querySelector(`b[data-volb="${i}"]`); if (b) b.textContent = e.target.value + '%';
  }));
  ol.querySelectorAll('.sfx-it-seek').forEach((b) => b.addEventListener('click', () => {
    const p = state.sfx[+b.dataset.seek]; if (p && sfxVideo) sfxVideo.currentTime = p.t;
  }));
  ol.querySelectorAll('.sfx-it-del').forEach((b) => b.addEventListener('click', () => {
    state.sfx.splice(+b.dataset.del, 1); renderSfxList();
  }));
}

if (sfxVideo) {
  sfxVideo.addEventListener('loadedmetadata', () => {
    const scr = $('#sfx-scrubber'); if (scr) scr.max = sfxVideo.duration || 0;
    const d = $('#sfx-dur'); if (d) d.textContent = fmtTime(sfxVideo.duration || 0);
  });
  sfxVideo.addEventListener('timeupdate', () => {
    const t = $('#sfx-time'); if (t) t.textContent = fmtTime(sfxVideo.currentTime || 0);
    const scr = $('#sfx-scrubber'); if (scr && document.activeElement !== scr) scr.value = sfxVideo.currentTime || 0;
  });
  sfxVideo.addEventListener('play', () => { const b = $('#sfx-play'); if (b) b.textContent = '❚❚'; });
  sfxVideo.addEventListener('pause', () => { const b = $('#sfx-play'); if (b) b.textContent = '▶'; });
  $('#sfx-play').addEventListener('click', () => { if (sfxVideo.paused) sfxVideo.play(); else sfxVideo.pause(); });
  $('#sfx-scrubber').addEventListener('input', (e) => { if (sfxVideo.duration) sfxVideo.currentTime = +e.target.value; });
  $('#btn-sfx-import').addEventListener('click', () => $('#sfx-file').click());
  $('#sfx-file').addEventListener('change', onSfxImport);
  $('#sfx-rs-cur').addEventListener('click', () => { $('#sfx-rs').value = (sfxVideo.currentTime || 0).toFixed(2); });
  $('#sfx-re-cur').addEventListener('click', () => { $('#sfx-re').value = (sfxVideo.currentTime || 0).toFixed(2); });
  $('#btn-sfx-addrange').addEventListener('click', addSfxRange);
}
