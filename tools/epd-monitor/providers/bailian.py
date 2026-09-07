"""Aliyun Bailian Token Plan provider, via the Bailian CLI console gateway.

`bl` (bailian-cli) reaches the console's private APIs through a CLI gateway
that takes a bearer token instead of browser cookies:

    POST https://{host}/cli/api.json?action={action}&product=sfm_bailian&api={api}
    Content-Type: application/x-www-form-urlencoded
    Authorization: Bearer <access_token>       # from ~/.bailian/config.json
    params={"Api":..,"V":"1.0","Data":{...,"cornerstoneParam":{...}}}&region=<region>

Reverse-engineered from bailian-cli 1.20.0 (bailian-cli-core's console gateway
and the `bl usage token-plan` command) and confirmed against
`bl usage token-plan --verbose`.  The response envelope is exactly the one
providers/webquota.py scrapes out of the console page - data.DataV2.data.data -
so the off-screen Edge window (`aliyun-web`) can be replaced by two HTTP
POSTs: no Playwright, no anti-bot gamble, same numbers.

One-time setup - either route works, and they need nothing but a browser:

    python epd_monitor.py login --provider bailian    # native, no Node required
    bl auth login --console --console-site domestic   # or let the CLI store it

The token lands in ~/.bailian/config.json.  When it expires the gateway answers
`NotLogined` and the login must be repeated; the CLI can only refresh it on
its own when an OpenAPI AK/SK pair is stored as well (`bl auth login
--open-api`).

Nothing here needs the CLI installed.  `mode = "http"` (default) posts with
httpx and only needs a console access token, and `login --provider bailian`
runs the same local-callback handshake the CLI does - browser to the console
login page, token posted back to a 127.0.0.1 port - so the whole path works
without Node.  `mode = "cli"` is the fallback that shells out to
`bl console call` and leaves the credential to the CLI.  When `bl` is already
logged in, its token file is picked up as it is.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register

# region -> site -> (gateway host, gateway action); the CLI's own table.
_GATEWAYS: dict[str, dict[str, tuple[str, str]]] = {
    "cn-beijing": {
        "domestic": ("bailian-cs.console.aliyun.com", "BroadScopeAspnGateway"),
        "international": ("bailian-cs.console.alibabacloud.com",
                          "BroadScopeAspnGateway"),
    },
    "ap-southeast-1": {
        "domestic": ("modelstudio-cs.console.aliyun.com",
                     "IntlBroadScopeAspnGateway"),
        "international": ("bailian-singapore-cs.alibabacloud.com",
                          "IntlBroadScopeAspnGateway"),
    },
}

# The two calls the console page makes for the Token Plan card.  `bl console
# call --api <name>` reaches any gateway API, so another metric (free tier,
# model usage statistics) is one more constant away.
API_USAGE = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"
API_SUBSCRIPTION = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/subscription"

DEFAULT_CONFIG_PATH = Path.home() / ".bailian" / "config.json"

# Login page per console site, and where this tool keeps the token it receives.
# profiles/ is gitignored already (it holds the browser session cookies).
LOGIN_PAGES = {"domestic": "https://bailian.console.aliyun.com",
               "international": "https://modelstudio.console.alibabacloud.com"}
DEFAULT_STORE = (Path(__file__).resolve().parent.parent
                 / "profiles" / "bailian" / "console.json")
LOGIN_TIMEOUT_S = 900.0        # the CLI waits 15 minutes for the console callback

# What the console posts back, and the alias spellings it may use for each key.
_ALIASES: dict[str, tuple[str, ...]] = {
    "access_token": ("access_token", "accessToken"),
    "api_key": ("api_key", "apiKey"),
    "base_url": ("base_url", "baseUrl"),
    "console_site": ("console_site", "consoleSite"),
    "console_region": ("console_region", "consoleRegion"),
    "console_switch_agent": ("console_switch_agent", "consoleSwitchAgent"),
    "workspace_id": ("workspace_id", "workspaceId"),
}
_CRED_FIELDS = tuple(_ALIASES)


def gateway_for(region: str, site: str) -> tuple[str, str]:
    """(host, action) for a region/site pair; unknown values fall back to Beijing."""
    table = _GATEWAYS.get(region) or _GATEWAYS["cn-beijing"]
    return table.get(site) or table["domestic"]


def gateway_url(host: str, action: str, api: str) -> str:
    """The gateway URL.

    The api name is percent-encoded here as a query value (a slash becomes %2F,
    matching the CLI's encodeURIComponent) and sent again, unencoded, inside
    the `params` body field.
    """
    return (f"https://{host}/cli/api.json?action={action}"
            f"&product=sfm_bailian&api={urllib.parse.quote(api, safe='')}")


def build_params(api: str, data: dict[str, Any], switch_agent: int | None) -> str:
    """The `params` form field: the gateway envelope around the API payload."""
    cornerstone: dict[str, Any] = {
        "protocol": "V2",
        "console": "ONE_CONSOLE",
        "productCode": "p_efm",
        "switchUserType": 3,
        "consoleSite": "BAILIAN_ALIYUN",
    }
    if switch_agent is not None:
        cornerstone["switchAgent"] = switch_agent
    override = data.get("cornerstoneParam")
    if isinstance(override, dict):
        cornerstone.update(override)
    return json.dumps({"Api": api, "V": "1.0",
                       "Data": {**data, "cornerstoneParam": cornerstone}})


def build_form(api: str, data: dict[str, Any], region: str,
               switch_agent: int | None) -> dict[str, str]:
    """The urlencoded body: `params` plus the gateway region."""
    return {"params": build_params(api, data, switch_agent), "region": region}


def unwrap(body: dict[str, Any], *, api: str) -> dict[str, Any]:
    """Pull the metric object out of the envelope, or say why there is none.

    Layers, outermost first: the gateway's `data`, then `DataV2.data` (the
    API call), then that call's `data` (the payload the console renders).
    """
    outer = body.get("data")
    outer = outer if isinstance(outer, dict) else {}
    if outer.get("success") is False and outer.get("errorCode"):
        code = outer["errorCode"]
        text = code if isinstance(code, str) else json.dumps(code)
        if "NotLogined" in text:
            raise ProviderError(
                f"[{api}] console session expired - re-run "
                "'python epd_monitor.py login --provider bailian' (or "
                "'bl auth login --console --console-site domestic')")
        raise ProviderError(f"[{api}] gateway error {text}: "
                            f"{outer.get('errorMsg', '')}")
    datav2 = outer.get("DataV2")
    datav2 = datav2 if isinstance(datav2, dict) else {}
    call = datav2.get("data")
    call = call if isinstance(call, dict) else {}
    if call.get("success") is False:
        raise ProviderError(
            f"[{api}] {call.get('code', 'ERROR')}: {call.get('msg', '')}")
    payload = call.get("data")
    return payload if isinstance(payload, dict) else {}


def parse_token_plan(usage: dict[str, Any],
                     subscription: dict[str, Any]) -> list[SubscriptionItem]:
    """The card `aliyun-web` draws: weekly usage share as the bar, reset time
    and remaining plan days on the note line."""
    if "per1WeekPercentage" not in usage:
        raise ProviderError(
            "[Bailian] token-plan usage response has no per1WeekPercentage")
    used = float(usage["per1WeekPercentage"]) * 100

    note: list[str] = []
    reset = usage.get("per1WeekResetTime")
    if reset:
        note.append(f"rst {datetime.fromtimestamp(int(reset) / 1000):%m-%d %H:%M}")
    days = subscription.get("remainingDays")
    if days is not None:
        note.append(f"{days}d")

    # Only present while a 5-hour window applies to the plan.  The weekly share
    # stays the bar so the card reads like every other provider's.
    extra = ""
    five = usage.get("per5HourPercentage")
    if five is not None:
        extra = f"5h {float(five) * 100:.0f}%"

    return [SubscriptionItem(plan_name="Aliyun TokenPlan", quota_total=100,
                             quota_used=round(used), balance=0, unit="%",
                             note=" ".join(note), extra=extra)]


def store_path(cfg: dict[str, Any] | None = None) -> Path:
    """Where `login` writes the token and `fetch` reads it back."""
    raw = str((cfg or {}).get("store", "")).strip()
    return Path(raw).expanduser() if raw else DEFAULT_STORE


def cli_config_path(cfg: dict[str, Any] | None = None) -> Path:
    """The file `bl` writes; BAILIAN_CONFIG_DIR beats the home directory."""
    raw = str((cfg or {}).get("cli_config", "")).strip()
    if raw:
        return Path(raw).expanduser()
    env_dir = os.environ.get("BAILIAN_CONFIG_DIR", "").strip()
    return Path(env_dir) / "config.json" if env_dir else DEFAULT_CONFIG_PATH


def _read_json(path: Path) -> dict[str, Any]:
    """Best-effort read: a missing or broken credential file just means 'none'."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_credentials(data: dict[str, Any], path: Path) -> Path:
    """Persist a callback payload beside the browser profiles (gitignored)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {k: data[k] for k in _CRED_FIELDS if k in data}
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)       # best effort: Windows largely ignores modes
    except OSError:
        pass
    return path


def load_credentials(path: Path) -> dict[str, Any]:
    """The stored credentials, or {} when there are none."""
    return _read_json(path)


def resolve_credentials(cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Find a console token, in the order the provider looks.

    1. ``access_token`` in the provider config, or ``BAILIAN_ACCESS_TOKEN``;
    2. this tool's own store, written by ``login --provider bailian``;
    3. the file ``bl`` writes, so an existing CLI login works untouched.

    Returns the credentials and a label saying where they came from.
    """
    inline = (str(cfg.get("access_token", "")).strip()
              or os.environ.get("BAILIAN_ACCESS_TOKEN", "").strip())
    if inline:
        return {"access_token": inline}, "config"
    store = store_path(cfg)
    stored = _read_json(store)
    if str(stored.get("access_token", "")).strip():
        return stored, f"store:{store}"
    cli = cli_config_path(cfg)
    from_cli = _read_json(cli)
    if str(from_cli.get("access_token", "")).strip():
        return from_cli, f"bl:{cli}"
    raise ProviderError(
        "[Bailian] no console token found. Run "
        "'python epd_monitor.py login --provider bailian' (needs no CLI), or "
        "'bl auth login --console --console-site domestic', or set access_token "
        f"/ BAILIAN_ACCESS_TOKEN. Looked in {store} and {cli}")


# ----------------------------------------------------------------------
# Native console login: the CLI's local-callback handshake, in Python
# ----------------------------------------------------------------------
def login_page(site: str) -> str:
    """Console login page for a site; unknown sites fall back to domestic."""
    return LOGIN_PAGES.get(site) or LOGIN_PAGES["domestic"]


def new_state() -> str:
    """32 hex chars - the same shape as the CLI's randomBytes(16) state."""
    return secrets.token_hex(16)


def build_login_url(base: str, port: int, state: str, *,
                    need_api_key: bool = False) -> str:
    """The login URL, character for character what the CLI builds.

    The second question mark is not a typo: the console reads `notice` as one
    opaque "host:port?state=..." value, so it must not become "&state=".
    """
    url = (f"{base}/console-login?notice=127.0.0.1:{port}"
           f"?state={urllib.parse.quote(state, safe='')}")
    return url + "&needapikey=true" if need_api_key else url


def mask_token(token: str) -> str:
    """Enough to recognise a token, never enough to use it."""
    return f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "..."


def _flat(query: str, body: str, content_type: str) -> dict[str, str]:
    """Merge a callback's query string and body into one flat string map."""
    kind = content_type.lower()
    looks_json = body.lstrip().startswith("{")
    merged: dict[str, str] = {}
    parts = [query]
    if body and ("x-www-form-urlencoded" in kind or not looks_json):
        parts.append(body)
    for part in parts:
        if part:
            for key, value in urllib.parse.parse_qsl(part):
                if value.strip():
                    merged[key] = value.strip()
    if body and ("json" in kind or looks_json):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (str, int, float)) and str(value).strip():
                    merged[key] = str(value).strip()
    return merged


def parse_callback(query: str, body: str, content_type: str) -> dict[str, Any]:
    """Credentials from one console callback, normalised to snake_case.

    The console may use either spelling (access_token / accessToken) and either
    body encoding (JSON or form-urlencoded); the CLI accepts all of them.
    """
    flat = _flat(query, body, content_type)
    out: dict[str, Any] = {}
    for key, names in _ALIASES.items():
        for name in names:
            if name in flat:
                out[key] = flat[name]
                break
    if "console_switch_agent" in out:
        try:
            out["console_switch_agent"] = int(out["console_switch_agent"])
        except ValueError:
            del out["console_switch_agent"]
    return out


def _callback_handler(state: str, collected: dict[str, Any],
                      done: threading.Event) -> type[BaseHTTPRequestHandler]:
    """The 127.0.0.1 receiver: the same contract as the CLI's local server."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Quiet: this tool prints its own progress lines."""

        def _reply(self, code: int, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, PUT, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._accept(urllib.parse.urlparse(self.path).query, "", "")

        def do_POST(self) -> None:
            self._accept(*self._body())

        do_PUT = do_POST
        do_PATCH = do_POST

        def _body(self) -> tuple[str, str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            return (urllib.parse.urlparse(self.path).query, raw,
                    str(self.headers.get("Content-Type") or ""))

        def _accept(self, query: str, body: str, content_type: str) -> None:
            seen = urllib.parse.parse_qs(query).get("state", [""])[0]
            if seen != state:
                self._reply(400, "bad state\n")
                return
            collected.update(parse_callback(query, body, content_type))
            self._reply(200, "OK\n")
            if collected.get("access_token") or collected.get("api_key"):
                done.set()

    return Handler


def console_login(*, site: str = "domestic", store: Path | None = None,
                  timeout_s: float = LOGIN_TIMEOUT_S, open_browser: bool = True,
                  need_api_key: bool = False) -> dict[str, Any]:
    """Sign in through the user's own browser and keep the token.

    Mirrors the CLI: bind a random 127.0.0.1 port, open
    `<console>/console-login?notice=127.0.0.1:<port>?state=<hex>` and wait for
    the console to hand the access token back.  No OAuth client and no secret
    are involved - the browser's own session is the credential.
    """
    state = new_state()
    collected: dict[str, Any] = {}
    done = threading.Event()
    handler = _callback_handler(state, collected, done)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        raise ProviderError(
            f"[Bailian] cannot bind 127.0.0.1 for the login callback: {exc}") from exc
    port = int(server.server_address[1])
    url = build_login_url(login_page(site), port, state, need_api_key=need_api_key)
    worker = threading.Thread(target=server.serve_forever, daemon=True,
                              name="bailian-login")
    worker.start()
    print(f"Waiting for the Bailian console callback on 127.0.0.1:{port} "
          f"(timeout {int(timeout_s)}s)...")
    opened = False
    if open_browser:
        try:
            opened = webbrowser.open(url)
        except Exception:            # no browser is not fatal: the URL is printed
            opened = False
    if not opened:
        print("Open this URL in a browser that is signed in to Bailian:")
    print(url)
    try:
        if not done.wait(timeout_s):
            raise ProviderError(
                "[Bailian] login timed out: the console never called back")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    token = str(collected.get("access_token", "")).strip()
    if not token:
        raise ProviderError("[Bailian] the console callback carried no access_token")
    path = save_credentials(collected, store or DEFAULT_STORE)
    print(f"Saved console credentials to {path} (token {mask_token(token)}, "
          f"site {collected.get('console_site', '?')}, "
          f"region {collected.get('console_region', '?')})")
    return collected


def login_from_config(cfg: dict[str, Any], *, timeout_s: float = LOGIN_TIMEOUT_S,
                      open_browser: bool = True) -> dict[str, Any]:
    """`epd_monitor.py login --provider bailian`.

    Honours the provider's own site/store keys so the login writes exactly
    where `fetch` will read.
    """
    site = str(cfg.get("console_site", "")).strip() or "domestic"
    return console_login(site=site, store=store_path(cfg), timeout_s=timeout_s,
                         open_browser=open_browser)


@register
class BailianProvider(ProviderBase):
    provider_type = "bailian"

    async def fetch(self) -> list[SubscriptionItem]:
        mode = str(self._cfg.get("mode", "http")).strip().lower() or "http"
        if mode not in ("http", "cli"):
            raise ProviderError(
                f"[{self.name}] mode must be 'http' or 'cli', got {mode!r}")
        timeout = float(self._cfg.get("timeout", 20) or 20)
        if mode == "cli":
            usage, subscription = await self._fetch_cli(timeout)
        else:
            usage, subscription = await self._fetch_http(timeout)
        return parse_token_plan(usage, subscription)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _api(self, key: str, default: str) -> str:
        """API names are pinnable, so a gateway rename is a config edit."""
        return str(self._cfg.get(key, "")).strip() or default

    def _switch_agent(self, cred: dict[str, Any]) -> int | None:
        agent = self._cfg.get("console_switch_agent",
                              cred.get("console_switch_agent"))
        if agent is None or agent == "":
            return None
        try:
            return int(agent)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Transport 1: direct HTTP against the console gateway
    # ------------------------------------------------------------------
    async def _fetch_http(self,
                          timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        cred, _source = resolve_credentials(self._cfg)
        token = str(cred["access_token"]).strip()
        region = str(self._cfg.get("console_region") or cred.get("console_region")
                     or "cn-beijing")
        site = str(self._cfg.get("console_site") or cred.get("console_site")
                   or "domestic")
        switch_agent = self._switch_agent(cred)
        host, action = gateway_for(region, site)
        headers = {"Accept": "*/*",
                   "Content-Type": "application/x-www-form-urlencoded",
                   "Authorization": f"Bearer {token}"}

        api_usage = self._api("api_usage", API_USAGE)
        api_sub = self._api("api_subscription", API_SUBSCRIPTION)
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            body = await self._post(client, host, action, region, switch_agent,
                                    api_usage)
            usage = unwrap(body, api=api_usage)
            subscription = await self._post_optional(client, host, action, region,
                                                     switch_agent, api_sub)
        return usage, subscription

    async def _post(self, client: httpx.AsyncClient, host: str, action: str,
                    region: str, switch_agent: int | None,
                    api: str) -> dict[str, Any]:
        url = gateway_url(host, action, api)
        form = build_form(api, {}, region, switch_agent)
        try:
            resp = await client.post(url, data=form)
        except httpx.HTTPError as exc:
            raise ProviderError(f"[{self.name}] {api} request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"[{self.name}] HTTP {resp.status_code} from {host}: "
                                f"{resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(f"[{self.name}] {api}: non-JSON response: "
                                f"{resp.text[:200]}") from exc
        return body if isinstance(body, dict) else {}

    async def _post_optional(self, client: httpx.AsyncClient, host: str, action: str,
                             region: str, switch_agent: int | None,
                             api: str) -> dict[str, Any]:
        """The remaining-plan-days count is a nicety: never fail the card over it."""
        try:
            body = await self._post(client, host, action, region, switch_agent, api)
            return unwrap(body, api=api)
        except ProviderError:
            return {}

    # ------------------------------------------------------------------
    # Transport 2: let the CLI own the credential
    # ------------------------------------------------------------------
    async def _fetch_cli(self,
                         timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        wanted = str(self._cfg.get("bl_path", "")).strip() or "bl"
        bl = shutil.which(wanted)
        if not bl:
            raise ProviderError(
                f"[{self.name}] {wanted!r} not found on PATH - install bailian-cli "
                "(npm install -g bailian-cli) or set bl_path")
        profile = str(self._cfg.get("profile", "")).strip()
        region = str(self._cfg.get("console_region", "")).strip()
        site = str(self._cfg.get("console_site", "")).strip()
        usage = await self._bl_call(bl, profile, region, site, timeout,
                                    self._api("api_usage", API_USAGE), required=True)
        subscription = await self._bl_call(
            bl, profile, region, site, timeout,
            self._api("api_subscription", API_SUBSCRIPTION), required=False)
        return usage, subscription

    async def _bl_call(self, bl: str, profile: str, region: str, site: str,
                       timeout: float, api: str, *,
                       required: bool) -> dict[str, Any]:
        argv = [bl, "console", "call"]
        if profile:
            argv += ["--config", profile]
        argv += ["--api", api, "--data", "{}", "--output", "json"]
        if region:
            argv += ["--console-region", region]
        if site:
            argv += ["--console-site", site]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"})
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            if required:
                raise ProviderError(
                    f"[{self.name}] `bl console call` failed: {exc}") from exc
            return {}

        out = out_b.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = (err_b.decode("utf-8", "replace").strip() or out)[:300]
            if required:
                raise ProviderError(f"[{self.name}] `bl console call` exited "
                                    f"{proc.returncode}: {detail}")
            return {}
        try:
            body = json.loads(out)
        except ValueError as exc:
            if required:
                raise ProviderError(f"[{self.name}] `bl console call` returned no "
                                    f"JSON: {out[:200]}") from exc
            return {}
        if not isinstance(body, dict):
            return {}
        try:
            return unwrap(body, api=api)
        except ProviderError:
            if required:
                raise
            return {}
