"""The EPD42 packed-bit stream protocol, shared by the client and the tests.

Kept free of any BLE or imaging dependency so the framing can be checked
offline, without a radio or a panel in reach.  Values mirror EPD/EPD_ble.h.
"""
from __future__ import annotations

from render import checksum

# 128-bit forms of the vendor base UUID registered by the firmware
# (EPD/EPD_ble.c BLE_EPD_BASE_UUID) with short IDs 0x0001 / 0x0002.
# The characteristic is 62750002-d828-918d-fb46-b6c11c675aec.
EPD_SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"

CMD_SLEEP = 0x06            # EPD_CMD_SLEEP
CMD_STREAM_BEGIN = 0xB0
CMD_STREAM_DATA = 0xB1
CMD_STREAM_END = 0xB2
CMD_STREAM_ABORT = 0xB3
CMD_GET_STATUS = 0xB5

FLAG_REFRESH = 0x01
FLAG_SLEEP = 0x02

STATUS_OK = 0x00
STATUS_NAMES = {
    STATUS_OK: "ok",
    0x01: "bad command / no plane open",
    0x02: "plane overrun",
    0x03: "verify failed (byte count or checksum)",
    0x04: "panel busy timeout",
}

# One GATT write carries the command byte plus BLE_EPD_MAX_DATA_LEN - 1 pixels.
# The ATT payload cannot grow: S110/S130 are Bluetooth 4.1, with no MTU exchange.
DATA_CHUNK = 19

# Offsets inside the EPD_CMD_GET_STATUS notification.
STATUS_STREAMING = 1
STATUS_PLANE = 2
STATUS_RECEIVED = 3
STATUS_PLANE_BYTES = 5
STATUS_DRIVER = 7


def describe_status(status: int) -> str:
    return STATUS_NAMES.get(status, f"unknown status 0x{status:02x}")


def iter_chunks(data: bytes):
    """Yield STREAM_DATA packets: the command byte plus up to DATA_CHUNK pixels."""
    for offset in range(0, len(data), DATA_CHUNK):
        yield bytes([CMD_STREAM_DATA]) + data[offset:offset + DATA_CHUNK]


def end_request(plane: bytes, flags: int) -> bytes:
    """Build a STREAM_END request: byte count, running sum, then the flags."""
    from render import checksum

    return (bytes([CMD_STREAM_END]) + len(plane).to_bytes(2, "little")
            + checksum(plane).to_bytes(4, "little") + bytes([flags]))
