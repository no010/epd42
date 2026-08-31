"""BLE client that streams a host-composed packed frame to the EPD42.

Protocol: EPD_CMD_STREAM_* (0xB0..0xB5), mirrored in ``protocol``.  The device
keeps no framebuffer, so every byte of a plane is forwarded to the panel as it
arrives.

Flow control relies on notifications, not on the GATT write response: the
SoftDevice answers writes on the application's behalf, so a write response
only proves the packet reached the link layer.  BEGIN and END therefore block
until the device notifies its ack, which happens after the panel has actually
been initialised or refreshed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Sequence

from bleak import BleakClient, BleakScanner

import protocol
import render
from providers import SubscriptionItem

logger = logging.getLogger(__name__)

ACK_TIMEOUT_S = 10.0


class EpdError(RuntimeError):
    """The device rejected the transfer or never answered."""


class EpdLink:
    """One open connection to an EPD42, speaking the stream protocol."""

    def __init__(self, client: BleakClient, char, acks: asyncio.Queue) -> None:
        self._client = client
        self._char = char
        self._acks = acks
        self.driver_id = 0
        self.plane_bytes = 0
        self.streaming = 0

    async def _write(self, payload: bytes, response: bool = True) -> None:
        await self._client.write_gatt_char(self._char, payload, response=response)

    async def _await_ack(self, command: int) -> bytes:
        """Return the next ack for ``command``, skipping unrelated notifications.

        The firmware also notifies its pin config as soon as the CCCD is
        written, so unsolicited packets must be dropped rather than mistaken
        for an ack.
        """
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            try:
                packet = await asyncio.wait_for(self._acks.get(), max(remaining, 0.01))
            except asyncio.TimeoutError:
                packet = b""
            if packet and packet[0] == command:
                return packet
            if remaining <= 0:
                raise EpdError(f"no ack for command 0x{command:02x} "
                               f"within {ACK_TIMEOUT_S:.0f}s")

    @staticmethod
    def _check_status(packet: bytes, what: str) -> None:
        status = packet[1] if len(packet) > 1 else 0xFF
        if status != protocol.STATUS_OK:
            raise EpdError(f"{what}: {protocol.describe_status(status)}")

    async def query_status(self) -> None:
        await self._write(bytes([protocol.CMD_GET_STATUS]))
        packet = await self._await_ack(protocol.CMD_GET_STATUS)
        if len(packet) < 8:
            raise EpdError(f"short status reply: {packet.hex()}")
        self.streaming = packet[protocol.STATUS_STREAMING]
        self.plane_bytes = int.from_bytes(packet[protocol.STATUS_PLANE_BYTES:
                                                 protocol.STATUS_PLANE_BYTES + 2], "little")
        self.driver_id = packet[protocol.STATUS_DRIVER]
        logger.info("device: driver=%d (%s), plane=%d bytes, plane in progress=%d",
                    self.driver_id, render.DRIVER_NAMES.get(self.driver_id, "unknown"),
                    self.plane_bytes, self.streaming)

    async def stream_plane(self, index: int, raw: bytes, *, last: bool,
                           fast: bool = False) -> None:
        """Push one packed plane.  Only the final plane triggers a refresh."""
        # Refreshing between planes would make the device re-run the panel
        # Init() for the next plane and erase what was just written.
        flags = (protocol.FLAG_REFRESH | protocol.FLAG_SLEEP) if last else 0
        encoded = protocol.packbits_encode(raw)

        await self._begin(index)

        started = time.monotonic()
        for packet in protocol.iter_chunks(encoded):
            await self._write(packet, response=not fast)
        elapsed = max(time.monotonic() - started, 1e-6)

        await self._write(protocol.end_request(raw, flags))
        self._check_status(await self._await_ack(protocol.CMD_STREAM_END), "STREAM_END")
        logger.info("plane %d: %d -> %d bytes (%.0f%%) in %.1fs, %d packets, %.1f kB/s",
                    index, len(raw), len(encoded), 100.0 * len(encoded) / len(raw),
                    elapsed, -(-len(encoded) // protocol.DATA_CHUNK),
                    len(encoded) / elapsed / 1024)

    async def _begin(self, index: int) -> None:
        await self._write(bytes([protocol.CMD_STREAM_BEGIN, index]))
        self._check_status(await self._await_ack(protocol.CMD_STREAM_BEGIN), "STREAM_BEGIN")

    async def stream_truncated(self, index: int, raw: bytes, fraction: float) -> int:
        """Send ``fraction`` of a plane, then END as if it were whole.

        Returns the device status rather than raising: the point is to confirm
        the device refuses to refresh a plane it cannot verify.
        """
        encoded = protocol.packbits_encode(raw)
        keep = max(1, int(len(encoded) * fraction))
        await self._begin(index)
        for packet in protocol.iter_chunks(encoded[:keep]):
            await self._write(packet)
        await self._write(protocol.end_request(raw, protocol.FLAG_REFRESH | protocol.FLAG_SLEEP))
        packet = await self._await_ack(protocol.CMD_STREAM_END)
        return packet[1]

    async def set_driver(self, driver_id: int) -> None:
        """Switch the panel driver the device stores in its config page."""
        await self._write(bytes([protocol.CMD_INIT, driver_id]))
        await self.query_status()

    async def abort(self) -> None:
        try:
            await self._write(bytes([protocol.CMD_STREAM_ABORT]))
            await self._await_ack(protocol.CMD_STREAM_ABORT)
        except EpdError:
            logger.debug("abort was not acked", exc_info=True)

    async def sleep(self) -> None:
        """Power the panel down before disconnecting, as the web host does."""
        try:
            await self._write(bytes([protocol.CMD_SLEEP]))
        except Exception:  # noqa: BLE001 - best effort, the link may be gone
            logger.debug("panel sleep command failed", exc_info=True)


async def _find_device(cfg: dict):
    address = cfg.get("device_address")
    if address:
        return address
    name = cfg.get("device_name", "NRF_EPD")
    timeout = float(cfg.get("scan_timeout", 15))
    logger.info("scanning for '%s*' (%.0fs)…", name, timeout)
    # The firmware advertises "NRF_EPD_<last two MAC bytes>", so accept prefix
    # matches as well as exact ones and take the strongest signal in range.
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches = [
        (adv.rssi, device) for device, adv in found.values()
        if (adv.local_name or device.name or "").startswith(name)
    ]
    if not matches:
        raise EpdError(
            f"no device advertising as '{name}…'. Run 'epd_monitor.py scan' to see "
            "what is in range, then set device_name or device_address in config.toml."
        )
    rssi, device = max(matches, key=lambda item: item[0])
    logger.info("found %s at %s (%d dBm)", device.name, device.address, rssi)
    return device


async def _find_char(client: BleakClient):
    """Locate the EPD characteristic, falling back to discovery by properties."""
    for service in client.services:
        if service.uuid.lower() == protocol.EPD_SERVICE_UUID:
            for char in service.characteristics:
                if "write" in char.properties and "notify" in char.properties:
                    return char
    # Vendor UUIDs differ between firmware forks, so accept the single
    # writable+notifiable characteristic that is not a Bluetooth SIG one.
    candidates = [
        char for service in client.services
        for char in service.characteristics
        if "write" in char.properties and "notify" in char.properties
        and not char.uuid.lower().startswith("0000")
    ]
    if len(candidates) == 1:
        logger.info("using discovered characteristic %s", candidates[0].uuid)
        return candidates[0]
    raise EpdError(
        f"no EPD characteristic found; service {protocol.EPD_SERVICE_UUID} absent "
        f"and {len(candidates)} generic candidates. Run 'epd_monitor.py describe'."
    )


@asynccontextmanager
async def epd_session(cfg: dict):
    """Yield a connected, subscribed :class:`EpdLink` with status already read."""
    acks: asyncio.Queue = asyncio.Queue()
    client = BleakClient(await _find_device(cfg), timeout=20)
    async with client:
        char = await _find_char(client)
        await client.start_notify(char, lambda _, data: acks.put_nowait(bytes(data)))
        link = EpdLink(client, char, acks)
        await link.query_status()
        yield link


async def push_image(image, cfg: dict, *, fast: bool = False) -> None:
    """Pack ``image`` for the panel the device reports, and stream it."""
    async with epd_session(cfg) as link:
        if link.plane_bytes != render.PLANE_BYTES:
            raise EpdError(f"device plane is {link.plane_bytes} bytes, renderer "
                           f"produces {render.PLANE_BYTES}")

        # A bi-colour panel needs its red plane too; a BW panel leaves it alone,
        # exactly like the working web host does.
        planes = render.pack_planes(image, link.driver_id,
                                    planes=2 if link.driver_id == 3 else 1)
        try:
            for position, plane in enumerate(planes):
                await link.stream_plane(plane.index, plane.data,
                                        last=position == len(planes) - 1, fast=fast)
        except EpdError:
            await link.abort()
            raise
        await link.sleep()

    logger.info("frame pushed")


async def push_frame(items: Sequence[SubscriptionItem], cfg: dict, *,
                     fast: bool = False, title: str = "SUB MONITOR",
                     font_path: str | None = None) -> None:
    """Compose ``items`` into a frame and stream it to the device."""
    await push_image(render.compose(items, title=title, font_path=font_path),
                     cfg, fast=fast)


async def push_pattern(name: str, cfg: dict, *, row: int = 123,
                       fast: bool = False) -> None:
    """Stream a synthetic pattern; see ``render.pattern`` for what each proves."""
    await push_image(render.pattern(name, row=row), cfg, fast=fast)


async def select_driver(driver_id: int, cfg: dict) -> None:
    """Point the device at the panel actually attached, and report the new id."""
    if driver_id not in render.DRIVER_PLANES:
        raise EpdError(f"unknown driver id {driver_id}; known: {sorted(render.DRIVER_PLANES)}")
    async with epd_session(cfg) as link:
        await link.set_driver(driver_id)
        print(f"driver is now {link.driver_id}: {render.DRIVER_NAMES.get(link.driver_id)}")


async def fault_test(cfg: dict, fraction: float = 0.5) -> int:
    """Half-send a plane, END as if complete, and check the device refuses it.

    Returns a process exit code: the plane must be rejected, and the frame
    pushed afterwards must still land, which is what proves the recovery path
    re-initialises the panel instead of continuing a half-written one.
    """
    plane = render.pack_plane(render.pattern("black"))
    async with epd_session(cfg) as link:
        status = await link.stream_truncated(0, plane, fraction)
        print(f"truncated plane ({fraction:.0%}) -> status {status} "
              f"({protocol.describe_status(status)})")
        rejected = status != protocol.STATUS_OK
        print("  screen must NOT have changed:", "expected" if rejected else "PROBLEM")

    # A rejected plane already reset the device's stream state, so this frame
    # exercises the automatic re-Init path.
    await push_pattern("white", cfg)
    print("recovery frame pushed; the panel should now be all paper")
    return 0 if rejected else 1


async def describe(cfg: dict) -> None:
    """Print every service and characteristic the device exposes."""
    client = BleakClient(await _find_device(cfg), timeout=20)
    async with client:
        print(f"device {client.address}")
        for service in client.services:
            marker = "  <-- expected EPD service" if service.uuid.lower() == protocol.EPD_SERVICE_UUID else ""
            print(f"  service {service.uuid}{marker}")
            for char in service.characteristics:
                print(f"    char    {char.uuid}  properties={char.properties}  "
                      f"handle={char.handle:#06x}")
