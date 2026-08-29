#!/usr/bin/env python3
"""Offline self-test for the packed-bit frame path.

Nothing here touches hardware.  It pins down the two things that silently
ruin an e-ink image when they drift: the bit packing convention, and the
agreement between this host and the firmware on plane order, plane size,
chunking and the stream protocol constants.

    python test_frame.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import protocol
import render
from PIL import Image

FIRMWARE = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Item:
    plan_name: str
    quota_total: int
    quota_used: int
    balance: int
    unit: str


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)
    print(f"  ok  {description}")


def frame(value: int = 255) -> Image.Image:
    return Image.new("L", (render.SCREEN_WIDTH, render.SCREEN_HEIGHT), value)


def test_geometry() -> None:
    print("geometry")
    check(render.LINE_BYTES * render.SCREEN_HEIGHT == render.PLANE_BYTES,
          f"one plane is {render.LINE_BYTES} x {render.SCREEN_HEIGHT} = {render.PLANE_BYTES} bytes")
    check(render.SCREEN_WIDTH % 8 == 0,
          "width is byte aligned, so a row never straddles two bytes")


def test_packing() -> None:
    print("bit packing (1 = white, MSB = leftmost)")
    check(set(render.pack_plane(frame(255))) == {0xFF}, "an all-paper image packs to 0xFF")
    check(set(render.pack_plane(frame(0))) == {0x00}, "an all-ink image packs to 0x00")

    paper = frame(255)
    dot = paper.copy()
    dot.putpixel((3, 1), 0)           # row 1, byte 0, bit 4 from the left
    plane = render.pack_plane(dot)
    check(plane[render.LINE_BYTES] == 0xFF ^ 0x10,
          "one ink pixel clears exactly its own MSB-first bit")
    check(render.checksum(plane) == render.PLANE_BYTES * 0xFF - 0x10,
          "and nothing else in the plane changed")

    corner = paper.copy()
    corner.putpixel((399, 299), 0)
    check(render.pack_plane(corner)[-1] == 0xFE,
          "the bottom-right pixel lands in the final byte")

    for size in ((399, 300), (400, 301)):
        try:
            render.pack_plane(Image.new("L", size, 255))
        except ValueError:
            check(True, f"a {size[0]}x{size[1]} image is rejected")
        else:
            check(False, f"a {size[0]}x{size[1]} image is rejected")


def test_checksum() -> None:
    print("checksum")
    check(render.checksum(bytes([1, 2, 253, 255])) == 511,
          "checksum is the plain running byte sum the firmware accumulates")
    check(render.checksum(render.pack_plane(frame())) < 2**32,
          "a full plane's sum cannot overflow the firmware's uint32")


def test_chunking() -> None:
    print("chunking")
    data = render.pack_plane(frame())
    packets = list(protocol.iter_chunks(data))
    check(len(packets) == 790 == -(-render.PLANE_BYTES // protocol.DATA_CHUNK),
          f"{render.PLANE_BYTES} bytes stream as 790 packets of {protocol.DATA_CHUNK} pixels")
    check(all(len(p) <= protocol.DATA_CHUNK + 1 for p in packets),
          "no packet exceeds the 20-byte ATT value the firmware advertises")
    check(all(p[0] == protocol.CMD_STREAM_DATA for p in packets),
          "every packet is tagged EPD_CMD_STREAM_DATA")
    check(b"".join(p[1:] for p in packets) == data, "the packets reassemble to the exact plane")
    check(len(packets[-1]) == render.PLANE_BYTES % protocol.DATA_CHUNK + 1,
          "the last packet carries only the 9 leftover pixels")

    request = protocol.end_request(b"\x01\x02\xff", protocol.FLAG_REFRESH | protocol.FLAG_SLEEP)
    check(request[0] == protocol.CMD_STREAM_END, "STREAM_END starts with its command byte")
    check(int.from_bytes(request[1:3], "little") == 3, "then the little-endian byte count")
    check(int.from_bytes(request[3:7], "little") == 258, "then the little-endian running sum")
    check(request[7] == 0x03 and len(request) == 8, "then the flags, in 8 bytes total")


def test_composition() -> None:
    print("composition")
    items = [Item("Copilot Pro", 1500, 375, 0, "req"),
             Item("Kimi", 0, 0, 1234, "CNY"),
             Item("DeepSeek", 2_000_000, 1_234_567, 0, "tkn"),
             Item("Fourth", 10, 1, 0, "req")]
    image = render.compose(items, updated="08-29 10:30")

    check(image.size == (render.SCREEN_WIDTH, render.SCREEN_HEIGHT), "frame is 400x300")
    check(len(items) == 4 and image.getpixel((0, render.RULE_ROW)) == 0,
          "the title rule is drawn across the full width")
    check(render.ITEM0_ROW + 3 * render.ITEM_STRIDE < render.UPDATED_ROW,
          "three item slots fit above the timestamp, so the fourth is dropped, not overlapped")

    bar_row = render.ITEM0_ROW + render.BAR_OFFSET + 2
    check(image.getpixel((render.MARGIN_X + 2, bar_row)) == 0
          and image.getpixel((render.SCREEN_WIDTH - render.MARGIN_X - 3, bar_row)) == 255,
          "a 25% bar fills from the left only")
    check(image.getpixel((0, render.UPDATED_ROW + 8)) == 255,
          "the left margin beside the timestamp stays paper white")


def test_planes() -> None:
    print("planes")
    image = render.compose([Item("A", 10, 5, 0, "req")], updated="")
    bw = render.pack_planes(image, 2)
    check([p.index for p in bw] == [0] and len(bw[0].data) == render.PLANE_BYTES,
          "a BW driver streams exactly one plane")
    bwr = render.pack_planes(image, 3, planes=2)
    check([p.index for p in bwr] == [0, 1], "a BWR driver streams plane 0 then plane 1")
    check(bwr[1].data == render.blank_plane(), "the red plane is all paper")

    for driver in (0, 7):
        try:
            render.pack_planes(image, driver)
        except ValueError:
            check(True, f"unknown driver id {driver} is rejected")
        else:
            check(False, f"unknown driver id {driver} is rejected")
    try:
        render.pack_planes(image, 2, planes=3)
    except ValueError:
        check(True, "asking for more planes than a driver has is rejected")
    else:
        check(False, "asking for more planes than a driver has is rejected")


def parse_commands(header: str) -> dict[str, int]:
    """Resolve EPD_CMDS the way the compiler does, implicit values included."""
    block = re.search(r"enum EPD_CMDS\s*\{(.*?)\n\};", header, re.S).group(1)
    commands: dict[str, int] = {}
    value = -1
    for name, explicit in re.findall(r"(EPD_CMD_[A-Z_]+)\s*(?:=\s*(0x[0-9A-Fa-f]+))?\s*,", block):
        value = int(explicit, 16) if explicit else value + 1
        commands[name] = value
    return commands


def plane_bytes_from_driver(header_name: str) -> int:
    """Evaluate a driver's plane size from its own WIDTH/HEIGHT defines."""
    text = (FIRMWARE / "EPD" / header_name).read_text(encoding="utf-8")
    prefix = re.search(r"#define (EPD_\w+)_WIDTH", text).group(1)
    width = int(re.search(rf"#define {prefix}_WIDTH\s+(\d+)", text).group(1))
    height = int(re.search(rf"#define {prefix}_HEIGHT\s+(\d+)", text).group(1))
    return (width // 8) * height


def test_firmware_parity() -> None:
    print("parity with the firmware sources")
    header = (FIRMWARE / "EPD" / "EPD_ble.h").read_text(encoding="utf-8")
    commands = parse_commands(header)

    for name, value in vars(protocol).items():
        if name.startswith("CMD_"):
            header_name = "EPD_CMD_" + name[4:]
            check(commands.get(header_name) == value,
                  f"protocol.{name} == {header_name} == 0x{value:02x}")
    check(not any(name.startswith("EPD_CMD_SET_SUBSCRIPTION") or
                  name.startswith("EPD_CMD_TRIGGER") for name in commands),
          "the removed 0xA0..0xA3 commands are gone from the header")

    flags = dict(re.findall(r"(EPD_STREAM_FLAG_[A-Z]+)\s*=\s*(0x[0-9A-Fa-f]{2})", header))
    check(int(flags["EPD_STREAM_FLAG_REFRESH"], 16) == protocol.FLAG_REFRESH,
          "EPD_STREAM_FLAG_REFRESH matches the client")
    check(int(flags["EPD_STREAM_FLAG_SLEEP"], 16) == protocol.FLAG_SLEEP,
          "EPD_STREAM_FLAG_SLEEP matches the client")
    check("BLE_EPD_MAX_DATA_LEN  (GATT_MTU_SIZE_DEFAULT - 3)" in header,
          "the ATT value stays at 20 bytes, so 19 pixels per write")

    sources = {1: ("EPD_4in2.h", "EPD_4in2.c"), 2: ("EPD_4in2_V2.h", "EPD_4in2_V2.c"),
               3: ("EPD_4in2b_V2.h", "EPD_4in2b_V2.c")}
    for driver, (header_name, source_name) in sources.items():
        check(plane_bytes_from_driver(header_name) == render.PLANE_BYTES,
              f"driver {driver} computes the same {render.PLANE_BYTES}-byte plane as the host")

        text = (FIRMWARE / "EPD" / source_name).read_text(encoding="utf-8")
        body = re.search(r"void EPD_\w+_StreamBegin\(uint8_t plane\)\s*\{(.*?)\n\}",
                         text, re.S).group(1)
        found = tuple(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{2})", body))
        check(found == render.DRIVER_PLANES[driver],
              f"driver {driver} planes {tuple(hex(v) for v in found)} match the host table")

        write = re.search(r"void EPD_\w+_StreamWrite\(const uint8_t \*buffer.*?\n\}",
                          text, re.S).group(0)
        check("~" not in write, f"driver {driver} forwards bytes without inverting them")


def main() -> int:
    for test in (test_geometry, test_packing, test_checksum, test_chunking,
                 test_composition, test_planes, test_firmware_parity):
        test()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
