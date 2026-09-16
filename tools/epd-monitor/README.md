# EPD Monitor — PC Companion

PC-side companion for the **EPD42 subscription monitor** firmware.

Periodically fetches usage / balance from AI providers (Kimi, DeepSeek, Zhipu, OpenAI and any custom provider), draws the result into a 400x300 monochrome frame, and streams the packed bits to an EPD42 e-ink panel over BLE. The firmware keeps no font and no framebuffer — everything visual is decided here.

## Requirements

- [uv](https://docs.astral.sh/uv/) — it provisions Python 3.10+ itself
- Bluetooth LE adapter

## Installation

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. There is
no `requirements.txt`.

```bash
cd tools/epd-monitor
uv sync
```

## Quick Start

```bash
# 1. Copy and fill in the config (config.toml is gitignored: it holds API keys)
cp config.example.toml config.toml
$EDITOR config.toml       # add your API keys; set device_address to skip the 15 s scan

# 2. Check what data will be fetched (no BLE)
uv run python epd_monitor.py status

# 3. See the frame before sending it (no BLE, no API keys)
uv run python epd_monitor.py render --demo

# 4. Push to the device once
uv run python epd_monitor.py push

# 5. Run as a daemon (loops at refresh_interval)
uv run python epd_monitor.py daemon
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
| `pattern`  | Stream a synthetic test image (`white`, `black`, `corner-dots`, `row-marker`, `left-half`, `grid`) |
| `setdriver`| Point the device at the panel actually attached (`--driver 1|2|3`) |
| `setmode`  | Choose the power mode (`--mode resident` or `--mode deep [--sleep-after SECONDS]`) |
| `fault`    | Send half a plane and END early: the device must refuse it, then recover |
| `login`    | Sign in a provider: `deepseek-web`/`kimi-web`/`aliyun-web` keep a browser profile, `bailian` only needs one loopback callback |

Bring-up order on fresh hardware: `scan` → `describe` → `setdriver` →
`pattern --name white` → `pattern --name corner-dots` →
`pattern --name row-marker` → `push --demo`. A composed UI cannot tell you
*which* of polarity, bit order, row order or plane addressing is wrong - it just
looks like a wrong picture - so each pattern answers one of those questions (see
`render.pattern`). `setdriver` matters because the panel physically attached and
the driver id stored in the device's config page can disagree, and every symptom
of that looks like a polarity bug.

`render --demo` needs no config file and no API keys, and `test_frame.py` checks the
packing and the protocol constants against the firmware sources:

```bash
uv run python test_frame.py
```

## Protocol

Wire convention — the same one the working web host in `html/` uses:

- MSB is the leftmost pixel of a byte
- bit `1` = white paper, bit `0` = black ink
- one plane is `50 * 300 = 15000` raw bytes
- the plane is **run-length encoded (TIFF PackBits, no end-of-line marker)**
  before packetising. This UI is ~90% paper white, so 15000 bytes encode to
  ~1650 — **87 writes instead of 790**. Incompressible data expands by at most
  one byte per 128.
- each write carries the command byte plus up to 19 payload bytes (the 20-byte
  ATT ceiling: S110/S130 are Bluetooth 4.1, with no MTU exchange to grow it)

| driver | image goes to | companion SRAM | planes the host sends |
|---|---|---|---|
| 1 `EPD_DRIVER_4IN2` (UC8176) | `0x13` (NEW) | `0x10` (OLD), filled by the firmware with `0x00` | 1 |
| 2 `EPD_DRIVER_4IN2_V2` | `0x24` | `0x26` untouched — unverified, see below | 1 |
| 3 `EPD_DRIVER_4IN2B_V2` (UC8276C) | `0x10` (B/W), then `0x13` (red) | both come from the host | 2 |

"Plane 0" in the protocol means *the image*, not a specific RAM command: each
driver decides where it goes. On the UC8176 a refresh is the **OLD → NEW**
transition, so both SRAMs must hold data - an unwritten SRAM keeps its power-on
contents and the panel then drives every pixel randomly, which on hardware looks
like a wall of noise. The firmware fills OLD itself, so the host still sends one
plane. Only the final plane carries the refresh flag.

Driver 2 streams a single plane because that is the only path observed working
in `html/js`; the SSD1683 datasheet is not in this repo, so its `0x26` semantics
are unverified rather than known-good. If a V2 panel shows noise, that plane
needs filling the same way.

| Command | Byte | Direction | Payload |
|---------|------|-----------|---------|
| `EPD_CMD_STREAM_BEGIN` | `0xB0` | host → device | `[plane]` |
|                        |        | device → host (notify) | `[status, plane, plane_bytes_le16]` |
| `EPD_CMD_STREAM_DATA`  | `0xB1` | host → device | `[encoded x 1..19]`, no reply |
| `EPD_CMD_STREAM_END`   | `0xB2` | host → device | `[bytes_le16, sum_le32, flags]` |
|                        |        | device → host (notify) | `[status, received_le16, sum_le32]` |
| `EPD_CMD_STREAM_ABORT` | `0xB3` | host → device | — |
| `EPD_CMD_GET_STATUS`   | `0xB5` | host → device | — |
|                        |        | device → host (notify) | `[streaming, plane, received_le16, plane_bytes_le16, driver, power, grace_s]` |

`flags`: `0x01` refresh, `0x02` put the panel to sleep. **The hosts in this repo
send only `0x01`** — putting a UC8176 into its deep-sleep command (0x07 0xA5)
between frames made later refreshes no-ops on hardware, so the panel rests in
hardware reset instead (the firmware holds RST low on disconnect, and the next
connection's rising edge wakes it). `0x02` remains available for explicit use.
`power`: `0x00` resident, `0x01` deep sleep (see [Power and reachability](#power-and-reachability)).
`grace_s`: the deep-sleep disconnect grace in seconds (firmware that predates
the power-mode feature simply sends a shorter status reply). `status`: see
`EPD_STREAM_STATUS_*` in `EPD/EPD_ble.h`. The byte count and sum in
`STREAM_END` describe the **decoded** plane, so encoding is invisible to the
check.

Flow control uses the notifications, not the GATT write response: the
SoftDevice answers writes on the application's behalf, so a write response
only proves the packet reached the link layer. The client therefore waits for
the `STREAM_BEGIN` and `STREAM_END` acks, which the device sends after the
panel has actually been initialised or refreshed. A plane whose decoded byte
count or running sum does not verify is dropped without refreshing, and the
next frame starts from a fresh panel `Init()`.

**No per-packet checksum, on purpose.** Every BLE data-channel PDU already
carries a 24-bit CRC, and with write-with-response the link layer retransmits a
bad one before the ATT response is sent — so a corrupted pixel reaching the
panel is not a failure mode a byte-level check could catch. What it *could*
catch, a dropped write in `fast_write` mode or a device reset mid-frame, is
detectable but not repairable: PackBits is positional, so the device cannot
resume at packet *k* without replaying from the start of the plane. Restarting
the plane is exactly what `STREAM_END` already triggers, and costs 87 writes —
about 0.7 s at a 7.5 ms interval, 1.3 s at 15 ms, 2.6 s at 30 ms. Per-packet
checks would add ~5% to that transfer and still end in the same restart. They
would earn their keep only once writes become seekable, i.e. row-window
updates, where a single row can be resent on its own.

## Supported Providers

| `type`     | Provider          | Data returned |
|------------|-------------------|---------------|
| `kimi`     | Kimi / Moonshot   | CNY balance |
| `deepseek` | DeepSeek          | CNY/USD balance |
| `zhipu`    | Zhipu AI (智谱)    | Token quota %, request count |
| `openai`   | OpenAI / ChatGPT  | USD monthly spend (requires Admin key) |
| `generic`  | Any REST API      | Configured via `balance_field` / `quota_*_field` |
| `bailian`  | Aliyun Bailian Token Plan | Weekly usage %, reset time, plan days left |
| `aliyun`   | Aliyun resource packages (BSS OpenAPI) | Package total/remaining tokens |
| `deepseek-web`, `kimi-web`, `aliyun-web` | Web-session scraping (Playwright + local Edge) | The console's own numbers, no API keys |

### Token-mode pilot (`auth = "token"`; kimi-web + deepseek-web)

Kimi and DeepSeek have no official loopback token channel like Bailian, but each
SPA keeps the real credential as an in-memory `Authorization: Bearer` on its own
API (membership / usage).  One browser session captures it into
`profiles/<type>/token.json`; with `auth = "token"` every fetch after that is
plain httpx - no Playwright, no profile lock, sub-second polls.

```toml
[[providers]]
type = "kimi-web"
name = "Kimi"
auth = "token"      # remove this line to fall back to the headless browser path

[[providers]]
type = "deepseek-web"
name = "DeepSeek"
auth = "token"
```

Two ways to supply the credential:

- **automatic**: `uv run python epd_monitor.py login --provider kimi-web` (or
  `deepseek-web`) - a headed window opens, you sign in, the token is saved and
  the window closes;
- **manual**: already signed in?  Copy the `Authorization` value from DevTools
  (Network tab → the membership/usage request → Request Headers) and paste it:

  ```bash
  uv run python epd_monitor.py login --provider deepseek-web --token "Bearer sk-..."
  ```

  No browser is launched at all; a bare token without the `Bearer ` prefix is
  accepted too.  For Kimi also copy `refresh_token` (same Local Storage panel)
  and pass `--refresh-token` - then even the 15-minute access token rotates
  itself over httpx.

Healing, newest first: on a 401 (or an empty store) `auto_refresh` (default
on) tries, in order,

1. **Kimi: the official refresh endpoint over httpx.**  Probing the live session
   (2026-09-07) showed the SPA's lifecycle: `access_token` is a 15-minute HS512
   JWT and `refresh_token` a 90-day one, rotated on every refresh.  The SPA
   keeps both in localStorage; a captured `refresh_token` lets the provider call
   `POST auth.kimi.com/.../AuthService/RefreshToken {"refreshToken":..}` and
   rotate the pair itself - **no browser at all** for Kimi once a refresh_token
   is stored (saved automatically by `login`, or pasted via
   `--refresh-token` from DevTools → Application → Local Storage).
2. Headless re-capture from the existing browser profile - the fallback for
   DeepSeek (whose token lifetime is still being measured) and for a Kimi
   session without a refresh_token.

`auto_refresh = false` fails hard instead; `refresh_timeout` (seconds) bounds
each re-capture.  Only when every path fails does the card report the login
command instead of stale numbers.  Because the Kimi refresh token rotates on
every use, keep exactly one refresher active (the provider, or the login page in
your own browser - the two stay in sync when a captured pair is written back to
localStorage).

### Bailian Token Plan without a browser

`type = "bailian"` reads the Token Plan card through the same console gateway the
`bl` CLI (bailian-cli) uses, so it needs no Playwright and no off-screen Edge window:

```
POST https://bailian-cs.console.aliyun.com/cli/api.json?action=BroadScopeAspnGateway&product=sfm_bailian&api=<api>
Content-Type: application/x-www-form-urlencoded
Authorization: Bearer <console access_token - see "Signing in" below>

params={"Api":"<api>","V":"1.0","Data":{"cornerstoneParam":{...}}}&region=cn-beijing
```

Two calls give the card: `.../tokenplan/personal/api/v2/usage` (weekly and 5-hour
usage share plus reset time) and `.../tokenplan/personal/api/v2/subscription`
(`remainingDays`). Both answer inside the envelope `data.DataV2.data.data` — the very
payload `aliyun-web` scrapes out of the page — so the rendered card is unchanged.

#### Signing in (no CLI, no browser profile)

```bash
uv run python epd_monitor.py login --provider bailian   # native; needs no Node/bl
uv run python epd_monitor.py login --provider bailian --no-browser   # just print the URL
```

That runs the same handshake `bl auth login --console` does, in-process: bind a random
`127.0.0.1` port, open `<console>/console-login?notice=127.0.0.1:<port>?state=<32-hex>`
in your own browser, and let the console post `access_token` (plus `console_site`,
`console_region`, `console_switch_agent`, `workspace_id`) back to that port. There is no
OAuth client and no client secret anywhere — your browser session *is* the credential,
and the `state` check plus the loopback-only bind are what keep the callback honest.
It lands in `profiles/bailian/console.json` (gitignored, mode 0600 best-effort).

If you already use the CLI, `bl auth login --console --console-site domestic` works too.
Token lookup order: `access_token` in the provider config (or `BAILIAN_ACCESS_TOKEN`) →
`profiles/bailian/console.json` → the file `bl` writes (`~/.bailian/config.json`, or
`$BAILIAN_CONFIG_DIR`), so an existing CLI login is picked up untouched.

When the token expires the gateway answers `NotLogined` and the provider says so —
repeat the login. (`bl` can only self-refresh when an OpenAPI AK/SK pair is stored as
well; this provider never needs that.) `mode = "cli"` shells out to `bl console call`
instead, which avoids reading any token file at the cost of a Node startup. Any other
gateway API works the same way — `bl console call --api <name> --data '{}'` is the
reference, and `bl <cmd> --verbose` prints the exact request to copy.

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
at any time. Two persisted power modes (set with `epd_monitor.py setmode`, saved
in the device's config page) decide what happens after a frame:

- **resident** (default): the MCU stays in System ON and keeps advertising —
  the mode for pomodoro timers and quota monitors that push on a schedule.
- **deep sleep**: a verified push only *arms* the countdown. Once the link has
  been **disconnected and idle for the grace period** (`--sleep-after`, 0–255 s,
  default 60), the panel sleeps (the e-ink image persists) and the MCU enters
  System OFF. Reconnecting inside the window cancels the countdown, so several
  frames can ride one connection; waking afterwards takes the reset pin or the
  configured `wakeup` pin (GPIO SENSE) — wire an NFC field detector, a
  wireless-charge power-good line or a reed switch to it. The wake is a cold
  boot, so the next frame re-runs the panel `Init()` regardless — which also
  sidesteps the panel's deep-sleep-command quirk described under `flags` above.

An nRF51 in System OFF never wakes on a timer (this board runs the synthetic
LF clock, which stops while the chip idles) — that is exactly why the
countdown lives in the still-running System ON state and only the final
power-off is deferred to it. `EPD_CMD_SYS_SLEEP` (`0x92`) still forces System
OFF immediately if you want it by hand.

## Automating with cron / launchd

```bash
# cron: every 30 minutes
*/30 * * * * /usr/bin/python3 /path/to/epd-monitor/epd_monitor.py push --config /path/to/config.toml

# macOS launchd: see config.example.toml comment for plist template
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Device not found | Run `uv run python epd_monitor.py scan`; the firmware advertises `NRF_EPD_xxxx`. Set `device_address` to skip scanning |
| `no EPD characteristic found` | Run `uv run python epd_monitor.py describe` and compare the service UUID against `EPD_SERVICE_UUID` in `protocol.py` |
| `no ack for command 0xb0` | The client could not subscribe to notifications, or the device reset mid-transfer. Reconnect |
| `STREAM_END: verify failed` | Packets were lost. Lower the connection interval, or stop using `fast_write` |
| `panel busy timeout` | The panel never released BUSY: check the wiring and that `busy_pin` is mapped correctly |
| Image is inverted or in the wrong colour | Plane order/polarity: see the table above and `test_frame.py` |
| Device vanishes (not advertising) shortly after a push | Deep-sleep mode is active: it powered off once the link was idle past the grace period. Wake with the reset pin or the `wakeup` pin, and switch back with `setmode --mode resident` |
| `[Zhipu] HTTP 401` | Use the full API key string from `open.bigmodel.cn`, not just the prefix |
| `[OpenAI] Permission denied` | Use an Admin key with `api.usage.read` scope |
| `[Kimi] status: false` | Balance exhausted or API key invalid |
