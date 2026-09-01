#!/usr/bin/env python3
"""EPD Monitor — PC companion for the EPD42 display.

The screen is composed here, not on the device: providers are polled, the
result is drawn into a 400x300 monochrome frame, and the packed bits are
streamed to the panel over BLE.

Usage:
    python epd_monitor.py push              # fetch + stream once
    python epd_monitor.py daemon            # loop forever, push on interval
    python epd_monitor.py render            # write frame.bin / preview.png only
    python epd_monitor.py status            # print current values, no BLE
    python epd_monitor.py scan              # scan for nearby BLE devices
    python epd_monitor.py describe          # dump the device's GATT services

Common options:
    --config PATH     Config file (default: config.toml)
    --no-ble          Fetch data but skip the BLE push (dry-run)
    --demo            Use fake items; needs no config file or API keys
    --verbose / -v    Verbose logging
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import config as cfg_module
import protocol
import providers as prov_module
import render
from providers import SubscriptionItem

logger = logging.getLogger("epd_monitor")

DEMO_ITEMS = [
    SubscriptionItem("Copilot Pro", 1500, 375, 0, "req"),
    SubscriptionItem("Kimi", 0, 0, 1234, "CNY"),
    SubscriptionItem("DeepSeek", 2_000_000, 1_234_567, 0, "tkn"),
]


# ── helpers ─────────────────────────────────────────────────────────────────

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, level.upper(), logging.INFO),
    )


async def _fetch_all(provider_cfgs: list[dict]) -> list:
    """Fetch items from all configured providers; skip failed ones."""
    all_items = []
    for pcfg in provider_cfgs:
        provider = prov_module.create(pcfg)
        try:
            items = await provider.fetch()
            all_items.extend(items)
            logger.info("[%s] fetched %d item(s)", provider.name, len(items))
        except prov_module.ProviderError as exc:
            logger.warning("%s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Unexpected error: %s: %s",
                         provider.name, type(exc).__name__, exc)
    return all_items[:render.MAX_ITEMS]


async def _collect_items(cfg: dict, demo: bool) -> list:
    if demo:
        return list(DEMO_ITEMS)
    return await _fetch_all(cfg["providers"])


def _print_items(items: list) -> None:
    if not items:
        print("No data fetched.")
        return
    print(f"\n{'─'*50}")
    print(f"  {'Plan':<16}  {'Used':>10}  {'Total':>10}  {'Balance':>12}  Unit")
    print(f"{'─'*50}")
    for it in items:
        bal = f"{it.balance/100:.2f}" if it.balance else "—"
        total = str(it.quota_total) if it.quota_total else "—"
        used = str(it.quota_used) if it.quota_used else "—"
        print(f"  {it.plan_name:<16}  {used:>10}  {total:>10}  {bal:>12}  {it.unit}")
    print(f"{'─'*50}\n")


# ── commands ────────────────────────────────────────────────────────────────

async def cmd_push(cfg: dict, no_ble: bool, demo: bool) -> None:
    items = await _collect_items(cfg, demo)
    _print_items(items)
    if no_ble:
        print("--no-ble: skipping BLE push.")
        return
    if not items:
        print("Nothing to push.")
        return
    from ble_client import push_frame

    await push_frame(items, cfg, fast=bool(cfg.get("fast_write")),
                     title=str(cfg.get("title", "SUB MONITOR")),
                     font_path=cfg.get("font_path") or None)


async def cmd_render(cfg: dict, demo: bool, out: Path, preview: Path,
                     driver_id: int) -> None:
    items = await _collect_items(cfg, demo)
    _print_items(items)
    image = render.compose(items, title=str(cfg.get("title", "SUB MONITOR")),
                           font_path=cfg.get("font_path") or None)
    image.save(preview)
    planes = render.pack_planes(image, driver_id,
                                planes=2 if driver_id == 3 else 1)
    out.write_bytes(b"".join(plane.data for plane in planes))
    encoded = sum(len(protocol.packbits_encode(plane.data)) for plane in planes)
    print(f"{len(items)} item(s) → {preview} and {out} "
          f"({out.stat().st_size} bytes, encoded {encoded} bytes = "
          f"{-(-encoded // protocol.DATA_CHUNK)} packets, "
          f"checksum {render.checksum(planes[0].data)})")


async def cmd_status(cfg: dict, demo: bool) -> None:
    _print_items(await _collect_items(cfg, demo))


async def cmd_daemon(cfg: dict, no_ble: bool, demo: bool) -> None:
    interval = int(cfg["refresh_interval"])
    print(f"Daemon mode — pushing every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            await cmd_push(cfg, no_ble, demo)
        except Exception as exc:  # noqa: BLE001
            logger.error("Push failed: %s", exc)
        logger.info("Sleeping %ds…", interval)
        await asyncio.sleep(interval)


async def cmd_scan(scan_timeout: float) -> None:
    from bleak import BleakScanner
    print(f"Scanning for BLE devices ({scan_timeout:.0f}s)…\n")
    # bleak 3.x moved RSSI off BLEDevice and onto AdvertisementData.
    found = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)
    if not found:
        print("No devices found.")
        return
    print(f"{'Address':<20}  {'RSSI':>5}  Name")
    print("─" * 60)
    for device, adv in sorted(found.values(), key=lambda pair: pair[1].rssi, reverse=True):
        name = adv.local_name or device.name or "(unknown)"
        print(f"{device.address:<20}  {adv.rssi:>5}  {name}"
              + ("  <-- EPD42" if name.startswith("NRF_EPD") else ""))


# ── main ────────────────────────────────────────────────────────────────────

_NEEDS_CONFIG = {"push", "daemon", "status", "render"}
# These talk to the device or draw synthetic frames: config is welcome (device
# name, address) but never required.
_BLE_OR_OPTIONAL_CONFIG = {"scan", "describe", "render", "pattern", "setdriver", "fault", "login"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EPD42 subscription monitor — PC companion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=["push", "daemon", "status", "render", "scan", "describe",
                 "pattern", "setdriver", "fault", "login"],
        help="Command to run",
    )
    parser.add_argument(
        "--config", "-c",
        default=str(Path(__file__).parent / "config.toml"),
        help="Path to TOML config file (default: config.toml)",
    )
    parser.add_argument("--no-ble", action="store_true",
                        help="Fetch data but do not connect to BLE device (dry-run)")
    parser.add_argument("--demo", action="store_true",
                        help="Use built-in sample items; no config file or API keys needed")
    parser.add_argument("--out", type=Path, default=Path("frame.bin"),
                        help="render: where to write the packed frame")
    parser.add_argument("--preview", type=Path, default=Path("preview.png"),
                        help="render: where to write the PNG of the composed frame")
    parser.add_argument("--driver", type=int, default=2, choices=sorted(render.DRIVER_PLANES),
                        help="render/setdriver: driver id (1 = 4.2in, 2 = 4.2in V2 BW, 3 = BWR)")
    parser.add_argument("--name", default="white", choices=render.PATTERNS,
                        help="pattern: which test image to stream")
    parser.add_argument("--row", type=int, default=123,
                        help="pattern row-marker: which row to draw (0-299)")
    parser.add_argument("--fraction", type=float, default=0.5,
                        help="fault: how much of the plane to send before ENDing early")
    parser.add_argument("--provider", choices=["deepseek-web", "kimi-web", "aliyun-web"],
                        help="login: which web provider to sign in")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    cfg: dict = {}
    if args.command in _NEEDS_CONFIG and not args.demo:
        try:
            cfg = cfg_module.load(args.config)
        except FileNotFoundError:
            print(
                f"Config file not found: {args.config}\n"
                "Copy config.example.toml to config.toml and fill in your API keys, "
                "or pass --demo.",
                file=sys.stderr,
            )
            return 1
        except (ValueError, KeyError) as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 1
    elif args.command in _BLE_OR_OPTIONAL_CONFIG:
        if Path(args.config).exists():
            try:
                cfg = cfg_module.load(args.config, require_providers=False)
            except (ValueError, KeyError) as exc:
                print(f"Config error: {exc}", file=sys.stderr)
                return 1
        else:
            cfg = cfg_module.defaults()

    log_level = "DEBUG" if args.verbose else cfg.get("log_level", "INFO")
    _setup_logging(log_level)
    fast = bool(cfg.get("fast_write"))

    try:
        if args.command == "push":
            asyncio.run(cmd_push(cfg, args.no_ble, args.demo))
        elif args.command == "daemon":
            asyncio.run(cmd_daemon(cfg, args.no_ble, args.demo))
        elif args.command == "status":
            asyncio.run(cmd_status(cfg, args.demo))
        elif args.command == "render":
            asyncio.run(cmd_render(cfg, args.demo, args.out, args.preview, args.driver))
        elif args.command == "scan":
            asyncio.run(cmd_scan(float(cfg.get("scan_timeout", 10))))
        elif args.command == "describe":
            from ble_client import describe as ble_describe

            asyncio.run(ble_describe(cfg))
        elif args.command == "pattern":
            from ble_client import push_pattern

            asyncio.run(push_pattern(args.name, cfg, row=args.row, fast=fast))
        elif args.command == "setdriver":
            from ble_client import select_driver

            asyncio.run(select_driver(args.driver, cfg))
        elif args.command == "fault":
            from ble_client import fault_test

            return asyncio.run(fault_test(cfg, fraction=args.fraction))
        elif args.command == "login":
            from providers.webquota import RECIPES, open_login

            if not args.provider:
                print(f"login needs --provider, one of: {sorted(RECIPES)}", file=sys.stderr)
                return 1
            open_login(args.provider)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
