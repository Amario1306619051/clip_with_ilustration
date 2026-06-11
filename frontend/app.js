// ILLUSTRATOR frontend — single crop box (top) + AI illustration track (bottom).
// Vanilla JS, no build step. Coordinate spaces mirror clipper:
//   1. source pixel space — boxes stored here (video native res)
//   2. overlay display space — mouse events, = canvas size
//   3. preview canvas space — fixed 270×480 mock-up
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const PREVIEW_W = 270, PREVIEW_H = 480;
const TOP_FRAC = 3 / 8;            // top slot = 3/8 of height
const TOP_PH = Math.round(PREVIEW_H * TOP_FRAC); // 180
// The crop box always renders into the top slot (1080×720 → 3:2). The box is
// FREE-FORM (any size); a render-area guide shows what survives the cover-crop.
const BOX_AR = 1080 / 720;         // 1.5

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
  activeBox: null,  // null | 1
  currentTime: 0,
  abDrag: null,                          // 'start' | 'end' while dragging a range handle
  autoRange: { start: 0, end: null },    // AI auto-box time range (null end = clip end)
  activeQueueKey: null,                  // batch-queue job currently loaded (null = ad-hoc)
  queueSig: null,                        // signature of last auto-saved queue state
  sounds: [],                            // soundboard library (from /api/soundboard)
  sfx: [],                               // SFX placements for this clip (sent at render)
  sfxPreview: null,                      // currently-playing preview Audio
};

// ───────────────────────── step navigation ─────────────────────────
function showStep(n) {
  $$('.panel').forEach((p) => p.classList.add('hidden'));
  $(`#panel-${n}`).classList.remove('hidden');
  $$('.steps .step').forEach((b) => b.classList.toggle('active', b.dataset.step === String(n)));
  if (n === 2) { resizeOverlay(); drawOverlay(); renderKfList(); renderAutoRange(); }
  if (n === 4) drawPreview($('#preview-2'));
  if (n === 5) requestAnimationFrame(initThumbStep);
  if (n === 6) requestAnimationFrame(initSfxStep);
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
    state.autoObs = '';            // no model observation for an ad-hoc clip
    if ($('#ab-context')) $('#ab-context').value = '';   // context is per-clip
    state.activeQueueKey = null;   // ad-hoc download — not editing a queue job
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
});
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
  drawOverlay();
  drawPreview($('#preview'));
}

$('[data-clear="1"]').addEventListener('click', () => { state.box = []; refreshKfUI(); });

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
    const keep = coverKeepRect({ x: sa.x, y: sa.y, w: sb.x - sa.x, h: sb.y - sa.y }, BOX_AR);
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
    d.className = 'timeline-dot';
    d.style.left = (k.t / dur * 100) + '%';
    d.title = `kf @ ${k.t.toFixed(2)}s`;
    d.addEventListener('click', () => { video.currentTime = k.t; });
    host.appendChild(d);
  }
}
function updateTimelineCursor() {
  const dur = video.duration || state.duration || 1;
  $('#timeline-cursor').style.left = (state.currentTime / dur * 100) + '%';
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
    li.className = 'kf-row' + (cur ? ' current' : '');
    // Editable start/end — click a time to lengthen/shorten the segment
    // (start = this kf's t, end = the NEXT kf's t; clip end isn't editable).
    li.innerHTML = `
      <span class="kf-seg">[<span class="kf-tedit" data-i="${i}" data-which="start"
        title="Click to edit when this segment STARTS (lengthen/shorten)">${k.t.toFixed(2)}s</span> → ${
        next ? `<span class="kf-tedit" data-i="${i}" data-which="end"
        title="Click to edit when this segment ENDS (moves the next keyframe)">${next.t.toFixed(2)}s</span>` : 'end'}]</span>
      <span class="kf-onscreen" title="this segment is what's on screen at the playhead right now">▶ ON SCREEN</span>
      <span class="kf-dim">${Math.round(k.w)}×${Math.round(k.h)} @(${Math.round(k.x)},${Math.round(k.y)})</span>
      <button class="kf-tag" data-i="${i}" data-act="interp">${(k.interp || 'hold') === 'linear' ? 'PAN→' : 'HOLD'}</button>
      <button class="kf-tag" data-i="${i}" data-act="fit">${k.fit === 'blur_pad' ? 'BLUR' : 'COVER'}</button>
      <button class="kf-tag" data-i="${i}" data-act="seek">seek</button>
      <button class="kf-tag danger" data-i="${i}" data-act="del">×</button>`;
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
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, PREVIEW_W, PREVIEW_H);

  // top slot: cropped video
  const b = boxAt(state.currentTime);
  if (b && video.videoWidth) {
    drawCover(ctx, video, b.x, b.y, b.w, b.h, 0, 0, PREVIEW_W, TOP_PH, b.fit === 'blur_pad');
  }
  // bottom slot: picked illustration for current time
  const seg = state.segments.find((s) => state.currentTime >= s.t_start && state.currentTime < s.t_end && s.picked);
  if (seg && seg._img && seg._img.complete) {
    drawCover(ctx, seg._img, 0, 0, seg._img.naturalWidth, seg._img.naturalHeight, 0, TOP_PH, PREVIEW_W, PREVIEW_H - TOP_PH, false);
  } else {
    ctx.fillStyle = '#1d1d24';
    ctx.fillRect(0, TOP_PH, PREVIEW_W, PREVIEW_H - TOP_PH);
    ctx.fillStyle = '#5a5a68';
    ctx.font = '10px JetBrains Mono';
    ctx.textAlign = 'center';
    ctx.fillText('illustration', PREVIEW_W / 2, TOP_PH + (PREVIEW_H - TOP_PH) / 2);
    ctx.textAlign = 'left';
  }
  // slot divider line (where caption sits)
  ctx.strokeStyle = 'rgba(232,255,58,0.4)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, TOP_PH); ctx.lineTo(PREVIEW_W, TOP_PH); ctx.stroke();
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
  setStatus($('#plan-status'), 'AI is finding an image for each segment…');
  $('#btn-plan').disabled = true;
  try {
    const r = await api('plan', {
      job_id: state.jobId,
      words: state.words,
      segment_seconds: state.segSeconds,
      duration: video.duration || state.duration,
      // overall video topic → LLM anchors every query to it (not literal words)
      title: $('#f-title').value.trim() || 'clip',
      description: $('#f-desc').value.trim(),
    });
    state.segments = (r.segments || []).map((s) => ({ ...s, picked: null, _img: null }));
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
  seg._img = img;
  if (rerender) renderSegments();
}

function renderSegments() {
  const host = $('#ill-segments');
  if (!state.segments.length) { host.innerHTML = '<div class="muted">Not generated yet.</div>'; return; }
  host.innerHTML = '';
  state.segments.forEach((seg) => {
    const card = document.createElement('div');
    card.className = 'seg-card';
    const cands = (seg.candidates || []).map((c) => `
      <div class="cand ${seg.picked === c.full ? 'picked' : ''}" data-id="${c.id}">
        <img src="${c.thumb}" alt="${(c.alt || '').replace(/"/g, '')}" loading="lazy">
        <span class="cand-by">${c.photographer || ''}</span>
      </div>`).join('');
    card.innerHTML = `
      <div class="seg-head">
        <span class="seg-time">${seg.t_start.toFixed(0)}–${seg.t_end.toFixed(0)}s</span>
        <span class="seg-text">"${(seg.text || '').slice(0, 80) || '(no speech)'}"</span>
        <input class="seg-query" value="${(seg.query || '').replace(/"/g, '')}">
        <button class="seg-research ghost">↻ search again</button>
      </div>
      <div class="seg-candidates">${cands || '<span class="seg-none">No results (check PEXELS_API_KEY / edit the keyword).</span>'}</div>`;

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
        const r = await api('search', { query: q });
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
}

$('#btn-to-render').addEventListener('click', () => showStep(4));

// ───────────────────────── step 4: render ─────────────────────────
$('#rd-clear').addEventListener('click', () => { $('#rd-start').value = ''; $('#rd-end').value = ''; });

function buildIllustrations() {
  return state.segments
    .filter((s) => s.picked)
    .map((s) => ({ t_start: s.t_start, t_end: s.t_end, url: s.picked }));
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
      words: withCaption ? state.words : [],
      caption_font: $('#cap-font').value,
      caption_size: Number($('#cap-size').value) || 64,
      cleanup: false,
      render_start: rs === '' ? null : Number(rs),
      render_end: re === '' ? null : Number(re),
      sfx: state.sfx,
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
    });
    const kfs = r.keyframes || [];
    if (!kfs.length) {
      setStatus($('#ab-status'), r.message || 'Nothing detected — try a different prompt or range', 'err');
      return;
    }
    // Keep manual keyframes OUTSIDE the predicted range; replace inside it.
    const keep = state.box.filter((k) => k.t < t0 - AB_EPS || k.t > t1 + AB_EPS);
    state.box = [...keep, ...kfs].sort((a, b) => a.t - b.t);
    setStatus($('#ab-status'),
      `${r.message} Added ${kfs.length} keyframes — drag / resize / delete below.`, 'ok');
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
};

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
  drawThumb();
}

function drawThumb() {
  if (!thumbCanvas) return;
  drawThumbnailInto(thumbCanvas.getContext('2d'), thumbCanvas.width, thumbCanvas.height);
}

function drawThumbnailInto(ctx, W, H) {
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W, H);
  drawThumbBg(ctx, W, H);
  if (thumb.shade) drawThumbShade(ctx, W, H);
  drawThumbText(ctx, W, H);
}

function drawThumbBg(ctx, W, H) {
  const vw = thumbVideo ? thumbVideo.videoWidth : 0;
  const vh = thumbVideo ? thumbVideo.videoHeight : 0;
  if (!vw || !vh) return;
  const dstAR = W / H, srcAR = vw / vh;
  let sw, sh, sx, sy;
  if (srcAR > dstAR) { sh = vh; sw = vh * dstAR; sy = 0; sx = (vw - sw) * thumb.panX; }
  else { sw = vw; sh = vw / dstAR; sx = 0; sy = (vh - sh) * thumb.panY; }
  try { ctx.drawImage(thumbVideo, sx, sy, sw, sh, 0, 0, W, H); } catch (_) {}
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
    const title = (($('#f-title').value || 'clip').trim() || 'clip').replace(/[^\w.-]+/g, '_');
    a.href = URL.createObjectURL(blob);
    a.download = `${title}_thumbnail.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    setStatus($('#thumb-status'), `Saved ${a.download}`, 'ok');
  }, 'image/png');
}

if (thumbVideo && thumbCanvas) {
  thumbVideo.addEventListener('loadedmetadata', () => {
    const dur = thumbVideo.duration || 0;
    const scr = $('#thumb-scrubber');
    if (scr) scr.max = dur;
    const d = $('#thumb-dur'); if (d) d.textContent = fmtTime(dur);
    drawThumb();
  });
  thumbVideo.addEventListener('loadeddata', drawThumb);   // first frame decoded → paint it
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
    renderQueueList(data.jobs || []);
  } catch (e) { /* best-effort */ }
}

function renderQueueList(jobs) {
  const ul = $('#queue-list'); const meta = $('#queue-meta');
  if (!ul) return;
  if (!jobs.length) { ul.innerHTML = ''; if (meta) meta.textContent = 'No jobs queued.'; return; }
  const c = jobs.reduce((a, j) => { a[j.status] = (a[j.status] || 0) + 1; return a; }, {});
  const working = (c.pending || 0) + (c.downloading || 0) + (c.downloaded || 0) + (c.predicting || 0);
  if (meta) meta.textContent = `${jobs.length} job(s) · ${c.ready || 0} ready · ${working} working${c.error ? ` · ${c.error} error` : ''}`;
  ul.innerHTML = jobs.map((j) => {
    const active = j.key === state.activeQueueKey ? ' active' : '';
    const canOpen = j.status === 'ready';
    const kf = canOpen ? ` · ${j.kf1} kf` : '';
    const retry = j.status === 'error' ? `<button class="q-retry" data-key="${j.key}" title="retry this job">↻</button>` : '';
    return `<li class="queue-item${active}" data-status="${j.status}">
      <button class="queue-open" data-key="${j.key}" ${canOpen ? '' : 'disabled'} title="${thumbEscape(j.message || '')}">
        <span class="q-id">${thumbEscape(j.id)}</span>
        <span class="q-title">${thumbEscape(j.title || '')}</span>
        <span class="q-sub">${qStatusBadge(j.status)}${kf}</span>
      </button>
      ${retry}
      <button class="q-del" data-key="${j.key}" title="delete job + its downloaded clip">×</button>
    </li>`;
  }).join('');
  ul.querySelectorAll('.queue-open').forEach((b) => b.addEventListener('click', () => openQueueJob(b.dataset.key)));
  ul.querySelectorAll('.q-del').forEach((b) => b.addEventListener('click', () => deleteQueueJob(b.dataset.key)));
  ul.querySelectorAll('.q-retry').forEach((b) => b.addEventListener('click', () => retryQueueJob(b.dataset.key)));
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
  state.words = [];
  state.sfx = [];                  // placements are per-clip
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
  video.src = job.video_path;
  $('#range-meta').textContent = `source: ${job.width}×${job.height} · ${(job.duration || 0).toFixed(1)}s`;
  state.queueSig = queueSig();
  setStatus($('#queue-status'), `Editing "${job.id}" — box edits auto-save. Illustration + render stay manual (Step 3-4).`, 'ok');
  showStep(2);
  refreshQueue();
}

function queueSig() {
  return JSON.stringify({
    t: ($('#f-title').value || ''),
    b: state.box,
    ctx: ($('#ab-context') ? $('#ab-context').value : ''),
    p1: ($('#ab-prompt') ? $('#ab-prompt').value : ''),
  });
}

async function autosaveQueue() {
  if (!state.activeQueueKey) return;
  const sig = queueSig();
  if (sig === state.queueSig) return;
  state.queueSig = sig;
  try {
    await api(`queue/${state.activeQueueKey}/save`, {
      title: ($('#f-title').value || 'clip').trim() || 'clip',
      box1: state.box || [],
      context: $('#ab-context') ? $('#ab-context').value : '',
      prompt1: $('#ab-prompt') ? $('#ab-prompt').value : '',
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
    setStatus($('#queue-status'), 'Importing…');
    try {
      const res = await api('queue/import', { content: text });
      setStatus($('#queue-status'), `Added ${res.added}, skipped ${res.skipped} (already queued). Working in the background…`, 'ok');
      refreshQueue();
    } catch (e) { setStatus($('#queue-status'), 'Import failed: ' + e.message, 'err'); }
  });
  refreshQueue();
  setInterval(refreshQueue, 3000);
  setInterval(autosaveQueue, 5000);
})();

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
