"""Compose the 400x300 Pomodoro e-ink face on the PC.

Wire convention is the epd-monitor one (see render.py): bit 1 = paper white,
bit 0 = black ink, MSB is the leftmost pixel, one plane is 50 * 300 = 15000
bytes. The face is pure black on paper; for a tri-colour panel the monitor app
sends an all-paper red plane, and this tool does the same through the shared
``render.pack_planes``.

The screen is an e-ink panel that refreshes every few minutes, so seconds are
meaningless here: progress is drawn as an hourglass (top sand drains, bottom
sand piles up, a stream falls while the timer runs) and the only number is the
remaining *minutes*.  Every row is measured from the fonts actually loaded, so
a font with an odd line height can never push the footer off the panel - it
only makes the hourglass a little smaller.
"""

from __future__ import annotations

import math
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from render import (
    MARGIN_X,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    FontError,
    select_fonts,
)
from state import PHASE_NAMES, PHASE_WORK, PomodoroState

TITLE = "POMODORO"
STAMP_PX = 16
LABEL_MIN_PX, LABEL_MAX_PX = 26, 44
MINUTE_MIN_PX, MINUTE_MAX_PX = 18, 28
DOT_RADIUS = 7
H_GAP = 10  # vertical gutters between the rows
HG_MIN_H, HG_MAX_H = 80, 170
HG_ASPECT = 0.72  # hourglass width / height
HG_INSET = 3  # keep the sand inside the outline
HG_BAR_H = 4  # frame bars above and below the bulbs


def _metrics(font) -> int:
    """Line height this font actually produces."""
    box = font.getbbox("AgjyQ")
    return max(box[3] - box[1], 1) + 2


def _truetype(path: str, size: int):
    return ImageFont.truetype(path, size)


def _fit_font(
    draw, path: str, text: str, start: int, min_px: int, max_px: int, usable_px: float
):
    """Largest font <= ``start`` whose text still fits ``usable_px``."""
    size = max(min_px, min(max_px, start))
    font = _truetype(path, size)
    while size > min_px and draw.textlength(text, font=font) > usable_px:
        size -= 2
        font = _truetype(path, size)
    return font


def _center_x(draw, text: str, font) -> int:
    return max(0, int((SCREEN_WIDTH - draw.textlength(text, font=font)) / 2))


def minute_text(state: PomodoroState) -> str:
    """The one number on screen: whole minutes, ceil so the last minute lasts."""
    total_min = max(1, math.ceil(state.phase_seconds / 60))
    left_min = max(0, math.ceil(state.remaining / 60))
    text = f"剩 {left_min} / {total_min} 分钟"
    if not state.running:
        text = f"已暂停 · {text}"
    return text


def _draw_hourglass(
    draw, cx: int, y_top: int, height: int, remaining: float, running: bool
) -> None:
    """Progress as an hourglass at the horizontal centre ``cx``.

    ``remaining`` is the fraction of the phase still left: it is the depth of
    sand in the top bulb; the bottom pile grows with the spent share.  While
    the timer runs a thin stream falls from the neck to the pile.
    """
    frac = max(0.0, min(1.0, remaining))
    width = int(height * HG_ASPECT)
    x0, x1 = cx - width // 2, cx + width // 2
    y0, y1 = y_top, y_top + height
    ym = y0 + height // 2
    frame_half = width // 2 + 4

    # ── sand, inset so it never covers the outline ─────────────────────────
    sx0, sx1 = x0 + HG_INSET, x1 - HG_INSET
    half_w = (sx1 - sx0) / 2
    top_h = (ym - HG_INSET) - (y0 + HG_INSET)
    bot_h = (y1 - HG_INSET) - (ym + HG_INSET)

    # Both bulbs are cones narrowing toward the neck.  A cut h pixels above
    # the neck therefore has half-width half_w * h / bulb_height.  The cut and
    # its width use the same integer height, so the sand edges sit exactly on
    # the inset sides and never poke past the outline.
    #
    # Top bulb: sand drains OUT through the neck, so it stays packed against
    # the neck and its surface sinks from the top edge downward - the void
    # appears at the top and grows.  (Drawing the sand stuck to the top edge
    # instead is the classic mistake: it leaves a floating gap above the neck.)
    sand_h = int(frac * top_h)  # depth of sand measured up from the neck
    if sand_h >= 1:
        y_surface = ym - HG_INSET - sand_h
        hw = half_w * (sand_h / top_h)
        draw.polygon(
            [
                (cx - hw, y_surface),
                (cx + hw, y_surface),
                (cx, ym - HG_INSET),
            ],
            fill=0,
        )

    pile_h = int((1 - frac) * bot_h)  # sand already down below
    if pile_h >= 1:
        y_cut = y1 - HG_INSET - pile_h
        hw = half_w * (1 - pile_h / bot_h)
        draw.polygon(
            [
                (cx - hw, y_cut),
                (cx + hw, y_cut),
                (sx1, y1 - HG_INSET),
                (sx0, y1 - HG_INSET),
            ],
            fill=0,
        )

    if running and 0 < frac < 1:
        pile_top = y1 - HG_INSET - pile_h
        draw.line((cx, ym, cx, max(ym, pile_top - 1)), fill=0, width=2)

    # ── outline on top of the sand so the edges stay crisp ─────────────────
    draw.polygon([(x0, y0), (x1, y0), (cx, ym)], outline=0, width=3)
    draw.polygon([(cx, ym), (x0, y1), (x1, y1)], outline=0, width=3)
    draw.rectangle((cx - frame_half, y0 - HG_BAR_H, cx + frame_half, y0), fill=0)
    draw.rectangle((cx - frame_half, y1, cx + frame_half, y1 + HG_BAR_H), fill=0)


def _draw_dots(draw, state: PomodoroState, y: int, dot_font) -> None:
    """Cycle dots plus the cycle counter, drawn as one centered group.

    A filled dot is a finished work session; the hollow dot with a solid
    centre marks the session currently running; empty dots are what is left.
    """
    filled = max(0, min(state.pomodoro_count, state.rounds))
    current = filled if state.phase == PHASE_WORK else -1
    text = f"第 {state.pomodoro_count}/{state.rounds} 个"
    gap = 12
    dot_w = state.rounds * 2 * DOT_RADIUS + (state.rounds - 1) * gap
    group_w = dot_w + 18 + draw.textlength(text, font=dot_font)
    x = max(MARGIN_X, int((SCREEN_WIDTH - group_w) / 2))
    cy = y + DOT_RADIUS
    for index in range(state.rounds):
        cx = x + DOT_RADIUS + index * (2 * DOT_RADIUS + gap)
        box = (cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS)
        if index < filled:
            draw.ellipse(box, fill=0)
        else:
            draw.ellipse(box, outline=0)
            if index == current:
                radius = max(2, DOT_RADIUS // 3)
                draw.ellipse(
                    (cx - radius, cy - radius, cx + radius, cy + radius), fill=0
                )
    draw.text((x + dot_w + 18, y), text, font=dot_font, fill=0)


def compose(
    state: PomodoroState, *, font_path: str | None = None, updated: str | None = None
):
    """Return the 400x300 ``L`` face: 0 = black ink, 255 = paper white."""
    text_path, mono_path = select_fonts(font_path)
    mono = mono_path or text_path
    image = Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    usable = SCREEN_WIDTH - 2 * MARGIN_X

    # ── header: title + update stamp + rule ────────────────────────────────
    stamp_font = _truetype(mono, STAMP_PX)
    draw.text((MARGIN_X, 0), TITLE, font=stamp_font, fill=0)
    stamp = updated if updated is not None else datetime.now().strftime("%m-%d %H:%M")
    stamp_x = SCREEN_WIDTH - MARGIN_X - draw.textlength(stamp, font=stamp_font)
    draw.text((stamp_x, 0), stamp, font=stamp_font, fill=0)
    rule_y = _metrics(stamp_font) + 2
    draw.line((0, rule_y, SCREEN_WIDTH, rule_y), fill=0)

    # ── measure every fixed row, then fit the hourglass in the leftover ────
    zh, en = PHASE_NAMES[state.phase]
    label = f"{zh}  {en}"
    label_font = _fit_font(
        draw, text_path, label, 40, LABEL_MIN_PX, LABEL_MAX_PX, usable
    )
    h_label = _metrics(label_font)

    minutes = minute_text(state)
    minute_font = _fit_font(
        draw, text_path, minutes, MINUTE_MAX_PX, MINUTE_MIN_PX, MINUTE_MAX_PX, usable
    )
    h_minute = _metrics(minute_font)

    dot_font = _truetype(text_path, STAMP_PX)
    h_dots = 2 * DOT_RADIUS + 2

    footer = f"今日完成 {state.cycle_total} 个番茄"
    footer_font = _fit_font(draw, text_path, footer, STAMP_PX, 14, STAMP_PX, usable)
    h_footer = _metrics(footer_font)

    top = rule_y + 8
    bottom = SCREEN_HEIGHT - 8
    available = bottom - top

    gap = H_GAP
    fixed = h_label + h_minute + h_dots + h_footer
    hg_h = min(HG_MAX_H, available - fixed - 4 * gap)
    if hg_h < HG_MIN_H:
        gap = 6
        hg_h = min(HG_MAX_H, available - fixed - 4 * gap)
    if hg_h < HG_MIN_H:
        raise FontError(
            f"only {hg_h}px left for the hourglass; pick a smaller font_path"
        )

    stage_h = fixed + hg_h + 4 * gap
    y = top + max(0, (available - stage_h) // 2)

    # ── phase label ────────────────────────────────────────────────────────
    draw.text((_center_x(draw, label, label_font), y), label, font=label_font, fill=0)
    y += h_label + gap

    # ── the hourglass: this row is the progress itself ─────────────────────
    remaining_frac = state.remaining / max(state.phase_seconds, 1)
    _draw_hourglass(
        draw, SCREEN_WIDTH // 2, y, hg_h, remaining_frac, running=state.running
    )
    y += hg_h + gap

    # ── remaining minutes (paused marker folds into this line) ─────────────
    draw.text(
        (_center_x(draw, minutes, minute_font), y), minutes, font=minute_font, fill=0
    )
    y += h_minute + gap

    # ── cycle dots + counter ───────────────────────────────────────────────
    _draw_dots(draw, state, y, dot_font)
    y += h_dots + gap

    # ── footer ─────────────────────────────────────────────────────────────
    draw.text(
        (_center_x(draw, footer, footer_font), y), footer, font=footer_font, fill=0
    )
    return image
