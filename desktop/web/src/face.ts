// 400x300 沙漏画面（移植自 tools/epd-pomodoro/face.py，含两处修正：
// 上半沙体贴颈部、沙面从上往下沉；下半沙堆永远不越出轮廓）。

import { PHASE_NAMES, PomodoroState, mmss } from "./timer.js";

export const W = 400;
export const H = 300;
const MARGIN_X = 8;
const TITLE = "POMODORO";
const HG_ASPECT = 0.72;
const HG_INSET = 3;
const HG_BAR_H = 4;
const DOT_RADIUS = 7;

interface Ctx2D {
  fillText: (text: string, x: number, y: number) => void;
  measureText: (text: string) => { width: number };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function stampNow(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function centerX(ctx: any, text: string, font: string): number {
  ctx.font = font;
  return Math.max(0, Math.floor((W - ctx.measureText(text).width) / 2));
}

/** 沙漏本体：`remaining` 为剩余比例，`running` 决定是否画沙流。 */
export function drawHourglass(
  ctx: any,
  cx: number,
  yTop: number,
  height: number,
  remaining: number,
  running: boolean,
): void {
  const frac = clamp(remaining, 0, 1);
  const width = Math.round(height * HG_ASPECT);
  const x0 = cx - Math.floor(width / 2);
  const x1 = x0 + width;
  const y0 = yTop;
  const y1 = yTop + height;
  const ym = y0 + Math.floor(height / 2);
  const frameHalf = Math.ceil(width / 2) + 4;

  // 沙体内缩，避免盖住轮廓描边
  const sx0 = x0 + HG_INSET;
  const sx1 = x1 - HG_INSET;
  const halfW = (sx1 - sx0) / 2;
  const topH = (ym - HG_INSET) - (y0 + HG_INSET);
  const botH = (y1 - HG_INSET) - (ym + HG_INSET);

  ctx.fillStyle = "#000";

  // 上半：沙体贴颈部，沙面从顶缘下沉（顶部先空）
  const sandH = Math.floor(frac * topH);
  if (sandH >= 1) {
    const surfaceY = ym - HG_INSET - sandH;
    const hw = halfW * (sandH / topH);
    ctx.beginPath();
    ctx.moveTo(cx - hw, surfaceY);
    ctx.lineTo(cx + hw, surfaceY);
    ctx.lineTo(cx, ym - HG_INSET);
    ctx.closePath();
    ctx.fill();
  }

  // 下半：沙堆从底边堆积，顶面宽度 = halfW * (1 - 沙高/瓶高)
  const pileH = Math.floor((1 - frac) * botH);
  if (pileH >= 1) {
    const surfaceY = y1 - HG_INSET - pileH;
    const hw = halfW * (1 - pileH / botH);
    ctx.beginPath();
    ctx.moveTo(cx - hw, surfaceY);
    ctx.lineTo(cx + hw, surfaceY);
    ctx.lineTo(sx1, y1 - HG_INSET);
    ctx.lineTo(sx0, y1 - HG_INSET);
    ctx.closePath();
    ctx.fill();
  }

  // 沙流（运行时）
  if (running && frac > 0 && frac < 1) {
    const pileTop = y1 - HG_INSET - pileH;
    ctx.fillRect(cx - 1, ym, 2, Math.max(0, pileTop - ym));
  }

  // 轮廓 + 上下横梁（最后画，盖住沙边保证边缘干净）
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#000";
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y0);
  ctx.lineTo(cx, ym);
  ctx.closePath();
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(cx, ym);
  ctx.lineTo(x0, y1);
  ctx.lineTo(x1, y1);
  ctx.closePath();
  ctx.stroke();
  ctx.fillRect(cx - frameHalf, y0 - HG_BAR_H, frameHalf * 2, HG_BAR_H);
  ctx.fillRect(cx - frameHalf, y1, frameHalf * 2, HG_BAR_H);
}

/** 本轮番茄圆点 + 计数，整体居中。 */
function drawDots(ctx: any, state: PomodoroState, y: number): void {
  const font = "16px 'Microsoft YaHei', sans-serif";
  const filled = Math.max(0, Math.min(state.pomodoroCount, state.rounds));
  const current = state.phase === "work" ? filled : -1;
  let text = `第 ${state.pomodoroCount}/${state.rounds} 个`;
  if (state.phase !== "work") text = `已完成 ${state.pomodoroCount}/${state.rounds}`;
  ctx.font = font;
  const gap = 12;
  const dotW = state.rounds * 2 * DOT_RADIUS + (state.rounds - 1) * gap;
  const groupW = dotW + 18 + ctx.measureText(text).width;
  const x = Math.max(MARGIN_X, Math.floor((W - groupW) / 2));
  const cy = y + DOT_RADIUS;
  ctx.fillStyle = "#000";
  for (let i = 0; i < state.rounds; i += 1) {
    const cx2 = x + DOT_RADIUS + i * (2 * DOT_RADIUS + gap);
    ctx.beginPath();
    ctx.arc(cx2, cy, DOT_RADIUS, 0, Math.PI * 2);
    if (i < filled) {
      ctx.fill();
    } else {
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#000";
      ctx.stroke();
      if (i === current) {
        ctx.beginPath();
        ctx.arc(cx2, cy, Math.max(2, Math.floor(DOT_RADIUS / 3)), 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
  ctx.fillStyle = "#000";
  ctx.font = font;
  ctx.textBaseline = "top";
  ctx.fillText(text, x + dotW + 18, y);
}

export function minuteText(state: PomodoroState): string {
  const totalMin = Math.max(1, Math.ceil(state.phaseSeconds / 60));
  const leftMin = Math.max(0, Math.ceil(state.remaining / 60));
  let text = `剩 ${leftMin} / ${totalMin} 分钟`;
  if (!state.running) text = `已暂停 · ${text}`;
  return text;
}

/** 画整屏：与 EPD 上的 face.py 布局一致。 */
export function drawFace(canvas: HTMLCanvasElement, state: PomodoroState, updated?: string): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#000";
  ctx.textBaseline = "top";

  // 标题 + 时间戳 + 分隔线
  const stampFont = "16px Consolas, monospace";
  ctx.font = stampFont;
  ctx.fillText(TITLE, MARGIN_X, 0);
  const stamp = updated ?? stampNow();
  ctx.fillText(stamp, W - MARGIN_X - ctx.measureText(stamp).width, 0);
  ctx.fillRect(0, 18, W, 1);

  // 阶段标签
  const [zh, en] = PHASE_NAMES[state.phase];
  const label = `${zh}  ${en}`;
  const labelFont = "34px 'Microsoft YaHei', 'PingFang SC', sans-serif";
  ctx.fillText(label, centerX(ctx, label, labelFont), 28);

  // 沙漏：占中间一大块，尺寸固定居中
  drawHourglass(ctx, W / 2, 78, 122, state.remaining / Math.max(state.phaseSeconds, 1), state.running);

  // 分钟文字
  const minutes = minuteText(state);
  const minuteFont = "24px 'Microsoft YaHei', 'PingFang SC', sans-serif";
  ctx.fillText(minutes, centerX(ctx, minutes, minuteFont), 212);

  // 圆点 + 计数
  drawDots(ctx, state, 248);

  // 页脚
  const footer = `今日完成 ${state.cycleTotal} 个番茄`;
  const footerFont = "16px 'Microsoft YaHei', 'PingFang SC', sans-serif";
  ctx.fillText(footer, centerX(ctx, footer, footerFont), 276);
}

/** 提取 400x300 灰度字节（>127 = 白纸），给 Rust 侧打包。 */
export function lumaBytes(canvas: HTMLCanvasElement): Uint8Array {
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("无法读取画布");
  const image = ctx.getImageData(0, 0, W, H);
  const out = new Uint8Array(W * H);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = image.data[i * 4] > 127 ? 255 : 0;
  }
  return out;
}

export type { Ctx2D };