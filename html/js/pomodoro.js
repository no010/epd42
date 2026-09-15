/* Browser pomodoro: the state machine of tools/epd-pomodoro/state.py and the
 * hourglass face of face.py, rendered on a 400x300 canvas for the stream
 * protocol.  Seconds are meaningless on an e-ink panel - progress is the
 * hourglass, and the only number is the remaining minutes.
 */
'use strict';

const PHASE_WORK = 'work';
const PHASE_SHORT_BREAK = 'short_break';
const PHASE_LONG_BREAK = 'long_break';
const PHASE_NAMES = {
  work: ['专注', 'FOCUS'],
  short_break: ['短休息', 'SHORT BREAK'],
  long_break: ['长休息', 'LONG BREAK'],
};
const PHASE_DURATION_KEY = {
  work: 'workMinutes',
  short_break: 'shortMinutes',
  long_break: 'longMinutes',
};
const POMO_DEFAULTS = { workMinutes: 25, shortMinutes: 5, longMinutes: 15, rounds: 4 };

const TITLE = 'POMODORO';
const STAMP_PX = 16;
const LABEL_MIN_PX = 26, LABEL_MAX_PX = 44;
const MINUTE_MIN_PX = 18, MINUTE_MAX_PX = 28;
const DOT_RADIUS = 7;
const H_GAP = 10;
const HG_MIN_H = 80, HG_MAX_H = 170;
const HG_ASPECT = 0.72;
const HG_INSET = 3;
const HG_BAR_H = 4;

const STORE_KEY = 'epd42.web.pomodoro';
const CFG_KEY = 'epd42.web.pomodoro.cfg';

/* ── state machine (port of state.py) ─────────────────────────────────── */

function minutesToSeconds(minutes) {
  return Math.max(1, Math.round(Number(minutes) * 60));
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-`
       + `${String(d.getDate()).padStart(2, '0')}`;
}

function loadCfg() {
  try {
    return { ...POMO_DEFAULTS, ...JSON.parse(localStorage.getItem(CFG_KEY)) };
  } catch (e) { return { ...POMO_DEFAULTS }; }
}

function saveCfg(cfg) {
  localStorage.setItem(CFG_KEY, JSON.stringify(cfg));
}

function freshState(cfg) {
  const work = minutesToSeconds(cfg.workMinutes);
  return {
    phase: PHASE_WORK, phaseSeconds: work, remaining: work,
    running: false, pomodoroCount: 0, cycleTotal: 0,
    cycleDate: todayIso(), rounds: Math.max(1, cfg.rounds | 0),
    updatedAt: 0,
  };
}

/** Load the saved state, rolling a mid-run state forward by wall clock. */
function loadState() {
  let state;
  try {
    const data = JSON.parse(localStorage.getItem(STORE_KEY));
    if (data && PHASE_NAMES[data.phase]) {
      state = { ...freshState(loadCfg()), ...data };
      state.rounds = Math.max(1, data.rounds | 0);
    }
  } catch (e) { /* fall through to a fresh state */ }
  if (!state) state = freshState(loadCfg());
  state.phaseSeconds = Math.max(1, state.phaseSeconds | 0);
  state.remaining = Math.max(0, Math.min(state.remaining | 0, state.phaseSeconds));
  if (state.running && state.updatedAt) {
    const elapsed = Math.max(0, Math.floor((Date.now() - state.updatedAt) / 1000));
    state.remaining = Math.max(0, state.remaining - elapsed);
    if (state.remaining === 0) state.running = false;
  }
  if (state.cycleDate !== todayIso()) {
    state.cycleDate = todayIso();
    state.cycleTotal = 0;
  }
  return state;
}

function saveState(state) {
  state.updatedAt = Date.now();
  localStorage.setItem(STORE_KEY, JSON.stringify(state));
}

function phaseSecondsFor(phase, cfg) {
  return minutesToSeconds(cfg[PHASE_DURATION_KEY[phase]]);
}

/** Move past the finished phase and start the next one. */
function advance(state, cfg) {
  if (state.cycleDate !== todayIso()) {
    state.cycleDate = todayIso();
    state.cycleTotal = 0;
  }
  if (state.phase === PHASE_WORK) {
    state.pomodoroCount += 1;
    state.cycleTotal += 1;
    state.phase = (state.pomodoroCount % state.rounds === 0)
      ? PHASE_LONG_BREAK : PHASE_SHORT_BREAK;
  } else {
    if (state.phase === PHASE_LONG_BREAK) state.pomodoroCount = 0;
    state.phase = PHASE_WORK;
  }
  state.phaseSeconds = phaseSecondsFor(state.phase, cfg);
  state.remaining = state.phaseSeconds;
}

/** Move to the next phase without touching the counters (manual skip). */
function skipPhase(state, cfg) {
  if (state.phase === PHASE_WORK) {
    state.phase = PHASE_SHORT_BREAK;
  } else {
    if (state.phase === PHASE_LONG_BREAK) state.pomodoroCount = 0;
    state.phase = PHASE_WORK;
  }
  state.phaseSeconds = phaseSecondsFor(state.phase, cfg);
  state.remaining = state.phaseSeconds;
}

/* ── text helpers (canvas analogues of PIL) ───────────────────────────── */

/** Largest size <= start whose text still fits usable (face.py _fit_font). */
function fitFont(ctx, text, start, minPx, maxPx, family, usable) {
  let size = Math.max(minPx, Math.min(maxPx, start));
  setFont(ctx, size, family);
  while (size > minPx && textWidth(ctx, text) > usable) {
    size -= 2;
    setFont(ctx, size, family);
  }
  return size;
}

function centerText(ctx, text, y, px, family, width) {
  setFont(ctx, px, family);
  drawText(ctx, text, Math.max(0, (width - textWidth(ctx, text)) / 2), y, px, family);
}

/* ── the face (port of face.py compose) ───────────────────────────────── */

function minuteText(state) {
  const totalMin = Math.max(1, Math.ceil(state.phaseSeconds / 60));
  const leftMin = Math.max(0, Math.ceil(state.remaining / 60));
  let text = `剩 ${leftMin} / ${totalMin} 分钟`;
  if (!state.running) text = `已暂停 · ${text}`;
  return text;
}

function drawHourglass(ctx, cx, yTop, height, remaining, running) {
  const frac = Math.max(0, Math.min(1, remaining));
  const width = Math.floor(height * HG_ASPECT);
  const x0 = cx - Math.floor(width / 2), x1 = cx + Math.floor(width / 2);
  const y0 = yTop, y1 = yTop + height;
  const ym = y0 + Math.floor(height / 2);
  const frameHalf = Math.floor(width / 2) + 4;

  const sx0 = x0 + HG_INSET, sx1 = x1 - HG_INSET;
  const halfW = (sx1 - sx0) / 2;
  const topH = (ym - HG_INSET) - (y0 + HG_INSET);
  const botH = (y1 - HG_INSET) - (ym + HG_INSET);

  // Top bulb: sand drains OUT through the neck, so the void grows at the top.
  const sandH = Math.floor(frac * topH);
  if (sandH >= 1) {
    const ySurface = ym - HG_INSET - sandH;
    const hw = halfW * (sandH / topH);
    ctx.beginPath();
    ctx.moveTo(cx - hw, ySurface);
    ctx.lineTo(cx + hw, ySurface);
    ctx.lineTo(cx, ym - HG_INSET);
    ctx.closePath();
    ctx.fill();
  }

  const pileH = Math.floor((1 - frac) * botH);
  if (pileH >= 1) {
    const yCut = y1 - HG_INSET - pileH;
    const hw = halfW * (1 - pileH / botH);
    ctx.beginPath();
    ctx.moveTo(cx - hw, yCut);
    ctx.lineTo(cx + hw, yCut);
    ctx.lineTo(sx1, y1 - HG_INSET);
    ctx.lineTo(sx0, y1 - HG_INSET);
    ctx.closePath();
    ctx.fill();
  }

  if (running && 0 < frac && frac < 1) {
    const pileTop = y1 - HG_INSET - pileH;
    ctx.beginPath();
    ctx.moveTo(cx, ym);
    ctx.lineTo(cx, Math.max(ym, pileTop - 1));
    ctx.stroke();
  }

  // Outline on top of the sand so the edges stay crisp.
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(x0, y0); ctx.lineTo(x1, y0); ctx.lineTo(cx, ym); ctx.closePath();
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(cx, ym); ctx.lineTo(x0, y1); ctx.lineTo(x1, y1); ctx.closePath();
  ctx.stroke();
  ctx.fillRect(cx - frameHalf, y0 - HG_BAR_H, frameHalf * 2, HG_BAR_H);
  ctx.fillRect(cx - frameHalf, y1, frameHalf * 2, HG_BAR_H);
}

function drawDots(ctx, state, y, dotPx, width) {
  const filled = Math.max(0, Math.min(state.pomodoroCount, state.rounds));
  const current = state.phase === PHASE_WORK ? filled : -1;
  const text = `第 ${state.pomodoroCount}/${state.rounds} 个`;
  setFont(ctx, dotPx, CJK_FONT);
  const textW = textWidth(ctx, text);
  const gap = 12;
  const dotW = state.rounds * 2 * DOT_RADIUS + (state.rounds - 1) * gap;
  let x = Math.max(8, (width - (dotW + 18 + textW)) / 2);
  const cy = y + DOT_RADIUS;
  for (let index = 0; index < state.rounds; index++) {
    const cx = x + DOT_RADIUS + index * (2 * DOT_RADIUS + gap);
    ctx.beginPath();
    ctx.arc(cx, cy, DOT_RADIUS, 0, 2 * Math.PI);
    if (index < filled) {
      ctx.fill();
    } else {
      ctx.stroke();
      if (index === current) {
        const radius = Math.max(2, Math.floor(DOT_RADIUS / 3));
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
  }
  drawText(ctx, text, x + dotW + 18, y, dotPx, CJK_FONT);
}

/** Render the face onto a 400x300 canvas: black ink on paper white. */
function composeFace(state, canvas, updated) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalAlpha = 1;
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#000';
  ctx.fillStyle = '#000';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#000';

  const usable = W - 16;

  // Header: title + update stamp + rule.
  setFont(ctx, STAMP_PX, MONO_FONT);
  drawText(ctx, TITLE, 8, 0, STAMP_PX, MONO_FONT);
  const stamp = updated !== undefined ? updated
    : `${String(new Date().getMonth() + 1).padStart(2, '0')}-`
    + `${String(new Date().getDate()).padStart(2, '0')} `
    + `${String(new Date().getHours()).padStart(2, '0')}:`
    + `${String(new Date().getMinutes()).padStart(2, '0')}`;
  const stampW = textWidth(ctx, stamp);
  drawText(ctx, stamp, W - 8 - stampW, 0, STAMP_PX, MONO_FONT);
  const ruleY = metricsOf(ctx, STAMP_PX, MONO_FONT) + 2;
  ctx.fillRect(0, ruleY, W, 1);

  // Measure every fixed row, then fit the hourglass in the leftover.
  const [zh, en] = PHASE_NAMES[state.phase];
  const label = `${zh}  ${en}`;
  const labelPx = fitFont(ctx, label, 40, LABEL_MIN_PX, LABEL_MAX_PX, CJK_FONT, usable);
  const hLabel = metricsOf(ctx, labelPx, CJK_FONT);

  const minutes = minuteText(state);
  const minutePx = fitFont(ctx, minutes, MINUTE_MAX_PX, MINUTE_MIN_PX, MINUTE_MAX_PX,
                           CJK_FONT, usable);
  const hMinute = metricsOf(ctx, minutePx, CJK_FONT);

  const dotPx = STAMP_PX;
  const hDots = 2 * DOT_RADIUS + 2;

  const footer = `今日完成 ${state.cycleTotal} 个番茄`;
  const footerPx = fitFont(ctx, footer, STAMP_PX, 14, STAMP_PX, CJK_FONT, usable);
  const hFooter = metricsOf(ctx, footerPx, CJK_FONT);

  const top = ruleY + 8;
  const bottom = H - 8;
  const available = bottom - top;

  let gap = H_GAP;
  const fixed = hLabel + hMinute + hDots + hFooter;
  let hgH = Math.min(HG_MAX_H, available - fixed - 4 * gap);
  if (hgH < HG_MIN_H) {
    gap = 6;
    hgH = Math.min(HG_MAX_H, available - fixed - 4 * gap);
  }

  const stageH = fixed + Math.max(hgH, 0) + 4 * gap;
  let y = top + Math.max(0, Math.floor((available - stageH) / 2));

  centerText(ctx, label, y, labelPx, CJK_FONT, W);
  y += hLabel + gap;

  drawHourglass(ctx, Math.floor(W / 2), y, Math.max(hgH, HG_MIN_H),
                state.remaining / Math.max(state.phaseSeconds, 1), state.running);
  y += Math.max(hgH, HG_MIN_H) + gap;

  centerText(ctx, minutes, y, minutePx, CJK_FONT, W);
  y += hMinute + gap;

  ctx.lineWidth = 1.5;
  drawDots(ctx, state, y, dotPx, W);
  ctx.lineWidth = 1;
  y += hDots + gap;

  centerText(ctx, footer, y, footerPx, CJK_FONT, W);
}

/* ── timer loop ───────────────────────────────────────────────────────── */

class PomodoroTimer {
  constructor(onChange) {
    this.cfg = loadCfg();
    this.state = loadState();
    this.onChange = onChange;          // called on any state the panel shows
    this.autoAdvance = true;
    this._lastSave = Date.now();
    this._ticker = setInterval(() => this._tick(), 250);
  }

  cfgChanged() {
    this.cfg = loadCfg();
    if (!this.state.running) {
      this.state.phaseSeconds = phaseSecondsFor(this.state.phase, this.cfg);
      this.state.remaining = this.state.phaseSeconds;
    }
    this._emit(true);
  }

  _tick() {
    const state = this.state;
    if (!state.running) return;
    const elapsed = Math.max(0, Math.floor((Date.now() - state.updatedAt) / 1000));
    if (elapsed <= 0) return;
    state.remaining = Math.max(0, state.remaining - elapsed);
    state.updatedAt = Date.now();
    if (state.remaining === 0) {
      advance(state, this.cfg);
      state.running = this.autoAdvance;
      saveState(state);
      this._emit(true);                // phase change: worth a panel push
      return;
    }
    if (Date.now() - this._lastSave > 15000) {
      saveState(state);
      this._lastSave = Date.now();
    }
    this._emit(false);
  }

  _emit(phaseChanged) {
    if (this.onChange) this.onChange(this.state, phaseChanged);
  }

  startPause() {
    const state = this.state;
    if (state.running) {
      state.running = false;
      saveState(state);
    } else {
      if (state.remaining === 0) {
        state.remaining = state.phaseSeconds;
      }
      state.running = true;
      saveState(state);
    }
    this._emit(true);
  }

  reset() {
    this.state.remaining = this.state.phaseSeconds;
    this.state.running = false;
    saveState(this.state);
    this._emit(true);
  }

  skip() {
    skipPhase(this.state, this.cfg);
    this.state.running = this.autoAdvance;
    saveState(this.state);
    this._emit(true);
  }

  wipe() {
    this.state.pomodoroCount = 0;
    this.state.phase = PHASE_WORK;
    this.state.phaseSeconds = phaseSecondsFor(PHASE_WORK, this.cfg);
    this.state.remaining = this.state.phaseSeconds;
    this.state.running = false;
    saveState(this.state);
    this._emit(true);
  }
}
