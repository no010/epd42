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

CMD_INIT = 0x01             # EPD_CMD_INIT, with a driver id payload
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
    0x02: "decoded length does not match the plane",
    0x03: "checksum mismatch (corrupted bytes)",
    0x04: "panel busy timeout",
}

# One GATT write carries the command byte plus BLE_EPD_MAX_DATA_LEN - 1 payload
# bytes. The ATT payload cannot grow: S110/S130 are Bluetooth 4.1, no MTU exchange.
DATA_CHUNK = 19

# Offsets inside the EPD_CMD_GET_STATUS notification.
STATUS_STREAMING = 1
STATUS_PLANE = 2
STATUS_RECEIVED = 3
STATUS_PLANE_BYTES = 5
STATUS_DRIVER = 7


def describe_status(status: int) -> str:
    return STATUS_NAMES.get(status, f"unknown status 0x{status:02x}")


def packbits_encode(plane: bytes) -> bytes:
    """Run-length encode a plane (TIFF PackBits, no end-of-line marker).

    Control bytes 0..127 introduce a literal packet of n+1 bytes, 129..255
    repeat the next byte 257-n times, and 128 is a no-op.  Runs of three or
    more identical bytes are worth a control byte; anything shorter is copied
    literally, which is what keeps incompressible data within one extra byte
    per 128.
    """
    out = bytearray()
    index = 0
    total = len(plane)
    while index < total:
        byte = plane[index]
        run = 1
        while run < 128 and index + run < total and plane[index + run] == byte:
            run += 1
        if run >= 3:
            out.append(257 - run)
            out.append(byte)
            index += run
            continue
        literal = bytearray()
        while index < total and len(literal) < 128:
            lookahead = 1
            while (lookahead < 128 and index + lookahead < total
                   and plane[index + lookahead] == plane[index]):
                lookahead += 1
            if lookahead >= 3:
                break
            literal.append(plane[index])
            index += 1
        out.append(len(literal) - 1)
        out.extend(literal)
    return bytes(out)


class PackbitsDecoder:
    """Incremental PackBits decoder, mirroring the firmware state machine.

    Feed it the stream in arbitrary slices - a GATT packet can end between a
    control byte and the payload byte it governs - and the decoded output must
    come out identical to feeding it whole.  That property is the reason this
    is a class and not a loop over a buffer.
    """

    CONTROL, LITERAL, RUN_VALUE = 0, 1, 2

    def __init__(self) -> None:
        self._out = bytearray()
        self._mode = self.CONTROL
        self._left = 0

    def feed(self, data: bytes) -> None:
        for byte in data:
            if self._mode == self.LITERAL:
                self._out.append(byte)
                self._left -= 1
                if self._left == 0:
                    self._mode = self.CONTROL
            elif self._mode == self.RUN_VALUE:
                self._out.extend(bytes([byte]) * self._left)
                self._mode = self.CONTROL
            elif byte == 128:
                continue                      # PackBits no-op
            elif byte < 128:
                self._mode = self.LITERAL
                self._left = byte + 1
            else:
                self._mode = self.RUN_VALUE
                self._left = 257 - byte

    def decoded(self) -> bytes:
        """Bytes produced so far; shorter than the plane until the stream ends."""
        return bytes(self._out)


def packbits_decode(encoded: bytes) -> bytes:
    decoder = PackbitsDecoder()
    decoder.feed(encoded)
    return decoder.decoded()


def iter_chunks(data: bytes):
    """Yield STREAM_DATA packets: the command byte plus up to DATA_CHUNK bytes."""
    for offset in range(0, len(data), DATA_CHUNK):
        yield bytes([CMD_STREAM_DATA]) + data[offset:offset + DATA_CHUNK]


def end_request(plane: bytes, flags: int) -> bytes:
    """Build a STREAM_END request from the *raw* plane.

    The device verifies the decoded byte count and sum, so encoding is
    invisible to the check.
    """
    return (bytes([CMD_STREAM_END]) + len(plane).to_bytes(2, "little")
            + checksum(plane).to_bytes(4, "little") + bytes([flags]))
