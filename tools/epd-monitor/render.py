"""Compose the 400x300 e-ink frame on the PC and pack it for streaming.

The firmware deliberately holds no framebuffer and no font: it forwards the
bytes produced here straight into the panel's RAM.  Everything visual -
layout, typography, progress bars - lives in this module.

Wire convention (must match the firmware, see EPD/EPD_4in2*.c):
  * MSB is the leftmost pixel of a byte
  * bit = 1 means white paper, bit = 0 means black ink
  * one plane is ``PLANE_BYTES`` = 50 * 300 = 15000 bytes
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

SCREEN_WIDTH = 400
SCREEN_HEIGHT = 300
LINE_BYTES = SCREEN_WIDTH // 8
PLANE_BYTES = LINE_BYTES * SCREEN_HEIGHT

# Plane RAM commands per driver, in stream order.  Mirrors the switch in
# EPD/EPD_4in2.c, EPD/EPD_4in2_V2.c and EPD/EPD_4in2b_V2.c.
DRIVER_PLANES: dict[int, tuple[int, ...]] = {
    1: (0x10, 0x13),      # EPD_DRIVER_4IN2      - old data, new data
    2: (0x24, 0x26),      # EPD_DRIVER_4IN2_V2   - black, second plane
    3: (0x10, 0x13),      # EPD_DRIVER_4IN2B_V2  - black, red
}
DRIVER_NAMES = {
    1: "4.2in e-Paper",
    2: "4.2in e-Paper V2 (BW)",
    3: "4.2in e-Paper B V2 (BWR)",
}

MAX_ITEMS = 3

# Vertical grid, inherited from the layout the firmware used to render.
TITLE_ROW = 0
RULE_ROW = 16
ITEM0_ROW = 20
ITEM_STRIDE = 44
NAME_OFFSET = 0
USAGE_OFFSET = 16
BAR_OFFSET = 32
BAR_HEIGHT = 4
UPDATED_ROW = 280
TEXT_HEIGHT = 16
MARGIN_X = 8


@dataclass(frozen=True)
class Plane:
    """One packed plane, ready for EPD_CMD_STREAM_DATA."""

    index: int
    data: bytes


def _font(font_path: str | None = None):
    from PIL import ImageFont

    if font_path:
        return ImageFont.truetype(font_path, TEXT_HEIGHT)
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", TEXT_HEIGHT)
    except OSError:
        try:
            return ImageFont.load_default(size=TEXT_HEIGHT)
        except TypeError:  # Pillow < 10.1 has no size argument
            return ImageFont.load_default()


def _clip(text: str, font, max_px: int, draw) -> str:
    while text and draw.textlength(text, font=font) > max_px:
        text = text[:-2]
    return text


def _usage_line(item) -> str:
    parts = []
    if item.quota_total:
        parts.append(f"{item.quota_used} / {item.quota_total} {item.unit}".strip())
    if item.balance:
        parts.append(f"${item.balance / 100:.2f}")
    return "  ".join(parts) or f"{item.quota_used} {item.unit}".strip()


def compose(items: Sequence, title: str = "SUB MONITOR",
            updated: str | None = None, font_path: str | None = None):
    """Return the frame as a PIL ``L`` image: 0 = black ink, 255 = paper."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (SCREEN_WIDTH, SCREEN_HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = _font(font_path)
    ink = 0

    draw.text(
        ((SCREEN_WIDTH - draw.textlength(title, font=font)) // 2, TITLE_ROW),
        title, font=font, fill=ink,
    )
    draw.line((0, RULE_ROW, SCREEN_WIDTH, RULE_ROW), fill=ink)

    for index, item in enumerate(items[:MAX_ITEMS]):
        base = ITEM0_ROW + index * ITEM_STRIDE
        usable = SCREEN_WIDTH - 2 * MARGIN_X
        draw.text((MARGIN_X, base + NAME_OFFSET),
                  _clip(item.plan_name, font, usable, draw), font=font, fill=ink)
        draw.text((MARGIN_X, base + USAGE_OFFSET),
                  _clip(_usage_line(item), font, usable, draw), font=font, fill=ink)

        top = base + BAR_OFFSET
        draw.rectangle((MARGIN_X, top, SCREEN_WIDTH - MARGIN_X, top + BAR_HEIGHT),
                       outline=ink)
        if item.quota_total:
            filled = min(item.quota_used, item.quota_total)
            width = (SCREEN_WIDTH - 2 * MARGIN_X - 2) * filled // item.quota_total
            if width:
                draw.rectangle((MARGIN_X + 1, top + 1, MARGIN_X + width,
                                top + BAR_HEIGHT - 1), fill=ink)

    stamp = updated if updated is not None else datetime.now().strftime("%m-%d %H:%M")
    if stamp:
        draw.text((MARGIN_X, UPDATED_ROW), f"Updated: {stamp}", font=font, fill=ink)
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


def pack_planes(image, driver_id: int, planes: int = 1) -> list[Plane]:
    """Pack ``image`` into the planes ``driver_id`` expects, in stream order."""
    if driver_id not in DRIVER_PLANES:
        raise ValueError(f"unknown driver id {driver_id}; "
                         f"known: {sorted(DRIVER_PLANES)}")
    if not 1 <= planes <= len(DRIVER_PLANES[driver_id]):
        raise ValueError(f"driver {driver_id} supports 1..{len(DRIVER_PLANES[driver_id])} planes")

    packed = [Plane(0, pack_plane(image))]
    if planes > 1:
        packed.append(Plane(1, blank_plane()))
    return packed


def checksum(data: bytes) -> int:
    """Running byte sum the firmware compares against in EPD_CMD_STREAM_END."""
    return sum(data) & 0xFFFFFFFF
