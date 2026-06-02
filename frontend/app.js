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
};

// ───────────────────────── step navigation ─────────────────────────
function showStep(n) {
  $$('.panel').forEach((p) => p.classList.add('hidden'));
  $(`#panel-${n}`).classList.remove('hidden');
  $$('.steps .step').forEach((b) => b.classList.toggle('active', b.dataset.step === String(n)));
  if (n === 2) { resizeOverlay(); drawOverlay(); renderKfList(); }
  if (n === 4) drawPreview($('#preview-2'));
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
  if (!url) { setStatus($('#dl-status'), 'URL kosong', 'err'); return; }
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
    state.srcW = r.width; state.srcH = r.height;
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
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
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
  if (!kfs.length) { ol.innerHTML = '<li class="muted">Belum ada keyframe.</li>'; return; }
  kfs.forEach((k, i) => {
    const next = kfs[i + 1];
    const tEnd = next ? next.t.toFixed(2) + 's' : 'end';
    const cur = state.currentTime >= k.t && (!next || state.currentTime < next.t);
    const li = document.createElement('li');
    li.className = 'kf-row' + (cur ? ' current' : '');
    li.innerHTML = `
      <span class="kf-seg">[${k.t.toFixed(2)}s → ${tEnd}]</span>
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
    ctx.fillText('ilustrasi', PREVIEW_W / 2, TOP_PH + (PREVIEW_H - TOP_PH) / 2);
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
  if (!state.box.length) { setStatus($('#tr-status'), 'Gambar minimal 1 crop box dulu', 'err'); return; }
  setStatus($('#tr-status'), 'Transcribe (Whisper)… first run lama (load model)');
  $('#btn-continue').disabled = true;
  try {
    const r = await api('transcribe', { job_id: state.jobId });
    state.words = r.words || [];
    setStatus($('#tr-status'), `OK · ${state.words.length} kata`, 'ok');
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
  setStatus($('#plan-status'), 'AI nyariin gambar per segmen…');
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
    // auto-pick first candidate per segment (biar bisa langsung render)
    for (const s of state.segments) {
      if (s.candidates && s.candidates.length) pickCandidate(s, s.candidates[0], false);
    }
    renderSegments();
    setStatus($('#plan-status'), `OK · ${state.segments.length} segmen`, 'ok');
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
  if (!state.segments.length) { host.innerHTML = '<div class="muted">Belum di-generate.</div>'; return; }
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
        <button class="seg-research ghost">↻ cari ulang</button>
      </div>
      <div class="seg-candidates">${cands || '<span class="seg-none">Ga ada hasil (cek PEXELS_API_KEY / edit keyword).</span>'}</div>`;

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
        btn.disabled = false; btn.textContent = '↻ cari ulang';
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
  if (!state.box.length) { setStatus($('#rd-status'), 'Belum ada crop box', 'err'); return; }
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
