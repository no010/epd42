"""BLE client for EPD42 subscription monitor.

Connects to the EPD42 device via BLE and sends subscription data
using the EPD_CMD_SET_SUBSCRIPTION_DATA (0xA0) command, then
optionally triggers an immediate refresh with EPD_CMD_TRIGGER_REFRESH (0xA2).

Protocol (from EPD/EPD_ble.h):
  - Write 1 byte command + payload to the EPD characteristic
  - EPD service UUID: based on vendor-specific UUID type 0x0001
  - The characteristic supports Write + Notify

subscription_data_t layout (packed, little-endian):
  uint32  refresh_interval_sec
  uint8   item_count
  items[3]:
    char[16] plan_name
    uint32   quota_total
    uint32   quota_used
    uint32   balance
    char[4]  unit
    uint8    valid          (0xA5)
  char[16] last_update      ("MM-DD HH:MM\\0")
  uint8    valid_marker     (0xA5)
  uint8[3] _pad
  uint32   checksum
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from typing import Sequence

from bleak import BleakClient, BleakScanner

from providers import SubscriptionItem

logger = logging.getLogger(__name__)

# ── EPD service / characteristic UUIDs ──────────────────────────────────────
# The base UUID is Nordic's 128-bit vendor UUID from the original firmware.
# EPD_SERVICE_UUID  → 0001xxxx-0000-1000-8000-00805f9b34fb  (short UUID 0x0001)
# EPD_CHAR_UUID     → Nordic UART-style single characteristic
# The exact 128-bit UUID depends on the base registered in the SoftDevice;
# for the no010/epd42 firmware it uses Nordic's base UUID type with short 0x0001.
EPD_BASE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"   # Nordic UART Service UUID
EPD_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # NUS RX characteristic

# EPD command bytes
EPD_CMD_SET_SUBSCRIPTION_DATA = 0xA0
EPD_CMD_TRIGGER_REFRESH       = 0xA2

# Validity marker
SUBSCRIPTION_VALID_MARKER = 0xA5

# Maximum items the firmware supports
SUBSCRIPTION_MAX_ITEMS = 3

# BLE MTU default leaves 20 bytes per write; we chunk as needed
_WRITE_CHUNK = 20


@dataclass
class SubscriptionPayload:
    items: list[SubscriptionItem]
    refresh_interval_sec: int = 1800
    last_update: str = ""


def _pack_subscription(payload: SubscriptionPayload) -> bytes:
    """Serialise subscription_data_t to bytes (packed little-endian)."""
    items = payload.items[:SUBSCRIPTION_MAX_ITEMS]
    refresh = payload.refresh_interval_sec
    item_count = len(items)

    ts = payload.last_update or time.strftime("%m-%d %H:%M")
    last_update_bytes = ts[:15].encode("ascii", errors="replace").ljust(16, b"\x00")

    # Pack items
    item_bytes = b""
    for item in items:
        name = item.plan_name[:15].encode("ascii", errors="replace").ljust(16, b"\x00")
        unit = item.unit[:3].encode("ascii", errors="replace").ljust(4, b"\x00")
        item_bytes += struct.pack(
            "<16sIII4sB",
            name,
            item.quota_total & 0xFFFFFFFF,
            item.quota_used  & 0xFFFFFFFF,
            item.balance     & 0xFFFFFFFF,
            unit,
            SUBSCRIPTION_VALID_MARKER,
        )
    # Pad remaining item slots
    item_struct_size = 16 + 4 + 4 + 4 + 4 + 1  # = 33 bytes
    item_bytes = item_bytes.ljust(SUBSCRIPTION_MAX_ITEMS * item_struct_size, b"\x00")

    # Build full structure (without checksum first)
    body = struct.pack("<IB", refresh, item_count) + item_bytes + last_update_bytes
    body += bytes([SUBSCRIPTION_VALID_MARKER]) + bytes(3)  # valid_marker + _pad[3]

    # Compute simple sum checksum over all bytes so far
    checksum = sum(body) & 0xFFFFFFFF
    body += struct.pack("<I", checksum)

    return body


async def _find_device(device_name: str, timeout: float = 10.0):
    """Scan for BLE device by name, return BleakDevice or None."""
    logger.info("Scanning for '%s' (%.0fs timeout)…", device_name, timeout)
    device = await BleakScanner.find_device_by_name(device_name, timeout=timeout)
    return device


async def send_subscription(
    items: Sequence[SubscriptionItem],
    *,
    device_name: str = "EPD42",
    device_address: str = "",
    refresh_interval: int = 1800,
    trigger_refresh: bool = True,
    scan_timeout: float = 15.0,
) -> None:
    """Find the EPD42 device and push subscription data via BLE.

    Args:
        items: Up to 3 subscription items to display.
        device_name: BLE advertisement name to scan for.
        device_address: Optional fixed MAC/UUID (skips scan when provided).
        refresh_interval: Seconds between auto-refreshes stored on device.
        trigger_refresh: Send TRIGGER_REFRESH command after pushing data.
        scan_timeout: Seconds to wait during BLE scan.
    """
    payload = SubscriptionPayload(
        items=list(items),
        refresh_interval_sec=refresh_interval,
    )
    data_bytes = _pack_subscription(payload)
    # Build the BLE write: command byte + payload
    command_packet = bytes([EPD_CMD_SET_SUBSCRIPTION_DATA]) + data_bytes

    if device_address:
        device = device_address
    else:
        device = await _find_device(device_name, timeout=scan_timeout)
        if device is None:
            raise RuntimeError(
                f"EPD42 device '{device_name}' not found. "
                "Ensure the device is powered on and advertising."
            )

    logger.info("Connecting to %s…", device)
    async with BleakClient(device, timeout=20) as client:
        logger.info("Connected. Writing %d bytes…", len(command_packet))
        # BLE characteristics have a max write size; chunk if needed
        for offset in range(0, len(command_packet), _WRITE_CHUNK):
            chunk = command_packet[offset: offset + _WRITE_CHUNK]
            await client.write_gatt_char(EPD_CHAR_UUID, chunk, response=True)
            await asyncio.sleep(0.05)

        if trigger_refresh:
            logger.info("Sending TRIGGER_REFRESH…")
            await asyncio.sleep(0.1)
            await client.write_gatt_char(
                EPD_CHAR_UUID, bytes([EPD_CMD_TRIGGER_REFRESH]), response=True
            )

    logger.info("Done — subscription data pushed to EPD42.")
