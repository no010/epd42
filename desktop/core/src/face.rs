//! 400x300 沙漏画面由 Rust 生成——预览和推送到墨水屏走的是同一份代码，
//! 不再有前后端"复刻漂移"。布局与 tools/epd-pomodoro/face.py 等价：
//! 上半沙体贴颈部、沙面随剩余时间下沉；下半沙堆按 `1 - 沙高/瓶高` 收窄，
//! 永远不越出轮廓。字体用 font_data.rs 烘焙的位图（运行时零字体依赖）。

use serde::{Deserialize, Serialize};

use super::font_data::{self, Glyph};
use super::{SCREEN_HEIGHT, SCREEN_WIDTH};

#[derive(Serialize, Deserialize, Clone, Debug)]
// 前端按 Tauri 习惯传 camelCase 键；嵌套结构体字段不做自动转换，这里显式声明。
#[serde(rename_all = "camelCase")]
pub struct FaceState {
    /// 0 = 专注, 1 = 短休息, 2 = 长休息
    pub phase: u8,
    pub phase_seconds: u32,
    pub remaining: u32,
    pub running: bool,
    pub pomodoro_count: u32,
    pub cycle_total: u32,
    pub rounds: u32,
    /// "MM-DD HH:MM"，由前端提供（Rust 侧不引日期库）
    pub stamp: String,
}

pub type Luma = Vec<u8>;

const PHASE_TEXT: [(u8, &str, &str); 3] = [
    (0, "专注", "FOCUS"),
    (1, "短休息", "SHORT BREAK"),
    (2, "长休息", "LONG BREAK"),
];

const HG_ASPECT: f64 = 0.72;
const HG_INSET: i32 = 3;
const HG_BAR_H: i32 = 4;
const DOT_RADIUS: i32 = 7;
const MARGIN_X: i32 = 8;

#[derive(Clone, Copy)]
enum FontKind {
    Cons16,
    Msy16,
    Msy24,
    Msy34,
}

fn font(kind: FontKind) -> (&'static [(char, Glyph)], u16, u16) {
    match kind {
        FontKind::Cons16 => (
            font_data::CONS16,
            font_data::CONS16_METRICS.0,
            font_data::CONS16_METRICS.1,
        ),
        FontKind::Msy16 => (
            font_data::MS16,
            font_data::MS16_METRICS.0,
            font_data::MS16_METRICS.1,
        ),
        FontKind::Msy24 => (
            font_data::MS24,
            font_data::MS24_METRICS.0,
            font_data::MS24_METRICS.1,
        ),
        FontKind::Msy34 => (
            font_data::MS34,
            font_data::MS34_METRICS.0,
            font_data::MS34_METRICS.1,
        ),
    }
}

fn text_width(text: &str, kind: FontKind) -> i32 {
    let (table, _, _) = font(kind);
    text.chars()
        .map(|ch| font_data::lookup(table, ch).map_or(0, |g| g.advance as i32))
        .sum()
}

fn set_ink(buf: &mut Luma, x: i32, y: i32) {
    if x >= 0 && x < SCREEN_WIDTH as i32 && y >= 0 && y < SCREEN_HEIGHT as i32 {
        buf[y as usize * SCREEN_WIDTH + x as usize] = 0;
    }
}

fn rect(buf: &mut Luma, x0: i32, y0: i32, x1: i32, y1: i32) {
    for y in y0.min(y1)..=y0.max(y1) {
        for x in x0.min(x1)..=x0.max(x1) {
            set_ink(buf, x, y);
        }
    }
}

fn set_paper(buf: &mut Luma, x: i32, y: i32) {
    if x >= 0 && x < SCREEN_WIDTH as i32 && y >= 0 && y < SCREEN_HEIGHT as i32 {
        buf[y as usize * SCREEN_WIDTH + x as usize] = 255;
    }
}

/// 粗线段（正方形笔迹），用于小时漏的 3px 轮廓。
fn line(buf: &mut Luma, x0: i32, y0: i32, x1: i32, y1: i32, width: i32) {
    let dx = (x1 - x0) as f64;
    let dy = (y1 - y0) as f64;
    let steps = x1.abs_diff(x0).max(y1.abs_diff(y0)) as i32;
    let r = width / 2;
    for s in 0..=steps {
        let t = s as f64 / steps as f64;
        let x = (x0 as f64 + t * dx).round() as i32;
        let y = (y0 as f64 + t * dy).round() as i32;
        rect(buf, x - r, y - r, x + r, y + r);
    }
}

/// 扫描线填充三角形（半开区间避免共边重复、水平边单独处理）。
fn fill_triangle(buf: &mut Luma, x0: i32, y0: i32, x1: i32, y1: i32, x2: i32, y2: i32) {
    let min_y = y0.min(y1).min(y2);
    let max_y = y0.max(y1).max(y2);
    for y in min_y..=max_y {
        let mut xs: Vec<i32> = Vec::new();
        for (ax, ay, bx, by) in [(x0, y0, x1, y1), (x1, y1, x2, y2), (x2, y2, x0, y0)] {
            if ay == by {
                if y == ay {
                    xs.push(ax);
                    xs.push(bx);
                }
            } else if (y >= ay && y < by) || (y >= by && y < ay) {
                let t = (y - ay) as f64 / (by - ay) as f64;
                xs.push((ax as f64 + t * (bx - ax) as f64).round() as i32);
            }
        }
        xs.sort_unstable();
        if let (Some(&l), Some(&r)) = (xs.first(), xs.last()) {
            rect(buf, l, y, r, y);
        }
    }
}

fn circle_fill(buf: &mut Luma, cx: i32, cy: i32, r: i32) {
    for y in -r..=r {
        let half = ((r * r - y * y) as f64).sqrt();
        let x0 = cx - half.floor() as i32;
        let x1 = cx + half.floor() as i32;
        rect(buf, x0, cy + y, x1, cy + y);
    }
}

/// 空心圆 = 填满后挖掉半径 r-2 的白色圆。
fn circle_outline(buf: &mut Luma, cx: i32, cy: i32, r: i32) {
    circle_fill(buf, cx, cy, r);
    if r >= 2 {
        circle_paper(buf, cx, cy, r - 2);
    }
}

fn circle_paper(buf: &mut Luma, cx: i32, cy: i32, r: i32) {
    for y in -r..=r {
        let half = ((r * r - y * y) as f64).sqrt();
        let x0 = cx - half.floor() as i32;
        let x1 = cx + half.floor() as i32;
        for x in x0..=x1 {
            set_paper(buf, x, cy + y);
        }
    }
}

fn blit_glyph(buf: &mut Luma, x: i32, y: i32, glyph: &Glyph) {
    let row_bytes = (glyph.w as usize + 7) / 8;
    for row in 0..glyph.h as usize {
        for col in 0..glyph.w as usize {
            let byte = glyph.bits[row * row_bytes + col / 8];
            if byte & (0x80 >> (col % 8)) != 0 {
                set_ink(buf, x + col as i32, y + row as i32);
            }
        }
    }
}

fn draw_text(buf: &mut Luma, x: i32, y: i32, text: &str, kind: FontKind) -> i32 {
    let (table, _, _) = font(kind);
    let mut cursor = x;
    for ch in text.chars() {
        if let Some(glyph) = font_data::lookup(table, ch) {
            blit_glyph(buf, cursor + glyph.x0 as i32, y + glyph.y0 as i32, glyph);
            cursor += glyph.advance as i32;
        }
    }
    cursor - x
}

fn draw_centered(buf: &mut Luma, y: i32, text: &str, kind: FontKind) {
    let x = ((SCREEN_WIDTH as i32 - text_width(text, kind)) / 2).max(MARGIN_X);
    draw_text(buf, x, y, text, kind);
}

// ── 沙漏 ────────────────────────────────────────────────────────────────

fn draw_hourglass(buf: &mut Luma, cx: i32, y_top: i32, height: i32, remaining: f64, running: bool) {
    let frac = remaining.clamp(0.0, 1.0);
    let width = (height as f64 * HG_ASPECT).round() as i32;
    let x0 = cx - width / 2;
    let x1 = x0 + width;
    let y0 = y_top;
    let y1 = y_top + height;
    let ym = y0 + height / 2;
    let frame_half = width / 2 + 4;
    let sx0 = x0 + HG_INSET;
    let sx1 = x1 - HG_INSET;
    let half_w = (sx1 - sx0) as f64 / 2.0;
    let top_h = ((ym - HG_INSET) - (y0 + HG_INSET)) as f64;
    let bot_h = ((y1 - HG_INSET) - (ym + HG_INSET)) as f64;

    // 上半：沙贴颈部，沙面下沉（顶部先空）
    let sand_h = (frac * top_h) as i32;
    if sand_h >= 1 {
        let surface_y = ym - HG_INSET - sand_h;
        let hw = half_w * (sand_h as f64 / top_h);
        fill_triangle(
            buf,
            cx,
            ym - HG_INSET,
            (cx as f64 - hw).round() as i32,
            surface_y,
            (cx as f64 + hw).round() as i32,
            surface_y,
        );
    }

    // 下半：沙堆从底边堆积，顶面宽度 = half_w * (1 - 沙高/瓶高)
    let pile_h = ((1.0 - frac) * bot_h) as i32;
    if pile_h >= 1 {
        let surface_y = y1 - HG_INSET - pile_h;
        let hw = half_w * (1.0 - pile_h as f64 / bot_h);
        let hl = (cx as f64 - hw).round() as i32;
        let hr = (cx as f64 + hw).round() as i32;
        fill_triangle(buf, hl, surface_y, hr, surface_y, sx1, y1 - HG_INSET);
        fill_triangle(buf, hl, surface_y, sx0, y1 - HG_INSET, sx1, y1 - HG_INSET);
    }

    // 沙流
    if running && frac > 0.0 && frac < 1.0 {
        let pile_top = y1 - HG_INSET - pile_h;
        if pile_top > ym {
            rect(buf, cx - 1, ym, cx + 1, pile_top);
        }
    }

    line(buf, x0, y0, x1, y0, 3);
    line(buf, x1, y0, cx, ym, 3);
    line(buf, cx, ym, x0, y0, 3);
    line(buf, cx, ym, x0, y1, 3);
    line(buf, x0, y1, x1, y1, 3);
    line(buf, x1, y1, cx, ym, 3);
    rect(buf, cx - frame_half, y0 - HG_BAR_H, cx + frame_half, y0);
    rect(buf, cx - frame_half, y1, cx + frame_half, y1 + HG_BAR_H);
}

fn draw_dots(buf: &mut Luma, state: &FaceState, y: i32) {
    let rounds = state.rounds.max(1);
    let filled = state.pomodoro_count.min(rounds);
    let current = if state.phase == 0 && filled < rounds {
        filled
    } else {
        u32::MAX
    };
    let text = if state.phase == 0 {
        format!("第 {}/{} 个", state.pomodoro_count, rounds)
    } else {
        format!("已完成 {}/{}", state.pomodoro_count, rounds)
    };
    let gap = 12;
    let dot_w = rounds as i32 * 2 * DOT_RADIUS + (rounds as i32 - 1) * gap;
    let group_w = dot_w + 18 + text_width(&text, FontKind::Msy16);
    let x = ((SCREEN_WIDTH as i32 - group_w) / 2).max(MARGIN_X);
    let cy = y + DOT_RADIUS;
    for i in 0..rounds as i32 {
        let cx2 = x + DOT_RADIUS + i * (2 * DOT_RADIUS + gap);
        if (i as u32) < filled {
            circle_fill(buf, cx2, cy, DOT_RADIUS);
        } else {
            circle_outline(buf, cx2, cy, DOT_RADIUS);
            if i as u32 == current {
                circle_fill(buf, cx2, cy, 2);
            }
        }
    }
    draw_text(buf, x + dot_w + 18, y, &text, FontKind::Msy16);
}

fn minute_text(state: &FaceState) -> String {
    let total_min = ((state.phase_seconds.max(1) + 59) / 60).max(1);
    let left_min = (state.remaining + 59) / 60;
    let base = format!("剩 {left_min} / {total_min} 分钟");
    if state.running {
        base
    } else {
        format!("已暂停 · {base}")
    }
}

// ── 布局：唯一的几何来源，渲染与测试都从这里取值 ──────────────────────────

pub struct Geometry {
    pub cx: i32,
    pub hg_y: i32,
    pub hg_h: i32,
}

struct Layout {
    cx: i32,
    rule_y: i32,
    label_y: i32,
    hg_y: i32,
    hg_h: i32,
    minute_y: i32,
    dots_y: i32,
    footer_y: i32,
}

fn layout() -> Layout {
    let (_, c_asc, c_desc) = font(FontKind::Cons16);
    let (_, l_asc, l_desc) = font(FontKind::Msy34);
    let (_, m_asc, m_desc) = font(FontKind::Msy24);
    let (_, f_asc, f_desc) = font(FontKind::Msy16);
    let rule_y = c_asc as i32 + c_desc as i32 + 2;
    let label_h = l_asc as i32 + l_desc as i32;
    let minute_h = m_asc as i32 + m_desc as i32;
    let footer_h = f_asc as i32 + f_desc as i32;
    let dots_h = 2 * DOT_RADIUS + 2;

    let top = rule_y + 8;
    let bottom = SCREEN_HEIGHT as i32 - 8;
    let available = bottom - top;

    let mut gap = 10;
    let fixed = label_h + minute_h + dots_h + footer_h;
    let mut hg_h = available - fixed - 4 * gap;
    if hg_h < 80 {
        gap = 6;
        hg_h = available - fixed - 4 * gap;
    }
    let hg_h = hg_h.clamp(80, 170);
    let stage = fixed + hg_h + 4 * gap;

    let label_y = top + (available - stage) / 2;
    let hg_y = label_y + label_h + gap;
    let minute_y = hg_y + hg_h + gap;
    let dots_y = minute_y + minute_h + gap;
    let footer_y = dots_y + dots_h + gap;

    Layout {
        cx: SCREEN_WIDTH as i32 / 2,
        rule_y,
        label_y,
        hg_y,
        hg_h,
        minute_y,
        dots_y,
        footer_y,
    }
}

pub fn geometry() -> Geometry {
    let lay = layout();
    Geometry {
        cx: lay.cx,
        hg_y: lay.hg_y,
        hg_h: lay.hg_h,
    }
}

pub fn render(state: &FaceState) -> Luma {
    let mut buf = vec![255u8; SCREEN_WIDTH * SCREEN_HEIGHT];
    let lay = layout();

    draw_text(&mut buf, MARGIN_X, 0, "POMODORO", FontKind::Cons16);
    if !state.stamp.is_empty() {
        let w = text_width(&state.stamp, FontKind::Cons16);
        draw_text(
            &mut buf,
            SCREEN_WIDTH as i32 - MARGIN_X - w,
            0,
            &state.stamp,
            FontKind::Cons16,
        );
    }
    rect(&mut buf, 0, lay.rule_y, SCREEN_WIDTH as i32 - 1, lay.rule_y);

    let (_, zh, en) = PHASE_TEXT[state.phase.clamp(0, 2) as usize];
    draw_centered(&mut buf, lay.label_y, &format!("{zh}  {en}"), FontKind::Msy34);

    let remaining_frac = state.remaining as f64 / state.phase_seconds.max(1) as f64;
    draw_hourglass(&mut buf, lay.cx, lay.hg_y, lay.hg_h, remaining_frac, state.running);

    draw_centered(&mut buf, lay.minute_y, &minute_text(state), FontKind::Msy24);
    draw_dots(&mut buf, state, lay.dots_y);
    draw_centered(&mut buf, lay.footer_y, &format!("今日完成 {} 个番茄", state.cycle_total), FontKind::Msy16);

    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn face_state_deserializes_from_camel_case() {
        let json = r#"{"phase":0,"phaseSeconds":1500,"remaining":750,"running":true,"pomodoroCount":2,"cycleTotal":7,"rounds":4,"stamp":"09-04 16:00"}"#;
        let st: FaceState =
            serde_json::from_str(json).expect("camelCase JSON 应能反序列化");
        assert_eq!(st.phase_seconds, 1500);
        assert_eq!(st.pomodoro_count, 2);
        assert_eq!(st.cycle_total, 7);
        assert!(st.running);
        assert_eq!(st.stamp, "09-04 16:00");
    }
    fn state(remaining_secs: u32, running: bool) -> FaceState {
        FaceState {
            phase: 0,
            phase_seconds: 1500,
            remaining: remaining_secs,
            running,
            pomodoro_count: 2,
            cycle_total: 7,
            rounds: 4,
            stamp: "09-04 16:00".to_string(),
        }
    }

    fn ink_at(buf: &Luma, x: i32, y: i32) -> bool {
        if x < 0 || x >= SCREEN_WIDTH as i32 || y < 0 || y >= SCREEN_HEIGHT as i32 {
            return false;
        }
        buf[y as usize * SCREEN_WIDTH + x as usize] < 128
    }

    fn center_ink(buf: &Luma, y: i32, cx: i32) -> usize {
        (cx - 10..=cx + 10).filter(|&x| ink_at(buf, x, y)).count()
    }

    #[test]
    fn render_dims_and_content() {
        let buf = render(&state(750, true));
        assert_eq!(buf.len(), SCREEN_WIDTH * SCREEN_HEIGHT);
        assert!(buf.iter().any(|&p| p < 128), "画面有墨");
        assert!(buf.iter().any(|&p| p > 127), "画面有纸");
    }

    #[test]
    fn top_bulb_drains_downward() {
        // 半程：顶部近横梁处应是空的，沙贴着颈部
        let lay = layout();
        let buf = render(&state(750, true));
        let ym = lay.hg_y + lay.hg_h / 2;
        let void_row = lay.hg_y + 8;
        let sand_row = ym - 6;
        assert_eq!(center_ink(&buf, void_row, lay.cx), 0, "半程顶部应已空");
        assert!(center_ink(&buf, sand_row, lay.cx) > 0, "沙应贴着颈部");
    }

    #[test]
    fn sand_stays_inside_outline() {
        // 任何沙量下，瓶内的墨都不得越过轮廓（含 3px 描边容差）
        let lay = layout();
        let y0 = lay.hg_y;
        let y1 = lay.hg_y + lay.hg_h;
        let ym = y0 + lay.hg_h / 2;
        let half = (lay.hg_h as f64 * HG_ASPECT).round() / 2.0;
        for frac in [0.95f64, 0.7, 0.3, 0.05] {
            let buf = render(&state((frac * 1500.0) as u32, true));
            for y in (y0 + 1)..y1 {
                let t = if y < ym {
                    (ym - y) as f64 / (ym - y0) as f64
                } else if y > ym {
                    (y - ym) as f64 / (y1 - ym) as f64
                } else {
                    0.0
                };
                let limit = (half * t) + 3.0;
                for x in (lay.cx - 90)..=(lay.cx + 90) {
                    if ink_at(&buf, x, y) && ((x - lay.cx).abs() as f64) > limit {
                        panic!(
                            "frac {frac}: 墨在 ({x},{y}) 越出轮廓（允许 ±{limit:.1}）"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn paused_and_breaks_render() {
        let mut full = state(1500, false);
        full.phase = 2;
        full.remaining = 900;
        full.pomodoro_count = 4;
        full.cycle_total = 9;
        full.rounds = 4;
        let buf = render(&full);
        assert_eq!(buf.len(), SCREEN_WIDTH * SCREEN_HEIGHT);
        assert!(has_substantial_ink(&buf));
    }

    fn has_substantial_ink(buf: &Luma) -> bool {
        // 粗略检查：文本区存在大量墨点即可（布局正确性由 drain/containment 保证）
        let ink = buf.iter().filter(|&&p| p < 128).count();
        ink > 500 && ink < 60000
    }
}