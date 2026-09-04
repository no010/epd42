#!/usr/bin/env python3
"""Offline self-test for the Pomodoro face, state machine and protocol reuse.

Nothing here touches hardware or BLE. It pins down the bit packing convention
(inherited from tools/epd-monitor via render.pack_plane) and the state machine
that decides what the face shows.

    uv run python test_face.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TOOLS / "epd-monitor"))

import protocol  # noqa: E402  shared wire constants
import render  # noqa: E402  shared plane packing
import state as stm  # noqa: E402  local: timer state machine
import face  # noqa: E402  local: the e-ink face
from PIL import Image, ImageDraw  # noqa: E402


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)
    print(f"  ok  {description}")


def test_geometry() -> None:
    print("geometry")
    check(
        render.SCREEN_WIDTH % 8 == 0,
        "width is byte aligned, so a row never straddles two bytes",
    )
    check(
        face.SCREEN_WIDTH == render.SCREEN_WIDTH
        and face.SCREEN_HEIGHT == render.SCREEN_HEIGHT,
        "the face composes at the shared panel size",
    )


def test_state_machine() -> None:
    print("state machine")
    cfg = {"work_minutes": 25, "short_minutes": 5, "long_minutes": 15, "rounds": 4}
    s = stm.PomodoroState.from_cfg(cfg)
    check(
        s.phase == stm.PHASE_WORK and s.remaining == 1500,
        "starts as a 25 min work session",
    )

    stm.advance(s, cfg)
    check(
        s.phase == stm.PHASE_SHORT_BREAK
        and s.remaining == 300
        and s.pomodoro_count == 1
        and s.cycle_total == 1,
        "work -> 5 min short break, counters increment",
    )

    for _ in range(2):
        stm.advance(s, cfg)  # break -> work
        stm.advance(s, cfg)  # work -> break
    check(
        s.phase == stm.PHASE_SHORT_BREAK and s.pomodoro_count == 3,
        "third break is still a short one",
    )

    stm.advance(s, cfg)  # break -> work
    stm.advance(s, cfg)  # 4th work finished
    check(
        s.phase == stm.PHASE_LONG_BREAK
        and s.remaining == 900
        and s.pomodoro_count == 4,
        "4th work session -> 15 min long break",
    )

    stm.advance(s, cfg)  # long break finished
    check(
        s.phase == stm.PHASE_WORK and s.pomodoro_count == 0, "long break -> fresh cycle"
    )

    before = s.cycle_total
    stm.skip(s, cfg)  # work -> short break, no counters touched
    check(
        s.phase == stm.PHASE_SHORT_BREAK and s.cycle_total == before,
        "manual skip never counts a finished tomato",
    )

    check(
        stm.mmss(1499.6) == "25:00"
        and stm.mmss(59) == "00:59"
        and stm.mmss(0) == "00:00"
        and stm.mmss(-3) == "00:00",
        "mmss ceiling formatting",
    )


def test_persistence() -> None:
    print("persistence")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "state.json"
        s = stm.PomodoroState.from_cfg({"work_minutes": 25})
        s.running = True
        s.remaining = 10
        s.save(path)
        loaded = stm.PomodoroState.load(path)
        check(
            loaded is not None and loaded.remaining == 10 and loaded.running,
            "save/load roundtrip",
        )

        s.remaining = 5
        s.save(path)
        loaded = stm.PomodoroState.load(path)
        check(
            loaded is not None and loaded.remaining <= 5,
            "a running state rolls forward by wall time on load",
        )

        s.running = False
        s.remaining = 5
        s.save(path)
        loaded = stm.PomodoroState.load(path)
        check(
            loaded is not None and loaded.remaining == 5,
            "a paused state keeps its remaining seconds",
        )

        path.write_text("{corrupt", encoding="utf-8")
        check(stm.PomodoroState.load(path) is None, "corrupt state falls back to None")


def _ink_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of every ink pixel; guards against clipped rows."""
    mask = image.convert("L").point(lambda p: 255 if p < 128 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return (0, 0, 0, 0)
    return bbox


def _unpack(plane: bytes) -> Image.Image:
    """Inverse of render.pack_plane, for roundtrip checks."""
    image = Image.new("L", (render.SCREEN_WIDTH, render.SCREEN_HEIGHT), 255)
    px = image.load()
    if px is None:
        raise RuntimeError("Pillow returned no pixel access object")
    for row in range(render.SCREEN_HEIGHT):
        for byte_index in range(render.LINE_BYTES):
            byte = plane[row * render.LINE_BYTES + byte_index]
            for bit in range(8):
                if not (byte & (0x80 >> bit)):
                    px[byte_index * 8 + bit, row] = 0
    return image


def test_hourglass_contains_sand() -> None:
    print("hourglass containment")
    cx, y_top, height = 200, 60, 120
    y_mid = y_top + height // 2
    y_bottom = y_top + height
    half = height * face.HG_ASPECT / 2
    for frac in (0.95, 0.7, 0.3, 0.05):
        img = Image.new("L", (render.SCREEN_WIDTH, render.SCREEN_HEIGHT), 255)
        draw = ImageDraw.Draw(img)
        face._draw_hourglass(draw, cx, y_top, height, frac, running=True)
        data = img.tobytes()
        # Skip the frame-bar rows: check only the two bulbs, row by row.
        for y in range(y_top + 1, y_bottom):
            if y < y_mid:
                extent = half * (y_mid - y) / (y_mid - y_top)
            elif y > y_mid:
                extent = half * (y - y_mid) / (y_bottom - y_mid)
            else:
                extent = 0.0
            limit = extent + 3  # outline stroke ±1.5 px plus rounding
            for x in range(cx - 90, cx + 91):
                if data[y * render.SCREEN_WIDTH + x] < 128 and abs(x - cx) > limit:
                    raise AssertionError(
                        f"frac {frac}: ink at ({x},{y}) escaped the bulb "
                        f"(allowed ±{limit:.1f} about x={cx})"
                    )
    check(True, "sand and stream stay inside the outline at every fill level")


def test_hourglass_top_drains_downward() -> None:
    print("hourglass drain direction")
    cx, y_top, height = 200, 60, 120
    y_mid = y_top + height // 2
    img = Image.new("L", (render.SCREEN_WIDTH, render.SCREEN_HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    face._draw_hourglass(draw, cx, y_top, height, 0.5, running=False)
    data = img.tobytes()
    width = render.SCREEN_WIDTH

    def center_ink(y: int) -> int:
        return sum(1 for x in range(cx - 10, cx + 11) if data[y * width + x] < 128)

    void_row = y_top + 8  # just below the top bar: must be empty at half time
    sand_row = y_mid - 6  # just above the neck: the sand rests on the neck
    check(
        center_ink(void_row) == 0 and center_ink(sand_row) > 0,
        "top bulb empties from the top edge down, sand rests against the neck",
    )


def test_single_file_bundle() -> None:
    print("single-file bundle")
    import subprocess
    import tempfile

    import build_single

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "pomodoro.py"
        build_single.build(out)
        src = out.read_text(encoding="utf-8")
        check(
            all(f"_bundle({name!r}, " in src for name in build_single._BUNDLED_MODULES),
            "bundle registers every module (config/render/protocol/ble_client/state/face)",
        )
        check(
            "class _BleakLazy" in src
            and "from bleak import BleakClient, BleakScanner" not in src,
            "bundle keeps bleak optional and lazy",
        )
        proc = subprocess.run(
            [sys.executable, str(out), "--help"], capture_output=True, text=True
        )
        check(
            proc.returncode == 0 and "start" in proc.stdout,
            "bundled file runs --help without uv or the sibling folder",
        )
        proc = subprocess.run(
            [sys.executable, str(out), "render", "--demo", "--preview", "p.png"],
            cwd=td,
            capture_output=True,
            text=True,
        )
        check(
            proc.returncode == 0 and Path(td, "p.png").exists(),
            "bundled file renders the demo frame (Pillow only)",
        )


def test_face() -> None:
    print("face")
    cfg = {"work_minutes": 25, "short_minutes": 5, "long_minutes": 15, "rounds": 4}
    base = stm.PomodoroState.from_cfg(cfg)
    base.remaining = 24 * 60 + 41
    base.running = True
    base.pomodoro_count = 2
    base.cycle_total = 7

    for phase in stm.PHASES:
        base.phase = phase
        image = face.compose(base, updated="09-03 14:30")
        check(
            image.size == (render.SCREEN_WIDTH, render.SCREEN_HEIGHT),
            f"{phase}: face is 400x300",
        )
        pixels = image.convert("L").tobytes()
        check(any(p < 128 for p in pixels), f"{phase}: has ink")
        check(any(p > 127 for p in pixels), f"{phase}: has paper")

    for phase in stm.PHASES:
        base.phase = phase
        image = face.compose(base, updated="09-03 14:30")
        x0, y0, x1, y1 = _ink_bbox(image)
        check(
            y1 <= render.SCREEN_HEIGHT - 4, f"{phase}: ink stays above the bottom edge"
        )

    paused = stm.PomodoroState.from_cfg(cfg)
    paused.running = False
    paused_image = face.compose(paused, updated="09-03 14:30")
    check(
        paused_image.size == (render.SCREEN_WIDTH, render.SCREEN_HEIGHT),
        "paused face still renders",
    )
    check(
        _ink_bbox(paused_image)[3] <= render.SCREEN_HEIGHT - 4,
        "paused face ink stays above the bottom edge",
    )

    def lower_half_ink(image: Image.Image) -> int:
        """Ink below the panel's midline; the hourglass pile lives there."""
        data = image.convert("L").tobytes()
        width = render.SCREEN_WIDTH
        count = 0
        for y in range(render.SCREEN_HEIGHT // 2, render.SCREEN_HEIGHT):
            row = y * width
            for x in range(0, width, 2):
                if data[row + x] < 128:
                    count += 1
        return count

    full = stm.PomodoroState.from_cfg(cfg)
    spent = stm.PomodoroState.from_cfg(cfg)
    spent.remaining = int(full.phase_seconds * 0.3)
    check(
        lower_half_ink(face.compose(full, updated="09-03 14:30"))
        < lower_half_ink(face.compose(spent, updated="09-03 14:30")),
        "hourglass: bottom sand piles up as time is spent",
    )

    image = face.compose(base, updated="09-03 14:30")
    plane = render.pack_plane(image)
    check(len(plane) == render.PLANE_BYTES, "packed plane is 15000 bytes")
    # pack_plane quantises at >127 (paper), so compare against the same cut.
    thresholded = image.convert("L").point(lambda p: 255 if p > 127 else 0)
    check(
        _unpack(plane).tobytes() == thresholded.tobytes(),
        "pack -> unpack roundtrip preserves every pixel (at the >127 cut)",
    )

    encoded = protocol.packbits_encode(plane)
    check(
        protocol.packbits_decode(encoded) == plane,
        "PackBits roundtrip preserves the plane",
    )
    max_payload = max(len(p) - 1 for p in protocol.iter_chunks(encoded))
    check(
        max_payload <= protocol.DATA_CHUNK, "chunked writes fit the 19-byte ATT payload"
    )


def main() -> int:
    for fn in (
        test_geometry,
        test_state_machine,
        test_persistence,
        test_hourglass_contains_sand,
        test_hourglass_top_drains_downward,
        test_single_file_bundle,
        test_face,
    ):
        fn()
    print("\nall tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
