/* LLM usage cards: the layout of tools/epd-monitor/render.py, driven by a
 * small in-page editor (localStorage).  A browser page cannot reach the
 * logged-in consoles the Python tool scrapes, so the numbers here are typed
 * by the user or copied from `epd_monitor.py status`.
 *
 * Every percentage reads as usage - the convention all cards share.
 */
'use strict';

const CARDS_STORE_KEY = 'epd42.web.cards';
const MAX_ITEMS = 3;
const CARD_MARGIN_X = 8;
const CARD_MIN_FONT_PX = 14;
const CARD_MAX_FONT_PX = 40;
const CARD_STAMP_PX = 16;
const BAR_MIN_PX = 4;
const CURRENCY = { CNY: '¥', RMB: '¥', USD: '$' };

const DEMO_CARDS = [
  { planName: 'DeepSeek', quotaUsed: 0, quotaTotal: 0, unit: 'CNY',
    balanceYuan: 52.74, note: 'tdy ¥5.95 tok 69.6M F100%/P0%',
    extra: '/ ¥278', showBar: false },
  { planName: 'Kimi Allegretto', quotaUsed: 6, quotaTotal: 100, unit: '%',
    balanceYuan: 0, note: 'Mo 28% Wk 40% 7d rst 09-05 exp 09-25',
    extra: 'rst 14:22', showBar: true },
  { planName: 'Aliyun TokenPlan', quotaUsed: 7, quotaTotal: 100, unit: '%',
    balanceYuan: 0, note: 'rst 09-07 10:11 19d', extra: '', showBar: true },
];

function loadCards() {
  try {
    const data = JSON.parse(localStorage.getItem(CARDS_STORE_KEY));
    if (Array.isArray(data) && data.length) return data;
  } catch (e) { /* defaults below */ }
  return JSON.parse(JSON.stringify(DEMO_CARDS));
}

function saveCards(cards) {
  localStorage.setItem(CARDS_STORE_KEY, JSON.stringify(cards));
}

function balanceText(item) {
  const cents = Math.round(item.balanceYuan * 100);
  const amount = `${Math.floor(cents / 100).toLocaleString('en-US')}.`
               + `${String(Math.abs(cents % 100)).padStart(2, '0')}`;
  const symbol = CURRENCY[item.unit.toUpperCase()];
  return symbol ? `${symbol}${amount}` : `${amount} ${item.unit}`.trim();
}

/** The card's metrics line: quota and balance, no notes (render.py). */
function usageLine(item) {
  const parts = [];
  if (item.quotaTotal > 0) {
    if (item.unit === '%') {
      parts.push(`${(item.quotaUsed / item.quotaTotal * 100).toFixed(1)}%`);
    } else {
      parts.push(`${item.quotaUsed.toLocaleString('en-US')} / `
                 + `${item.quotaTotal.toLocaleString('en-US')} ${item.unit}`);
    }
  } else if (item.quotaUsed > 0) {
    parts.push(`${item.quotaUsed.toLocaleString('en-US')} ${item.unit} used`);
  }
  if (item.balanceYuan > 0) parts.push(balanceText(item));
  let line = parts.join('   ');
  if (item.extra) line = line ? `${line} ${item.extra}` : item.extra;
  return line || 'no data';
}

function isAscii(text) {
  for (const ch of text) if (ch.codePointAt(0) >= 128) return false;
  return true;
}

function hasBar(item) {
  return item.quotaTotal > 0 && item.showBar !== false;
}

function fitText(ctx, text, maxPx) {
  if (!text || ctx.measureText(text).width <= maxPx) return text;
  while (text && ctx.measureText(`${text}…`).width > maxPx) text = text.slice(0, -1);
  return text ? `${text}…` : '';
}

/** Render the SUB MONITOR frame onto a 400x300 canvas. */
function composeCards(items, canvas, updated) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.strokeStyle = '#000';
  ctx.fillStyle = '#000';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#000';

  const shown = items.slice(0, MAX_ITEMS);
  const usable = W - 2 * CARD_MARGIN_X;
  const stampFont = CARD_STAMP_PX;
  setFont(ctx, stampFont, MONO_FONT);
  const stampH = metricsOf(ctx, stampFont, MONO_FONT);

  // Title row: title left, update stamp right, rule below.
  drawText(ctx, 'SUB MONITOR', CARD_MARGIN_X, 0, stampFont, MONO_FONT);
  const stamp = updated !== undefined ? updated
    : `${String(new Date().getMonth() + 1).padStart(2, '0')}-`
    + `${String(new Date().getDate()).padStart(2, '0')} `
    + `${String(new Date().getHours()).padStart(2, '0')}:`
    + `${String(new Date().getMinutes()).padStart(2, '0')}`;
  setFont(ctx, stampFont, MONO_FONT);
  drawText(ctx, stamp, W - CARD_MARGIN_X - textWidth(ctx, stamp), 0, stampFont, MONO_FONT);
  const ruleRow = stampH + 2;
  ctx.fillRect(0, ruleRow, W, 1);

  const count = Math.max(1, shown.length);
  const contentTop = ruleRow + 4;
  const contentBottom = H - 4;
  const cardH = Math.floor((contentBottom - contentTop) / count);
  const pad = Math.max(2, Math.floor(cardH / 10));

  // Shrink the body font until the usage lines and row stack fit.
  let fontPx = Math.max(CARD_MIN_FONT_PX,
                        Math.min(CARD_MAX_FONT_PX, Math.floor(cardH / 4)));
  const rows = (px) => {
    const textH = metricsOf(ctx, px, CJK_FONT);
    const noteH = stampH;
    const barH = Math.max(BAR_MIN_PX, Math.floor(cardH / 10));
    const stack = textH * 2 + noteH + barH;
    const gap = Math.max(2, Math.floor((cardH - pad - stack) / 3));
    return { textH, noteH, barH, gap,
             usageRow: textH + gap,
             noteRow: textH + gap + textH + gap,
             barRow: textH + gap + textH + gap + noteH + gap };
  };
  setFont(ctx, fontPx, MONO_FONT);
  while (fontPx > CARD_MIN_FONT_PX
         && shown.some((it) => {
           setFont(ctx, fontPx, isAscii(usageLine(it)) ? MONO_FONT : CJK_FONT);
           return textWidth(ctx, usageLine(it)) > usable;
         })) {
    fontPx -= 2;
  }
  const { textH, noteH, barH, gap, usageRow, noteRow, barRow } = rows(fontPx);

  shown.forEach((item, index) => {
    const top = contentTop + index * cardH;

    // Plan name.
    setFont(ctx, fontPx, CJK_FONT);
    drawText(ctx, fitText(ctx, item.planName, usable), CARD_MARGIN_X, top,
             fontPx, CJK_FONT);

    // Metrics line: one font per line (mono for pure ASCII).
    const usage = usageLine(item);
    const usageFamily = isAscii(usage) ? MONO_FONT : CJK_FONT;
    setFont(ctx, fontPx, usageFamily);
    drawText(ctx, fitText(ctx, usage, usable), CARD_MARGIN_X, top + usageRow,
             fontPx, usageFamily);

    // Note line.
    const note = item.note || '';
    if (note) {
      const noteFamily = isAscii(note) ? MONO_FONT : CJK_FONT;
      setFont(ctx, stampFont, noteFamily);
      drawText(ctx, fitText(ctx, note, usable), CARD_MARGIN_X, top + noteRow,
               stampFont, noteFamily);
    }

    // Progress bar: the fill is the usage share.
    if (hasBar(item)) {
      const barTop = top + barRow;
      const barBottom = Math.min(barTop + barH, top + cardH - 1);
      ctx.lineWidth = 1;
      ctx.strokeRect(CARD_MARGIN_X + 0.5, barTop + 0.5, usable - 1,
                     barBottom - barTop - 1);
      const inner = usable - 2;
      const width = Math.floor(inner * Math.min(Math.max(item.quotaUsed, 0),
                                                item.quotaTotal) / item.quotaTotal);
      if (width > 0) {
        ctx.fillRect(CARD_MARGIN_X + 1, barTop + 1, width,
                     barBottom - barTop - 2);
      }
    }
  });
}
