#!/usr/bin/env python3
"""EPD Monitor — PC companion for the EPD42 subscription display.

Usage:
    python epd_monitor.py push              # fetch + push once
    python epd_monitor.py status            # print current values, no BLE
    python epd_monitor.py daemon            # loop forever, push on interval
    python epd_monitor.py scan              # scan for nearby BLE devices

Common options:
    --config PATH     Config file (default: config.toml)
    --no-ble          Fetch data but skip BLE push (dry-run)
    --verbose / -v    Verbose logging
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import config as cfg_module
import providers as prov_module
from ble_client import send_subscription

logger = logging.getLogger("epd_monitor")


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
            logger.info("[%s] ✓ %d item(s)", provider.name, len(items))
        except prov_module.ProviderError as exc:
            logger.warning("%s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Unexpected error: %s", provider.name, exc)
    return all_items[:3]  # EPD supports max 3 items


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

async def cmd_push(cfg: dict, no_ble: bool) -> None:
    items = await _fetch_all(cfg["providers"])
    _print_items(items)
    if no_ble:
        print("--no-ble: skipping BLE push.")
        return
    if not items:
        print("Nothing to push.")
        return
    await send_subscription(
        items,
        device_name=cfg["device_name"],
        device_address=cfg.get("device_address", ""),
        refresh_interval=int(cfg["refresh_interval"]),
        trigger_refresh=bool(cfg["trigger_refresh"]),
        scan_timeout=float(cfg["scan_timeout"]),
    )


async def cmd_status(cfg: dict) -> None:
    items = await _fetch_all(cfg["providers"])
    _print_items(items)


async def cmd_daemon(cfg: dict, no_ble: bool) -> None:
    interval = int(cfg["refresh_interval"])
    print(f"Daemon mode — refreshing every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            await cmd_push(cfg, no_ble)
        except Exception as exc:  # noqa: BLE001
            logger.error("Push failed: %s", exc)
        logger.info("Sleeping %ds…", interval)
        await asyncio.sleep(interval)


async def cmd_scan(scan_timeout: float) -> None:
    from bleak import BleakScanner
    print(f"Scanning for BLE devices ({scan_timeout:.0f}s)…\n")
    devices = await BleakScanner.discover(timeout=scan_timeout)
    if not devices:
        print("No devices found.")
        return
    print(f"{'Address':<20}  {'RSSI':>5}  Name")
    print("─" * 60)
    for d in sorted(devices, key=lambda x: x.rssi or -999, reverse=True):
        print(f"{d.address:<20}  {d.rssi or '?':>5}  {d.name or '(unknown)'}")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="EPD42 subscription monitor — PC companion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=["push", "status", "daemon", "scan"],
        help="Command to run",
    )
    parser.add_argument(
        "--config", "-c",
        default=str(Path(__file__).parent / "config.toml"),
        help="Path to TOML config file (default: config.toml)",
    )
    parser.add_argument(
        "--no-ble",
        action="store_true",
        help="Fetch data but do not connect to BLE device (dry-run)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Load config (not needed for scan)
    cfg: dict = {}
    if args.command != "scan":
        try:
            cfg = cfg_module.load(args.config)
        except FileNotFoundError:
            print(
                f"Config file not found: {args.config}\n"
                "Copy config.example.toml to config.toml and fill in your API keys.",
                file=sys.stderr,
            )
            return 1
        except (ValueError, KeyError) as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 1

    log_level = "DEBUG" if args.verbose else cfg.get("log_level", "INFO")
    _setup_logging(log_level)

    try:
        if args.command == "push":
            asyncio.run(cmd_push(cfg, args.no_ble))
        elif args.command == "status":
            asyncio.run(cmd_status(cfg))
        elif args.command == "daemon":
            asyncio.run(cmd_daemon(cfg, args.no_ble))
        elif args.command == "scan":
            asyncio.run(cmd_scan(float(cfg.get("scan_timeout", 10))))
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
