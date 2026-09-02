"""Compose the 400x300 e-ink frame on the PC and pack it for streaming.

The firmware deliberately holds no framebuffer and no font: it forwards the
bytes produced here straight into the panel's RAM.  Everything visual -
layout, typography, progress bars - lives in this module.

Layout is measured, not assumed.  Card height comes from the item count and
every row offset comes from the metrics of the font that was actually loaded,
so a font whose line height is not 16 px can no longer push text into a
progress bar, and one item fills the screen instead of sitting in it.

Wire convention (must match the firmware, see EPD/EPD_4in2*.c):
  * MSB is the leftmost pixel of a byte
  * bit = 1 means white paper, bit = 0 means black ink
  * one plane is ``PLANE_BYTES`` = 50 * 300 = 15000 bytes
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

SCREEN_WIDTH = 400
SCREEN_HEIGHT = 300
LINE_BYTES = SCREEN_WIDTH // 8
PLANE_BYTES = LINE_BYTES * SCREEN_HEIGHT

# What the host streams, and where each image lands in the panel's RAM.
#
# A black-and-white update on the UC8176 is the OLD (0x10) -> NEW (0x13)
# transition, so both SRAMs need data; the host sends only the image and the
# firmware fills OLD itself.  A tri-colour panel genuinely needs two planes from
# the host: B/W and red.  Driver 2 streams one plane because that is the only
# path observed working from html/js - it is not verified against the SSD1683
# datasheet, which is not in this repo.
DRIVER_PLANES: dict[int, int] = {1: 1, 2: 1, 3: 2}
DRIVER_IMAGE_RAM: dict[int, int] = {1: 0x13, 2: 0x24, 3: 0x10}
DRIVER_NAMES = {
    1: "4.2in e-Paper (UC8176)",
    2: "4.2in e-Paper V2 (BW)",
    3: "4.2in e-Paper B V2 (UC8276C)",
}

MAX_ITEMS = 3
MARGIN_X = 8
MIN_FONT_PX = 14
MAX_FONT_PX = 40
STAMP_FONT_PX = 16
BAR_MIN_PX = 4

# Only fonts known to carry CJK glyphs.  Pillow exposes no cmap API, so rather
# than guess at coverage, a font is only auto-selected from a list where CJK is
# a fact - a screen full of tofu is not a visible enough failure.
CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",          # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",        # 黑体
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
# Tabular digits, so a column of numbers does not shift sideways between
# refreshes as the digits change.
MONO_FONT_CANDIDATES = (
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)


class FontError(RuntimeError):
    """No usable font: raise instead of drawing tofu nobody notices."""


@dataclass(frozen=True)
class Plane:
    """One packed plane, ready for EPD_CMD_STREAM_DATA."""

    index: int
    data: bytes


@dataclass(frozen=True)
class Layout:
    """Measured geometry for one frame, derived from item count and fonts.

    ``usage_row``, ``note_row``, ``bar_row`` and ``bar_h`` are offsets from a
    card's top; the card tops themselves are ``content_top + index * card_h``.
    """

    font_px: int
    text_h: int
    rule_row: int
    content_top: int
    content_bottom: int
    card_h: int
    pad: int
    usage_row: int
    note_row: int
    note_h: int
    bar_row: int
    bar_h: int

    @property
    def card_bottom_offset(self) -> int:
        return self.bar_row + self.bar_h


def _metrics(font) -> int:
    """Line height this font actually produces, ascenders and descenders in."""
    box = font.getbbox("AgjyQ")
    return max(box[3] - box[1], 1) + 2


def _open_font(path: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


def _first_existing(paths: Sequence[str]) -> str | None:
    for path in paths:
        if Path(path).exists():
            return path
    return None


def select_fonts(font_path: str | None = None) -> tuple[str, str | None]:
    """Return (body font path, optional monospace path) or raise FontError."""
    text = font_path or _first_existing(CJK_FONT_CANDIDATES)
    if not text:
        raise FontError(
            "no CJK-capable font found. Set font_path in config.toml to a font that "
            "covers your plan names, or install one of: "
            + ", ".join(CJK_FONT_CANDIDATES)
        )
    return text, (None if font_path else _first_existing(MONO_FONT_CANDIDATES))


def _content_box(stamp_h: int) -> tuple[int, int, int]:
    """Title row up top (shared by the title and the update stamp), then the
    card area down to a thin bottom margin - no reserved bottom row."""
    rule_row = stamp_h + 2
    return rule_row, rule_row + 4, SCREEN_HEIGHT - 4


CURRENCY_SYMBOLS = {"CNY": "¥", "RMB": "¥", "元": "¥", "USD": "$", "US$": "$", "$": "$"}


def _balance_text(item) -> str:
    """A bare number does not say whether it is yuan or dollars."""
    amount = f"{item.balance // 100:,}.{item.balance % 100:02d}"
    symbol = CURRENCY_SYMBOLS.get(item.unit.upper())
    return f"{symbol}{amount}" if symbol else f"{amount} {item.unit}".strip()


def usage_line(item) -> str:
    """The card's metrics line: quota and balance, no notes."""
    parts = []
    if item.quota_total:
        if item.unit == "%":
            parts.append(f"{item.quota_used / item.quota_total * 100:.1f}%")
        else:
            parts.append(f"{item.quota_used:,} / {item.quota_total:,} {item.unit}")
    elif item.quota_used:
        parts.append(f"{item.quota_used:,} {item.unit} used")
    if item.balance:
        parts.append(_balance_text(item))
    line = "   ".join(parts)
    extra = getattr(item, "extra", "")
    if extra:
        line = f"{line} {extra}" if line else extra
    return line or "no data"


def line_font(text: str, ascii_font, cjk_font):
    """One font per line: a line containing CJK renders entirely in the CJK
    face - per-run font mixing misaligns baselines and advances."""
    return ascii_font if all(ord(ch) < 128 for ch in text) else cjk_font


def has_bar(item) -> bool:
    """A bar only means something with a quota; an empty outline reads as 0%."""
    return bool(item.quota_total) and getattr(item, "show_bar", True)


def plan(count: int, text_font_path: str, mono_path: str | None, stamp_font,
         texts: Sequence[str] = (), usable_px: int = SCREEN_WIDTH - 2 * MARGIN_X):
    """Lay out ``count`` cards, shrinking the body font until it fits.

    "Fits" means both directions: name + usage + bar stacked inside the card
    height, and every ``texts`` line inside the usable width.  Clipping a unit
    off the end of a number is a worse outcome than a slightly smaller font, so
    width is what caps a single item's size.

    Each card keeps a bottom pad so neighbours never touch, and the slack left
    over is split into two equal gaps.  That is what makes one item fill the
    panel rather than sit at the top of it in larger text.
    """
    count = max(1, min(count, MAX_ITEMS))
    rule_row, top, bottom = _content_box(_metrics(stamp_font))
    card_h = (bottom - top) // count
    pad = max(2, card_h // 10)
    mono_path = mono_path or text_font_path

    def rows(size: int) -> tuple[int, int, int, int]:
        text_h = _metrics(_open_font(text_font_path, size))
        note_h = _metrics(stamp_font)
        bar_h = max(BAR_MIN_PX, card_h // 10)
        usable = card_h - pad
        stack = text_h * 2 + note_h + bar_h
        gap = max(2, (usable - stack) // 3)
        usage_row = text_h + gap
        note_row = usage_row + text_h + gap
        bar_row = note_row + note_h + gap
        return text_h, note_h, usage_row, note_row, bar_row, bar_h

    def fits(size: int) -> bool:
        digit_font = _open_font(mono_path, size)
        if any(digit_font.getlength(text) > usable_px for text in texts):
            return False
        _text_h, _note_h, _usage, _note, bar_row, bar_h = rows(size)
        return bar_row + bar_h <= card_h

    font_px = max(MIN_FONT_PX, min(MAX_FONT_PX, card_h // 4))
    while font_px > MIN_FONT_PX and not fits(font_px):
        font_px -= 2

    text_h, note_h, usage_row, note_row, bar_row, bar_h = rows(font_px)
    layout = Layout(
        font_px=font_px,
        text_h=text_h,
        rule_row=rule_row,
        content_top=top,
        content_bottom=bottom,
        card_h=card_h,
        pad=pad,
        usage_row=usage_row,
        note_row=note_row,
        note_h=note_h,
        bar_row=bar_row,
        bar_h=bar_h,
    )
    body = _open_font(text_font_path, font_px)
    return layout, body, _open_font(mono_path, font_px)


def fit(text: str, font, max_px: float, draw) -> str:
    """Shorten text until it fits, marking that it was shortened."""
    if not text or draw.textlength(text, font=font) <= max_px:
        return text
    while text and draw.textlength(text + "…", font=font) > max_px:
        text = text[:-1]
    return text + "…" if text else ""


def compose(items: Sequence, title: str = "SUB MONITOR",
            updated: str | None = None, font_path: str | None = None):
    """Return the frame as a PIL ``L`` image: 0 = black ink, 255 = paper."""
    from PIL import Image, ImageDraw

    text_font_path, mono_path = select_fonts(font_path)
    image = Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    items = list(items)[:MAX_ITEMS]
    usable = SCREEN_WIDTH - 2 * MARGIN_X
    stamp_font = _open_font(mono_path or text_font_path, STAMP_FONT_PX)
    note_cjk = _open_font(text_font_path, STAMP_FONT_PX)
    layout, body, digits = plan(len(items) or 1, text_font_path, mono_path, stamp_font,
                                texts=[usage_line(item) for item in items],
                                usable_px=usable)

    draw.text((MARGIN_X, 0), title, font=stamp_font, fill=0)
    stamp = updated if updated is not None else datetime.now().strftime("%m-%d %H:%M")
    if stamp:
        stamp_x = SCREEN_WIDTH - MARGIN_X - draw.textlength(stamp, font=stamp_font)
        draw.text((stamp_x, 0), stamp, font=stamp_font, fill=0)
    draw.line((0, layout.rule_row, SCREEN_WIDTH, layout.rule_row), fill=0)

    for index, item in enumerate(items):
        top = layout.content_top + index * layout.card_h
        draw.text((MARGIN_X, top), fit(item.plan_name, body, usable, draw),
                  font=body, fill=0)
        usage = usage_line(item)
        usage_font = line_font(usage, body, digits)
        draw.text((MARGIN_X, top + layout.usage_row),
                  fit(usage, usage_font, usable, draw), font=usage_font, fill=0)
        note = getattr(item, "note", "")
        if note:
            note_font = line_font(note, stamp_font, note_cjk)
            draw.text((MARGIN_X, top + layout.note_row),
                      fit(note, note_font, usable, draw), font=note_font, fill=0)

        if has_bar(item):
            bar_top = top + layout.bar_row
            bar_bottom = min(bar_top + layout.bar_h, top + layout.card_h - 1)
            bar_right = SCREEN_WIDTH - MARGIN_X - 1
            bar_text = getattr(item, "bar_text", "")
            if bar_text:
                bar_right -= int(usable * 0.34)
            draw.rectangle((MARGIN_X, bar_top, bar_right, bar_bottom), outline=0)
            inner = bar_right - MARGIN_X - 1
            filled = min(max(item.quota_used, 0), item.quota_total)
            width = inner * filled // item.quota_total
            if width:
                draw.rectangle((MARGIN_X + 1, bar_top + 1, MARGIN_X + width,
                                bar_bottom - 1), fill=0)
            if bar_text:
                draw.text((bar_right + 6, bar_top), bar_text, font=stamp_font, fill=0)

    return image


def pack_plane(image) -> bytes:
    """Pack an ``L`` image into one plane of ``PLANE_BYTES``."""
    if image.size != (SCREEN_WIDTH, SCREEN_HEIGHT):
        raise ValueError(f"expected {SCREEN_WIDTH}x{SCREEN_HEIGHT}, got {image.size}")

    pixels = image.convert("L").tobytes()
    plane = bytearray(PLANE_BYTES)
    for row in range(SCREEN_HEIGHT):
        offset = row * SCREEN_WIDTH
        for byte_index in range(LINE_BYTES):
            bits = 0
            for bit in range(8):
                if pixels[offset + byte_index * 8 + bit] > 127:
                    bits |= 0x80 >> bit
            plane[row * LINE_BYTES + byte_index] = bits
    return bytes(plane)


def blank_plane() -> bytes:
    """An all-paper plane, used for the red channel of a BWR panel."""
    return bytes([0xFF]) * PLANE_BYTES


# Each pattern answers one question; a wrong UI would only look "wrong".
PATTERNS = ("white", "black", "corner-dots", "row-marker", "left-half", "grid")


def pattern(name: str, row: int = 123):
    """Return a synthetic 400x300 ``L`` image for bring-up on real hardware.

    white / black     polarity: an inverted result means the wire convention
                      (1 = white) and the panel disagree.
    corner-dots       bit order: four single pixels at the extremes plus one at
                      (3, 1).  If that last one lands at x = 4, MSB-first is wrong.
    row-marker        row addressing: one solid row at ``row``.  If it shows up
                      elsewhere, the RAM cursor or window is off.
    left-half         scan direction: left must be ink, right paper.
    grid              scaling and byte alignment: lines every 25 px, which is a
                      whole number of bytes in x.
    """
    from PIL import Image

    if name not in PATTERNS:
        raise ValueError(f"unknown pattern {name!r}; known: {PATTERNS}")

    if name == "white":
        return Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 255)
    if name == "black":
        return Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 0)

    image = Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 255)
    last_x, last_y = SCREEN_WIDTH - 1, SCREEN_HEIGHT - 1

    if name == "corner-dots":
        for x, y in ((0, 0), (last_x, 0), (0, last_y), (last_x, last_y), (3, 1)):
            image.putpixel((x, y), 0)
    elif name == "row-marker":
        if not 0 <= row < SCREEN_HEIGHT:
            raise ValueError(f"row {row} is off the panel")
        for x in range(SCREEN_WIDTH):
            image.putpixel((x, row), 0)
    elif name == "left-half":
        for y in range(SCREEN_HEIGHT):
            for x in range(SCREEN_WIDTH // 2):
                image.putpixel((x, y), 0)
    elif name == "grid":
        for y in range(0, SCREEN_HEIGHT, 25):
            for x in range(SCREEN_WIDTH):
                image.putpixel((x, y), 0)
        for x in range(0, SCREEN_WIDTH, 25):
            for y in range(SCREEN_HEIGHT):
                image.putpixel((x, y), 0)
    return image


def pack_planes(image, driver_id: int, planes: int = 1) -> list[Plane]:
    """Pack ``image`` into the planes ``driver_id`` expects, in stream order."""
    if driver_id not in DRIVER_PLANES:
        raise ValueError(f"unknown driver id {driver_id}; known: {sorted(DRIVER_PLANES)}")
    if not 1 <= planes <= DRIVER_PLANES[driver_id]:
        raise ValueError(f"driver {driver_id} supports 1..{DRIVER_PLANES[driver_id]} planes")

    packed = [Plane(0, pack_plane(image))]
    if planes > 1:
        packed.append(Plane(1, blank_plane()))
    return packed


def checksum(data: bytes) -> int:
    """Running byte sum the firmware compares against in EPD_CMD_STREAM_END."""
    return sum(data) & 0xFFFFFFFF
