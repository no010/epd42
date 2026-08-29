# EPD Monitor — PC Companion

PC-side companion for the **EPD42 subscription monitor** firmware.

Periodically fetches usage / balance from AI providers (Kimi, DeepSeek, Zhipu, OpenAI and any custom provider), draws the result into a 400x300 monochrome frame, and streams the packed bits to an EPD42 e-ink panel over BLE. The firmware keeps no font and no framebuffer — everything visual is decided here.

## Requirements

- Python 3.10+
- Bluetooth LE adapter

## Installation

```bash
cd tools/epd-monitor
pip install -r requirements.txt
```

## Quick Start

```bash
# 1. Copy and fill in the config
cp config.example.toml config.toml
$EDITOR config.toml           # add your API keys

# 2. Check what data will be fetched (no BLE)
python epd_monitor.py status

# 3. See the frame before sending it (no BLE, no API keys)
python epd_monitor.py render --demo

# 4. Push to the device once
python epd_monitor.py push

# 5. Run as a daemon (loops at refresh_interval)
python epd_monitor.py daemon
```

## Commands

| Command | Description |
|---------|-------------|
| `push`     | Fetch all providers, compose the frame and stream it once |
| `render`   | Compose only: write `frame.bin` + `preview.png`, never touches BLE |
| `status`   | Print fetched data to stdout, no BLE |
| `daemon`   | Loop forever, push on `refresh_interval` |
| `scan`     | Scan for nearby BLE devices (to find your device address) |
| `describe` | Print the device's GATT services and characteristics |

`render --demo` needs no config file and no API keys, and `test_frame.py` checks the
packing and the protocol constants against the firmware sources:

```bash
python test_frame.py
```

## Protocol

Wire convention — the same one the working web host in `html/` uses:

- MSB is the leftmost pixel of a byte
- bit `1` = white paper, bit `0` = black ink
- one plane is `50 * 300 = 15000` bytes, streamed as 790 packets of 19 pixels
  (20-byte ATT value minus the command byte; S110/S130 are Bluetooth 4.1, so
  there is no MTU exchange to grow it)

| plane | driver 1 `EPD_DRIVER_4IN2` | driver 2 `EPD_DRIVER_4IN2_V2` | driver 3 `EPD_DRIVER_4IN2B_V2` |
|-------|--------------------------|-------------------------------|--------------------------------|
| 0     | `0x10`                   | `0x24`                        | `0x10` (black)                 |
| 1     | `0x13`                   | `0x26`                        | `0x13` (red)                   |

A black-and-white panel is driven with plane 0 only; a BWR panel gets both, and
only the final plane carries the refresh flag.

| Command | Byte | Direction | Payload |
|---------|------|-----------|---------|
| `EPD_CMD_STREAM_BEGIN` | `0xB0` | host → device | `[plane]` |
|                        |        | device → host (notify) | `[status, plane, plane_bytes_le16]` |
| `EPD_CMD_STREAM_DATA`  | `0xB1` | host → device | `[pixel x 1..19]`, no reply |
| `EPD_CMD_STREAM_END`   | `0xB2` | host → device | `[bytes_le16, sum_le32, flags]` |
|                        |        | device → host (notify) | `[status, received_le16, sum_le32]` |
| `EPD_CMD_STREAM_ABORT` | `0xB3` | host → device | — |
| `EPD_CMD_GET_STATUS`   | `0xB5` | host → device | — |
|                        |        | device → host (notify) | `[streaming, plane, received_le16, plane_bytes_le16, driver]` |

`flags`: `0x01` refresh, `0x02` put the panel to sleep. `status`: see
`EPD_STREAM_STATUS_*` in `EPD/EPD_ble.h`.

Flow control uses the notifications, not the GATT write response: the
SoftDevice answers writes on the application's behalf, so a write response
only proves the packet reached the link layer. The client therefore waits for
the `STREAM_BEGIN` and `STREAM_END` acks, which the device sends after the
panel has actually been initialised or refreshed. A plane whose byte count or
running sum does not verify is dropped without refreshing, and the next frame
starts from a fresh panel `Init()`.

## Supported Providers

| `type`     | Provider          | Data returned |
|------------|-------------------|---------------|
| `kimi`     | Kimi / Moonshot   | CNY balance |
| `deepseek` | DeepSeek          | CNY/USD balance |
| `zhipu`    | Zhipu AI (智谱)    | Token quota %, request count |
| `openai`   | OpenAI / ChatGPT  | USD monthly spend (requires Admin key) |
| `generic`  | Any REST API      | Configured via `balance_field` / `quota_*_field` |

### OpenAI Note

The OpenAI usage API requires an **Admin API key** (`sk-admin-…`) with the `api.usage.read` scope. Regular project keys (`sk-…`) will return a 403 error. Create an Admin key at [platform.openai.com/settings/organization/api-keys](https://platform.openai.com/settings/organization/api-keys).

### Adding a custom provider

Use `type = "generic"` with dotted-path field selectors:

```toml
[[providers]]
type              = "generic"
name              = "Claude"
url               = "https://api.anthropic.com/v1/credits"
auth_header       = "x-api-key"
auth_prefix       = ""
api_key           = "sk-ant-..."
balance_field     = "credits_remaining"
balance_scale     = 100
unit              = "USD"
```

## Frame Slots

The composed frame has **3 item slots**. If more than 3 provider entries are configured, only the first 3 items returned (in config order) are drawn.

## Power and reachability

The device advertises fast for 30 s after boot or disconnect, then falls back to
1.28 s slow advertising and stays connectable indefinitely, so a push can happen
at any time. It never enters System OFF on its own: an nRF51 in System OFF wakes
only on GPIO SENSE, NFC or reset — never on a timer — and this board runs the
synthetic LF clock, which stops while the chip idles. Send `EPD_CMD_SYS_SLEEP`
(`0x92`) if you want the extreme-low-power mode, and accept that waking it then
needs the wakeup pin or an NFC field.

## Automating with cron / launchd

```bash
# cron: every 30 minutes
*/30 * * * * /usr/bin/python3 /path/to/epd-monitor/epd_monitor.py push --config /path/to/config.toml

# macOS launchd: see config.example.toml comment for plist template
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Device not found | Run `python epd_monitor.py scan`; the firmware advertises `NRF_EPD_xxxx`. Set `device_address` to skip scanning |
| `no EPD characteristic found` | Run `python epd_monitor.py describe` and compare the service UUID against `EPD_SERVICE_UUID` in `protocol.py` |
| `no ack for command 0xb0` | The client could not subscribe to notifications, or the device reset mid-transfer. Reconnect |
| `STREAM_END: verify failed` | Packets were lost. Lower the connection interval, or stop using `fast_write` |
| `panel busy timeout` | The panel never released BUSY: check the wiring and that `busy_pin` is mapped correctly |
| Image is inverted or in the wrong colour | Plane order/polarity: see the table above and `test_frame.py` |
| `[Zhipu] HTTP 401` | Use the full API key string from `open.bigmodel.cn`, not just the prefix |
| `[OpenAI] Permission denied` | Use an Admin key with `api.usage.read` scope |
| `[Kimi] status: false` | Balance exhausted or API key invalid |
