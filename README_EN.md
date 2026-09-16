# EPD42

[中文](README.md)

4.2" e-ink firmware plus three companion hosts: push AI subscription quotas, a pomodoro timer, or any picture onto an e-ink panel hacked from a price tag. The host composes the full 400x300 bitmap and compresses it; the firmware only streams the packed bits straight into the panel's RAM — no fonts, no framebuffer, no layout on the device.

The [web host](https://no010.github.io/epd42/) works out of the box (Chrome/Edge, Web Bluetooth), nothing to install; for heavier use pick the desktop app or a CLI tool (see [Companion hosts](#companion-hosts)).

Theoretically supports all nRF51 series MCUs, with three built-in Waveshare 4.2" e-ink drivers (switchable), custom pin mapping from panel to MCU, and wake-from-sleep on external events (NFC field detectors / wireless-charge power signals).

## Web host

Address: <https://no010.github.io/epd42/> (this repository's GitHub Pages, auto-deployed from main)

One page, four kinds of content, pushed over Web Bluetooth:

- **Pomodoro**: hourglass progress + remaining minutes + per-round dots + today's count; the timer lives in localStorage and pushes automatically on actions and phase changes.
- **LLM usage cards**: up to 3 cards (DeepSeek / Kimi / Aliyun TokenPlan layout); type the numbers or copy them from the CLI tool. Every percentage reads as *usage*.
- **Image**: upload + dithering (Bayer / Floyd–Steinberg), pushed compressed.
- **Debug**: driver select, clear, raw commands, and the power-mode switch.

The page PackBits-encodes each frame and streams it with the new protocol (`0xB0`–`0xB5`) — about 190 packets, ~6 s per frame.

## Companion hosts

| Tool | Form | Purpose | Docs |
|---|---|---|---|
| Web host | browser (Web Bluetooth) | pomodoro / usage cards / images / debug, zero install | link above |
| [desktop](desktop/) | Tauri desktop app (Windows) | pomodoro: tray, notifications, auto-push, statistics | [desktop/README.md](desktop/README.md) |
| [tools/epd-monitor](tools/epd-monitor/) | Python CLI | AI subscription monitor: scrapes logged-in consoles (DeepSeek / Kimi / Bailian TokenPlan) on a schedule | [tools/epd-monitor/README.md](tools/epd-monitor/README.md) |
| [tools/epd-pomodoro](tools/epd-pomodoro/) | Python CLI | pomodoro in the terminal, pushes on demand | [tools/epd-pomodoro/README.md](tools/epd-pomodoro/README.md) |

## Power modes

Selected with `EPD_CMD_SET_POWER (0x93)`, persisted in the device's config page:

- **resident** (default): after a refresh the MCU stays in System ON and keeps advertising — for pomodoro timers and quota monitors that push periodically.
- **deep sleep**: a verified push only *arms* the countdown; once the link has been **disconnected and idle for the grace period** (default 60 s, 0–255 s) the panel sleeps and the MCU enters System OFF. Reconnecting inside the window cancels the countdown, so several frames can ride one connection. The image stays on the e-ink panel at the lowest possible current.
- **wake-up**: the reset pin, or the configured `wakeup` pin (GPIO SENSE) — wire an NFC field detector, a wireless-charge power-good line or a reed switch to it. Waking is a cold boot; the next frame re-runs the panel Init() as usual.

Switch with `epd_monitor.py setmode --mode deep --sleep-after 60`, or in the web host's debug tab.

## Supported Devices

The hardware is hacked from a 4.2" e-ink price tag available on e-commerce platforms, you can [click here to buy](https://item.taobao.com/item.htm?ft=t&id=874071462547); both black-and-white and black-white-red versions are supported.

- Black and White Dual-Color Version

  ```
  MCU: nRF51822
  RAM: 16K
  ROM: 128K

  Driver: UC8176 (EPD_4in2)
  Screen Pins: 0508090A0B0C0D
  Coil Pins: 07
  ```

  ![](html/images/1.jpg)

- Black, White and Red Tri-Color Version

  ```
  MCU: nRF51802
  RAM: 16K
  ROM: 256K

  Driver: UC8276C (EPD_4in2b_V2)
  Screen Pins: 0A0B0C0D0E0F10
  Coil Pins: 09
  LED Pins: 03/04/05 (three LEDs, any one can be used)
  ```

  ![](html/images/2.jpg)

The default driver and pin mapping are for the black and white dual-color version, other versions need to switch drivers and modify pin mapping.

## Protocol

Images travel over the stream protocol `0xB0`–`0xB5` (BEGIN/DATA/END/ABORT/GET_STATUS); the full
specification lives in [tools/epd-monitor/README.md](tools/epd-monitor/README.md#protocol). Highlights: the
15000-byte plane is PackBits-compressed first (a typical UI compresses to ~23%), 19 bytes per
packet, checksum verified over the decoded plane with no refresh on failure; acks arrive as
notifications, not write responses. The legacy raw panel commands (`0x00`–`0x06`) are kept for
bring-up debugging.

## Development

> **Notice:**
> - Local development still uses [Keil 5.36](https://img.anfulai.cn/bbs/96992/MDK536.EXE) or earlier, while this repository now also provides an `arm-none-eabi-gcc` build path for GitHub Actions automation.

## Compilation Targets

- `nRF51822_xxAB`: Used to compile nRF51822 firmware, with built-in black and white dual-color version configuration
- `nRF51802_xxAA`: Used to compile nRF51802 firmware, with built-in black, white, and red tri-color version configuration
- `flash_softdevice` Target: Used to flash the Bluetooth protocol stack (only needs to be flashed once)

### GitHub Actions / GCC build

- Workflow: `.github/workflows/firmware.yml`
- Build script: `python3 tools/build_firmware.py`
- Output directory: `build/<target>/`

Examples:

```bash
python3 tools/build_firmware.py --target nRF51822_xxAB
python3 tools/build_firmware.py --target nRF51802_xxAA
```

Generated artifacts:

- `build/nRF51822_xxAB/epd42-bw.{elf,hex,bin,map}`
- `build/nRF51802_xxAA/epd42-bwr.{elf,hex,bin,map}`
- `build/nRF51822_xxAB/epd42-bw-merged.hex`
- `build/nRF51802_xxAA/epd42-bwr-merged.hex`

Notes:

- The regular `*.hex` / `*.bin` artifacts still contain the application firmware only.
- `*-merged.hex` combines the application firmware with the Nordic SoftDevice binary from `components/softdevice/s130/hex/s130_nrf51_2.0.1_softdevice.hex`.
- The current nRF51 targets depend on `S130`, not `s132`.

You can use J-Link or DAPLink as the programmer (you can use [RTTView](https://github.com/XIVN1987/RTTView) to view RTT logs).

**Flashing Process:**

> If you do not modify the code, it is recommended to download the binary firmware from [Releases](https://github.com/no010/epd42/releases) for immediate use.

1. Erase all (if Keil cannot erase, try using the programmer's upper computer software to erase)
2. Choose one:
   - Switch to the `flash_softdevice` Target corresponding to the MCU, **do not compile, just download** (only needs to be flashed once), then flash the regular application `*.hex` / `*.bin`
   - Flash the corresponding `*-merged.hex` directly
3. If you use the regular Target, compile first and then download

## Acknowledgements

- This project is forked from [EPD-nRF51](https://github.com/tsl0922/EPD-nRF51), thanks to the original author for sharing and contributing.
