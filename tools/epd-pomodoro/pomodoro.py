#!/usr/bin/env python3
"""EPD42 番茄钟 —— 命令行上位机（PC companion）。

在终端里倒计时，需要时把当前状态画成 400x300 单色画面，通过 BLE 推送到
EPD42 墨水屏 —— 与 tools/epd-monitor 共用同一套 wire protocol 和绘图原语
（render / protocol / ble_client / config 从同级目录直接导入，不复制代码）。

用法：
    python pomodoro.py start                  # 交互式倒计时
    python pomodoro.py start --push           # 阶段切换 + 默认每 3 分钟推送画面
    python pomodoro.py push                   # 把当前状态推一次
    python pomodoro.py render --demo          # 只预览，不连蓝牙
    python pomodoro.py status                 # 查看当前状态
    python pomodoro.py scan                   # 扫描附近的 BLE 设备
    python pomodoro.py setdriver --driver 3   # 把设备指向实际安装的屏幕驱动

文件：
    state.json    计时状态，每 tick 写入；status/push/render 从这里读
    preview.png   render / --no-ble 的预览输出
    frame.bin     render 打包后的平面数据
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
import time
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(
    0, str(_TOOLS / "epd-monitor")
)  # shared render/protocol/ble_client/config

import config as epd_config  # noqa: E402  sibling tool's config loader
import protocol  # noqa: E402  wire constants & PackBits
import render  # noqa: E402  plane packing & fonts
import state as stm  # noqa: E402  local: the timer state machine
import face  # noqa: E402  local: the 400x300 e-ink face

logger = logging.getLogger("epd-pomodoro")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "state.json"
DEMO_REMAINING = 12 * 60 + 30  # a mid-work snapshot for --demo: sand half spent
DEFAULT_PUSH_INTERVAL_S = 180  # e-ink refreshes every few minutes, not every second


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def _require_bleak() -> None:
    """BLE 推送/扫描是可选功能：缺少 bleak 时给出安装提示而不是堆栈。"""
    try:
        import bleak  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "蓝牙功能需要 bleak：python -m pip install bleak（只渲染/计时无需安装）"
        ) from exc


def _load_cfg(path: Path) -> dict:
    if path.exists():
        try:
            return epd_config.load(path, require_providers=False)
        except (ValueError, KeyError) as exc:
            logger.warning("config error (%s); using defaults", exc)
    return epd_config.defaults()


def _state_path(cfg: dict, arg: str | None) -> Path:
    if arg:
        return Path(arg)
    return Path(str(cfg.get("state_file") or DEFAULT_STATE_FILE))


def _durations(cfg: dict, args) -> dict:
    return {
        "work_minutes": args.work
        or cfg.get("work_minutes", stm.DEFAULTS["work_minutes"]),
        "short_minutes": args.short
        or cfg.get("short_minutes", stm.DEFAULTS["short_minutes"]),
        "long_minutes": args.long
        or cfg.get("long_minutes", stm.DEFAULTS["long_minutes"]),
        "rounds": args.rounds or cfg.get("rounds", stm.DEFAULTS["rounds"]),
    }


def _demo_state(cfg: dict) -> stm.PomodoroState:
    state = stm.PomodoroState.from_cfg(cfg)
    state.remaining = DEMO_REMAINING
    state.running = True
    state.pomodoro_count = 2
    state.cycle_total = 7
    return state


def _push_face(
    state: stm.PomodoroState, cfg: dict, *, no_ble: bool, preview: Path
) -> None:
    """Compose the face and stream it; with ``no_ble`` only write the preview."""
    image = face.compose(state, font_path=cfg.get("font_path") or None)
    if no_ble:
        image.save(preview)
        print(f"preview saved to {preview} (BLE skipped: --no-ble)")
        return
    _require_bleak()  # only an actual BLE push needs it
    from ble_client import push_image  # lazy: keeps BLE out of render-only paths

    asyncio.run(push_image(image, cfg, fast=bool(cfg.get("fast_write"))))
    logger.info("frame pushed")


def _push_face_quiet(state: stm.PomodoroState, cfg: dict, args) -> bool:
    """Push, but never kill the countdown: failures are logged only."""
    try:
        _push_face(state, cfg, no_ble=args.no_ble, preview=args.preview)
        return True
    except Exception as exc:
        logger.error("推送失败（计时继续）: %s", exc)
        return False


def _read_key() -> str | None:
    """Read one key without blocking; None when nothing is pressed."""
    if os.name == "nt":
        import msvcrt

        try:
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow / function key: eat the scan code
                msvcrt.getwch()
                return None
            return ch
        except OSError:
            return None
    import select

    if not select.select([sys.stdin], [], [], 0)[0]:
        return None
    try:
        return sys.stdin.read(1) or None
    except (OSError, ValueError):
        return None


def _render_cli(state: stm.PomodoroState) -> None:
    zh, _ = stm.PHASE_NAMES[state.phase]
    icon = "▶" if state.running else "⏸"
    line = (
        f"{icon} {zh} {stm.mmss(state.remaining)}  "
        f"第 {state.pomodoro_count}/{state.rounds} 个  今日 {state.cycle_total}"
    )
    sys.stdout.write("\r" + line.ljust(56))
    sys.stdout.flush()


def _announce(state: stm.PomodoroState, previous_phase: str) -> None:
    zh, _ = stm.PHASE_NAMES[state.phase]
    prev_zh, _ = stm.PHASE_NAMES[previous_phase]
    print(f"\n  {prev_zh}结束 → {zh}（{stm.mmss(state.remaining)}）")
    sys.stdout.write("\a")  # terminal bell
    sys.stdout.flush()


# ── commands ───────────────────────────────────────────────────────────────


def cmd_start(cfg: dict, args) -> int:
    state_path = _state_path(cfg, args.state)
    durations = _durations(cfg, args)
    rounds = max(1, int(durations["rounds"]))

    state = stm.PomodoroState.load(state_path)
    if state is None:
        state = stm.PomodoroState.from_cfg(durations)
        print(
            f"新会话：专注 {stm.mmss(state.phase_seconds)}，"
            f"每 {rounds} 个番茄一次长休息。"
        )
    else:
        state.rounds = rounds
        if not state.running:
            print(f"继续上次暂停的计时：专注 {stm.mmss(state.remaining)}。")
    # "start" 就是要开始/继续计时；暂停按空格，阶段结束想等按键用 --manual
    if not state.running:
        state.running = True
    state.save(state_path)

    interactive = not args.no_input and sys.stdin.isatty()
    push_enabled = bool(args.push or (args.push_interval or 0) > 0)
    interval_arg = args.push_interval
    if interval_arg is None:
        interval_arg = cfg.get("push_interval", DEFAULT_PUSH_INTERVAL_S)
    interval = max(0, int(interval_arg))

    restore = None
    if interactive and os.name != "nt":
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)

            def restore() -> None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            interactive = False

    deadline = time.monotonic() + state.remaining
    if push_enabled:
        _push_face_quiet(state, cfg, args)
        deadline = time.monotonic() + state.remaining
    next_push = time.monotonic() + max(interval, 1)

    if interactive:
        print(
            "\n  空格/p 暂停|继续   r 重置本阶段   n 跳到下一阶段"
            "   s 推送到屏幕   q 退出"
        )
    try:
        while True:
            now = time.monotonic()
            if state.running:
                state.remaining = max(0, math.ceil(deadline - now))
                if state.remaining <= 0:
                    previous = state.phase
                    stm.advance(state, durations)
                    if args.manual:
                        state.running = False
                    state.save(state_path)
                    _announce(state, previous)
                    if push_enabled:
                        _push_face_quiet(state, cfg, args)
                    deadline = time.monotonic() + state.remaining
                    next_push = time.monotonic() + max(interval, 1)
                elif interval > 0 and now >= next_push:
                    _push_face_quiet(state, cfg, args)
                    next_push = now + interval
            if interactive:
                key = _read_key()
                if key is not None:
                    now = time.monotonic()
                    if key in (" ", "p", "P"):
                        if state.running:
                            state.remaining = max(1, math.ceil(deadline - now))
                            state.running = False
                        else:
                            state.running = True
                            deadline = time.monotonic() + state.remaining
                            print("\n  ▶ 开始")
                        state.save(state_path)
                    elif key in ("r", "R"):
                        state.remaining = stm.phase_seconds_for(state.phase, durations)
                        state.running = False
                        deadline = time.monotonic() + state.remaining
                        state.save(state_path)
                        print("\n  已重置当前阶段。")
                    elif key in ("n", "N"):
                        previous = state.phase
                        stm.skip(state, durations)
                        state.running = not args.manual
                        deadline = time.monotonic() + state.remaining
                        state.save(state_path)
                        _announce(state, previous)
                        if push_enabled:
                            _push_face_quiet(state, cfg, args)
                        next_push = time.monotonic() + max(interval, 1)
                    elif key in ("s", "S"):
                        ok = _push_face_quiet(state, cfg, args)
                        print("\n  ✓ 已推送" if ok else "\n  ✗ 推送失败（查看日志）")
                    elif key in ("q", "Q", "\x1b"):
                        state.save(state_path)
                        print("\n  已保存并退出。")
                        return 0
                _render_cli(state)
            time.sleep(0.2)
    except KeyboardInterrupt:
        state.save(state_path)
        print("\n  已保存，Ctrl-C 退出。")
        return 0
    finally:
        if restore is not None:
            restore()


def cmd_status(state_path: Path) -> int:
    state = stm.PomodoroState.load(state_path)
    if state is None:
        print(
            "还没有状态文件（state.json 不存在或不可读）。"
            "\n先运行 `python pomodoro.py start`，"
            "或 `python pomodoro.py render --demo` 预览。"
        )
        return 0
    zh, en = stm.PHASE_NAMES[state.phase]
    print(f"阶段   {zh} {en}")
    print(f"剩余   {stm.mmss(state.remaining)} / {stm.mmss(state.phase_seconds)}")
    print(f"状态   {'运行中' if state.running else '已暂停'}")
    print(f"本轮   {state.pomodoro_count}/{state.rounds} 个番茄")
    print(f"今日   {state.cycle_total} 个番茄")
    return 0


def cmd_render(cfg: dict, args) -> int:
    state = None if args.demo else stm.PomodoroState.load(_state_path(cfg, args.state))
    if state is None:
        state = _demo_state(_durations(cfg, args))
        print(
            "（无状态文件：使用示例状态 "
            f"{stm.mmss(DEMO_REMAINING)}；加 --state 指定或先运行 start）"
        )
    image = face.compose(state, font_path=cfg.get("font_path") or None)
    image.save(args.preview)
    planes = render.pack_planes(image, args.driver, planes=2 if args.driver == 3 else 1)
    payload = b"".join(plane.data for plane in planes)
    args.out.write_bytes(payload)
    encoded = sum(len(protocol.packbits_encode(plane.data)) for plane in planes)
    packets = -(-encoded // protocol.DATA_CHUNK)
    print(
        f"{len(planes)} 个平面 → {args.preview} + {args.out} "
        f"({len(payload)} 字节, 压缩后 {encoded} 字节 = {packets} 包, "
        f"checksum 0x{render.checksum(planes[0].data):08x})"
    )
    return 0


def cmd_push(cfg: dict, args) -> int:
    if args.demo:
        state = _demo_state(_durations(cfg, args))
    else:
        state = stm.PomodoroState.load(_state_path(cfg, args.state))
        if state is None:
            print("没有可推送的状态：先运行 start，或加 --demo。", file=sys.stderr)
            return 1
    try:
        _push_face(state, cfg, no_ble=args.no_ble, preview=args.preview)
    except Exception as exc:
        print(f"推送失败：{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_reset(state_path: Path) -> int:
    if state_path.exists():
        state_path.unlink()
        print(f"已清除 {state_path}")
    else:
        print("没有状态文件可清除。")
    return 0


def cmd_scan(scan_timeout: float) -> int:
    _require_bleak()
    from bleak import BleakScanner

    print(f"扫描 BLE 设备（{scan_timeout:.0f}s）…\n")
    found = asyncio.run(BleakScanner.discover(timeout=scan_timeout, return_adv=True))
    if not found:
        print("没有发现设备。")
        return 0
    print(f"{'地址':<22} {'RSSI':>5}  名称")
    print("─" * 62)
    for device, adv in sorted(
        found.values(), key=lambda pair: pair[1].rssi, reverse=True
    ):
        name = adv.local_name or device.name or "(unknown)"
        marker = "  <-- EPD42" if name.startswith("NRF_EPD") else ""
        print(f"{device.address:<22} {adv.rssi:>5}  {name}{marker}")
    return 0


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EPD42 番茄钟 —— 命令行上位机",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=[
            "start",
            "status",
            "push",
            "render",
            "reset",
            "scan",
            "describe",
            "setdriver",
        ],
        help="要执行的命令",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=str(Path(__file__).parent / "config.toml"),
        help="TOML 配置文件（默认 config.toml；可不建，全部用默认值）",
    )
    parser.add_argument("--state", default=None, help="计时状态文件（默认 state.json）")
    parser.add_argument("--work", type=int, default=0, help="专注时长，分钟（默认 25）")
    parser.add_argument(
        "--short", type=int, default=0, help="短休息时长，分钟（默认 5）"
    )
    parser.add_argument(
        "--long", type=int, default=0, help="长休息时长，分钟（默认 15）"
    )
    parser.add_argument(
        "--rounds", type=int, default=0, help="几个番茄后进入长休息（默认 4）"
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="start：阶段切换（及 --push-interval）时推送画面到墨水屏",
    )
    parser.add_argument(
        "--push-interval",
        type=int,
        default=None,
        help="start --push：每隔 N 秒推送一次（默认 180 = 3 分钟；0 = 只在切换时）",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="start：阶段结束/跳过后暂停等待按键，不自动开始",
    )
    parser.add_argument(
        "--no-input", action="store_true", help="start：不读取键盘（无人值守/后台运行）"
    )
    parser.add_argument(
        "--no-ble", action="store_true", help="push/start：只渲染预览图，不连接设备"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="push/render：使用内置示例状态，不需要 state.json",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("frame.bin"), help="render：打包数据输出路径"
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=Path("preview.png"),
        help="render/start --no-ble：预览图输出路径",
    )
    parser.add_argument(
        "--driver",
        type=int,
        default=2,
        choices=sorted(render.DRIVER_PLANES),
        help="render/setdriver：驱动 id（1 = 4.2in BW，2 = 4.2in V2，3 = BWR）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="调试日志")
    args = parser.parse_args()

    cfg = _load_cfg(Path(args.config))
    logging_level = "DEBUG" if args.verbose else str(cfg.get("log_level", "INFO"))
    _setup_logging(logging_level)
    state_path = _state_path(cfg, args.state)

    try:
        if args.command == "start":
            return cmd_start(cfg, args)
        if args.command == "status":
            return cmd_status(state_path)
        if args.command == "push":
            return cmd_push(cfg, args)
        if args.command == "render":
            return cmd_render(cfg, args)
        if args.command == "reset":
            return cmd_reset(state_path)
        if args.command == "scan":
            return cmd_scan(float(cfg.get("scan_timeout", 15)))
        if args.command == "describe":
            _require_bleak()
            from ble_client import describe as ble_describe

            asyncio.run(ble_describe(cfg))
            return 0
        if args.command == "setdriver":
            _require_bleak()
            from ble_client import select_driver

            asyncio.run(select_driver(args.driver, cfg))
            return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
