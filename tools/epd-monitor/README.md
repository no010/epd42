# EPD Monitor — PC Companion

PC-side companion for the **EPD42 subscription monitor** firmware.

Periodically fetches usage / balance from AI providers (Kimi, DeepSeek, Zhipu, OpenAI and any custom provider) and pushes the data to an EPD42 e-ink display over BLE.

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

# 3. Push to the device once
python epd_monitor.py push

# 4. Run as a daemon (loops at refresh_interval)
python epd_monitor.py daemon
```

## Commands

| Command | Description |
|---------|-------------|
| `push`   | Fetch all providers and push to EPD42 once |
| `status` | Print fetched data to stdout, no BLE |
| `daemon` | Loop forever, push on `refresh_interval` |
| `scan`   | Scan for nearby BLE devices (to find your device address) |

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

## EPD Slot Limit

The EPD42 firmware supports **up to 3 items** on screen. If more than 3 provider entries are configured, only the first 3 items returned (in config order) are pushed.

## Automating with cron / launchd

```bash
# cron: every 30 minutes
*/30 * * * * /usr/bin/python3 /path/to/epd-monitor/epd_monitor.py push --config /path/to/config.toml

# macOS launchd: see config.example.toml comment for plist template
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Device not found | Run `python epd_monitor.py scan` and set `device_address` in config |
| `[Zhipu] HTTP 401` | Use the full API key string from `open.bigmodel.cn`, not just the prefix |
| `[OpenAI] Permission denied` | Use an Admin key with `api.usage.read` scope |
| `[Kimi] status: false` | Balance exhausted or API key invalid |
