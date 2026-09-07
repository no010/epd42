"""Subscription providers that read the logged-in web console via Playwright.

The vendor APIs for these subscriptions either do not exist (Bailian Token
Plan, Kimi membership) or report far less than the console shows (DeepSeek's
API key endpoint has no token usage figure).  The console pages themselves call
internal endpoints whose responses carry the real numbers, so this module
drives a persistent, logged-in browser profile to the page, captures those
responses, and parses them.

The recipes were written from responses captured on 2026-09-01 (see
test_providers.py, which replays those payloads offline).  The auth token for
these pages lives in the page's JS realm - replaying cookies from a plain HTTP
client gets a 401 on Kimi - so the browser is the integration point, not a
shortcut.

Profiles live in ``profiles/<provider>/`` next to the tool; a profile starts
empty, so run ``epd_monitor.py login --provider <type>`` once per provider to
sign in headed.  DeepSeek and Kimi then fetch headless from that profile.  The
Bailian console keeps its login in session cookies, which Chromium does not
persist across browser restarts, so its window parks off-screen and stays
resident instead (configure it with headless = false).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from playwright.sync_api import sync_playwright

from providers import ProviderBase, ProviderError, SubscriptionItem, register

PROFILE_ROOT = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_CHANNEL = "msedge"          # drive the locally installed Edge: no download
DEFAULT_WAIT_S = 45.0


@dataclass(frozen=True)
class Recipe:
    """What to open, which responses to capture, and how to read them."""

    start_url: str
    expect: tuple[str, ...]                       # URL fragments to capture
    parse: Callable[..., list[SubscriptionItem]]
    login_hint: str
    replay: Callable[[], tuple[str, ...]] = ()    # URLs to re-fetch in the page
                                                  # context with custom params


def _cents(amount: str | float) -> int:
    return int(round(float(amount) * 100))


def _local(iso: str) -> datetime:
    """Server timestamps are UTC; the device sits next to the user, so reset
    and expiry times read in the machine's own timezone (matches the console)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def _md(iso: str) -> str:
    return _local(iso).strftime("%m-%d")


def _last_with(bodies: list[dict[str, Any]], *path: str) -> dict[str, Any]:
    """Walk the newest-to-oldest bodies and return the first whose nested path
    exists - a cold start can serve an empty pre-auth shape first."""
    for body in reversed(bodies):
        node = body
        for key in path:
            if not isinstance(node, dict) or key not in node:
                break
            node = node[key]
        else:
            return node
    return {}


def _tokens_fmt(value: float) -> str:
    if value >= 1e9:
        return f"{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}K"
    return f"{value:,.0f}"


def _deepseek_replay_urls(now: datetime) -> tuple[str, ...]:
    """The usage tab charts a window ending at local midnight, so today's
    buckets never appear in its own requests.  These copies - same origin,
    same cookies - ask for today only."""
    base = "https://platform.deepseek.com/api/v0/usage/by_api_key"
    # The API rejects anything but day-aligned boundaries (tz=28800, UTC+8),
    # matching how the page itself frames its chart window.
    tz_off = 28800
    day = (int(now.timestamp()) + tz_off) // 86400 * 86400 - tz_off
    return (f"{base}/amount?start={day}&end={day + 86400}&tz={tz_off}",
            f"{base}/cost?start={day}&end={day + 86400}&tz={tz_off}")


DEEPSEEK_SUMMARY_FRAGMENT = "users/get_user_summary"
DEEPSEEK_COST_FRAGMENT = "usage/by_api_key/cost"
DEEPSEEK_AMOUNT_FRAGMENT = "usage/by_api_key/amount"
KIMI_SUBSCRIPTION_FRAGMENT = "MembershipService/GetSubscription"
KIMI_STATS_FRAGMENT = "MembershipService/GetSubscriptionStats"
ALIYUN_USAGE_FRAGMENT = "tokenplan/personal/api/v2/usage"
ALIYUN_SUBSCRIPTION_FRAGMENT = "tokenplan/personal/api/v2/subscription"


def parse_deepseek(responses: dict[str, list[dict[str, Any]]],
                   now: datetime | None = None) -> list[SubscriptionItem]:
    """Balance and lifetime spend from the page summary; 30-day cost and
    today's token usage by model (with the flash/pro split) from the
    usage-chart endpoints the tab loads.  ``now`` fixes 'today' for the daily
    buckets in tests."""
    now = now or datetime.now().astimezone()
    summary = _last_with(responses.get(DEEPSEEK_SUMMARY_FRAGMENT) or [],
                         "data", "biz_data")
    wallets = summary.get("normal_wallets") or []
    if not wallets:
        raise ProviderError("[DeepSeek] usage page returned no wallet data")
    balance = _cents(wallets[0].get("balance", 0))

    note_parts: list[str] = []
    costs = summary.get("total_costs") or []
    # Same currency symbol on both numbers: the line reads "balance / lifetime".
    extra = f"/ ¥{float(costs[0].get('amount', 0)):,.0f}" if costs else ""

    cost_body = _last_with(responses.get(DEEPSEEK_COST_FRAGMENT) or [],
                           "data", "biz_data", "data")
    today_start = int(now.replace(hour=0, minute=0, second=0,
                                  microsecond=0).timestamp())
    today_cost = 0.0
    for group in cost_body or []:
        for series in group.get("series") or []:
            for bucket in series.get("buckets") or []:
                if bucket.get("time", 0) >= today_start:
                    today_cost += float(bucket.get("cost", 0))
    if cost_body:
        note_parts.append(f"tdy ¥{today_cost:,.2f}")

    amount_biz = _last_with(responses.get(DEEPSEEK_AMOUNT_FRAGMENT) or [],
                            "data", "biz_data")
    per_model: dict[str, float] = {}
    for series in amount_biz.get("series") or []:
        model = series.get("model", "?")
        for bucket in series.get("buckets") or []:
            usage = bucket.get("usage") or {}
            tokens = sum(float(v) for k, v in usage.items() if "TOKEN" in k)
            if bucket.get("time", 0) >= today_start:
                per_model[model] = per_model.get(model, 0) + tokens
    total_tokens = sum(per_model.values())
    if amount_biz:
        flash = sum(v for k, v in per_model.items() if "flash" in k.lower())
        pro = sum(v for k, v in per_model.items() if "pro" in k.lower())
        note_parts.append(f"tok {_tokens_fmt(total_tokens)}")
        if total_tokens:
            note_parts.append(f"F{flash / total_tokens * 100:.0f}%/P{pro / total_tokens * 100:.0f}%")

    return [SubscriptionItem(plan_name="DeepSeek", quota_total=0, quota_used=0,
                             balance=balance, unit="CNY",
                             note=" ".join(note_parts), extra=extra)]


def parse_kimi(responses: dict[str, list[dict[str, Any]]]) -> list[SubscriptionItem]:
    """The quota tab's own view: 5-hour and 7-day window usage shares from
    GetSubscriptionStats, the plan title and monthly credits from
    GetSubscription.  Every share on the card reads as usage (the convention
    the Aliyun card set); the bar is the 5-hour usage (the window that
    actually blocks mid-day), with its reset time parked at the bar's right."""
    bodies = responses.get(KIMI_SUBSCRIPTION_FRAGMENT) or []
    stats_bodies = responses.get(KIMI_STATS_FRAGMENT) or []

    title, credits = "", None
    for body in reversed(bodies):
        if not title and (body.get("subscription") or {}).get("goods"):
            title = body["subscription"]["goods"].get("title", "")
        if credits is None:
            balance = body.get("subscriptionBalance") or {}
            if balance.get("amountUsedRatio") is not None:
                credits = balance
            else:
                for b in body.get("balances") or []:
                    if "amountUsedRatio" in b:
                        credits = b
                        break

    stats = {}
    for body in reversed(stats_bodies):
        r5 = body.get("ratelimitCode5h") or {}
        # The 5h window omits ``ratio`` while it is fresh (nothing used yet);
        # the quota tab shows that state as 0% used.
        if "enabled" in r5:
            stats = body
            break
    if not stats or credits is None:
        raise ProviderError("[Kimi] quota page returned no window usage data")

    five_used = round(float(stats["ratelimitCode5h"].get("ratio", 0)) * 100)
    note = [f"Mo {round(float(credits['amountUsedRatio']) * 100)}%",
            f"Wk {round(float(stats['ratelimitCode7d']['ratio']) * 100)}%",
            f"7d rst {_md(stats['ratelimitCode7d']['resetTime'])}",
            f"exp {_md(credits['expireTime'])}"]

    # The 5h usage and its reset time share the metrics line; the bar below
    # runs full width.
    extra = ""
    reset5h = stats["ratelimitCode5h"].get("resetTime")
    if reset5h:
        extra = f"rst {_local(reset5h):%H:%M}"

    return [SubscriptionItem(plan_name=f"Kimi {title}".strip(), quota_total=100,
                             quota_used=five_used, balance=0, unit="%",
                             note=" ".join(note), extra=extra)]


def parse_aliyun(responses: dict[str, list[dict[str, Any]]]) -> list[SubscriptionItem]:
    def last_with(fragment: str, key: str) -> dict[str, Any]:
        for body in reversed(responses.get(fragment) or []):
            inner = (((body.get("data") or {}).get("DataV2") or {}).get("data") or {}).get("data") or {}
            if key in inner:
                return inner
        return {}

    usage = last_with(ALIYUN_USAGE_FRAGMENT, "per1WeekPercentage")
    sub = last_with(ALIYUN_SUBSCRIPTION_FRAGMENT, "remainingDays")
    if not usage:
        raise ProviderError("[Aliyun] token-plan page returned no usage data")

    # Every share reads as usage across all cards, so the bar and the metrics
    # line agree; reset and remaining days carry the planning info.
    used = float(usage["per1WeekPercentage"]) * 100
    note = []
    reset = usage.get("per1WeekResetTime")
    if reset:
        note.append(f"rst {datetime.fromtimestamp(reset / 1000):%m-%d %H:%M}")
    days = sub.get("remainingDays")
    if days is not None:
        note.append(f"{days}d")
    return [SubscriptionItem(plan_name="Aliyun TokenPlan", quota_total=100,
                             quota_used=round(used), balance=0, unit="%",
                             note=" ".join(note))]


RECIPES: dict[str, Recipe] = {
    "deepseek-web": Recipe(
        start_url="https://platform.deepseek.com/usage",
        expect=(DEEPSEEK_SUMMARY_FRAGMENT, DEEPSEEK_COST_FRAGMENT,
                DEEPSEEK_AMOUNT_FRAGMENT),
        parse=parse_deepseek,
        replay=lambda _h: _deepseek_replay_urls(datetime.now().astimezone()),
        login_hint="登录 DeepSeek 开放平台（手机验证码或微信扫码）",
    ),
    "kimi-web": Recipe(
        start_url="https://www.kimi.com/membership/subscription?tab=quota",
        expect=(KIMI_SUBSCRIPTION_FRAGMENT, KIMI_STATS_FRAGMENT),
        parse=parse_kimi,
        replay=lambda _h: ((
            "https://www.kimi.com/apiv2/kimi.gateway.membership.v2"
            ".MembershipService/GetSubscriptionStats", "POST", "{}"),),
        login_hint="登录 www.kimi.com（扫码后进入会员页）",
    ),
    "aliyun-web": Recipe(
        start_url="https://bailian.console.aliyun.com/cn-beijing?tab=plan"
                  "#/efm/subscription/token-plan/personal",
        expect=(ALIYUN_SUBSCRIPTION_FRAGMENT, ALIYUN_USAGE_FRAGMENT),
        parse=parse_aliyun,
        login_hint="登录阿里云百炼控制台",
    ),
}


def _profile_dir(provider_type: str) -> Path:
    return PROFILE_ROOT / provider_type


# Consoles that keep their login in session cookies (Chromium does not persist
# those across browser restarts) get a standalone Edge of their own: launched
# once via subprocess with a CDP port, parked off-screen, and reused by every
# later run through connect_over_cdp.  The monitor process may exit; the
# browser keeps the session alive.
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CDP_BASE_PORT = 9330
_OFFSCREEN = ("--window-position=-32000,-32000",
              "--disable-backgrounding-occluded-windows",
              "--disable-renderer-backgrounding")


def _cdp_port(name: str) -> int:
    return CDP_BASE_PORT + sorted(RECIPES).index(name)


def _edge_executable() -> str:
    from shutil import which

    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            return path
    found = which("msedge")
    if found:
        return found
    raise ProviderError("Microsoft Edge not found; install it or point "
                        "EDGE_CANDIDATES at your browser")


def _port_open(port: int) -> bool:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
        return True


def _ensure_cdp_browser(name: str, recipe: Recipe, profile: Path,
                        on_screen: bool) -> int:
    """Start the provider's standalone Edge if it is not already running."""
    port = _cdp_port(name)
    if _port_open(port):
        return port
    args = [_edge_executable(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,900",
            *(_OFFSCREEN if not on_screen else ()),
            recipe.start_url]
    subprocess.Popen(args)
    for _ in range(60):                     # up to 12 s for the CDP endpoint
        if _port_open(port):
            return port
        time.sleep(0.2)
    raise ProviderError(f"[{name}] the standalone browser did not open its "
                        f"debug port {port}")


def _connect(name: str):
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_cdp_port(name)}")
    context = browser.contexts[0]
    return pw, browser, context


# All Playwright operations run on one dedicated thread: sync Playwright
# objects are bound to the thread that created them.
_PW_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="epd-playwright")


async def _pw_run(fn, *args) -> list[SubscriptionItem]:
    return await asyncio.wrap_future(_PW_POOL.submit(fn, *args))


def _run_replays(page, recipe: Recipe, captured: dict[str, Any],
                 auth_headers: dict[str, str]) -> None:
    """Re-fetch selected endpoints from the page's own context - its cookies
    plus the in-memory Authorization header the SPA attaches - and file the
    bodies as extra captures.

    A replay entry is a URL, or a ``(url, method, body)`` tuple for the
    POST-style gateways.  Needed because some of these calls fire once per
    page mount and are then served from the SPA's cache, so a reload alone
    does not re-trigger them."""
    import json as _json

    if not recipe.replay:
        return
    # The SPA attaches its in-memory Bearer token on the requests it fires
    # once its bundle boots; replaying before that yields 401s, so wait for
    # the token to show up (bounded - if it never does, the replays are
    # harmless and the natural capture decides).
    deadline = time.monotonic() + 8
    while "authorization" not in auth_headers and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    auth = auth_headers.get("authorization", "")
    for entry in recipe.replay(auth_headers):
        url, method, body = (entry, "GET", None) if isinstance(entry, str) else entry
        try:
            text = page.evaluate(
                "async ([u, m, b, a]) => await (await fetch(u, {method: m,"
                " credentials: 'include',"
                " headers: {...(a ? {authorization: a} : {}),"
                "          ...(b ? {'content-type': 'application/json'} : {})},"
                " body: b})).text()",
                [url, method, body, auth])
            parsed = _json.loads(text)
            for fragment in recipe.expect:
                if fragment in url:
                    captured.setdefault(fragment, []).append(parsed)
        except Exception:
            pass


def _headless_fetch(recipe: Recipe, profile: Path, wait_s: float) -> list[SubscriptionItem]:
    """Launch headless and poll until the parser accepts the captured bodies.

    Counting captured fragments is not enough: on a cold start the SPA can hit
    the endpoint before its session refresh and produce an empty
    unauthenticated shape, so the parser - not the fragment count - decides
    when the data is real, and one reload is thrown in midway.
    """
    captured: dict[str, Any] = {}
    auth_headers: dict[str, str] = {}
    last_error: ProviderError | None = None

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:       # non-JSON or empty body: keep waiting
                    pass

    def on_request(req) -> None:
        if "authorization" in req.headers:
            auth_headers.setdefault("authorization", req.headers["authorization"])

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), channel=DEFAULT_CHANNEL, headless=True,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("response", on_response)
        page.on("request", on_request)
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        _run_replays(page, recipe, captured, auth_headers)

        remaining, reloaded = wait_s, False
        while remaining > 0:
            page.wait_for_timeout(500)
            remaining -= 0.5
            try:
                return recipe.parse(captured)
            except ProviderError as exc:
                last_error = exc
            if remaining < wait_s / 2 and not reloaded:
                page.reload(wait_until="domcontentloaded")
                reloaded = True

        context.close()
    raise last_error or ProviderError("the page produced no parseable responses")


def _capture_and_parse(recipe: Recipe, profile: Path, wait_s: float) -> list[SubscriptionItem]:
    """Two attempts: the first run of a session can still race its own login."""
    last_error: ProviderError | None = None
    for _attempt in range(2):
        try:
            return _headless_fetch(recipe, profile, wait_s)
        except ProviderError as exc:
            last_error = exc
    raise last_error


def _park_offscreen(context, page) -> None:
    """Move the real window far off-screen once the user is signed in."""
    try:
        cdp = context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id,
                  "bounds": {"left": -32000, "top": -32000, "windowState": "normal"}})
    except Exception:
        pass


def _cdp_fetch(recipe: Recipe, name: str, wait_s: float) -> list[SubscriptionItem]:
    """One fetch through the resident standalone browser."""
    _ensure_cdp_browser(name, recipe, _profile_dir(name), on_screen=False)
    pw, _browser, context = _connect(name)
    captured: dict[str, list[dict[str, Any]]] = {}
    auth_headers: dict[str, str] = {}
    last_error: ProviderError | None = None

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    def on_request(req) -> None:
        if "authorization" in req.headers:
            auth_headers.setdefault("authorization", req.headers["authorization"])

    page = context.new_page()
    try:
        page.on("response", on_response)
        page.on("request", on_request)
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        _run_replays(page, recipe, captured, auth_headers)

        remaining, reloaded = wait_s, False
        while remaining > 0:
            page.wait_for_timeout(500)
            remaining -= 0.5
            try:
                return recipe.parse(captured)
            except ProviderError as exc:
                last_error = exc
            if remaining < wait_s / 2 and not reloaded:
                page.reload(wait_until="domcontentloaded")
                reloaded = True
    finally:
        page.close()
        pw.stop()                           # disconnect only; the browser stays

    raise ProviderError(
        f"the {name} console is not signed in (last error: {last_error}). "
        f"Run 'epd_monitor.py login --provider {name}' again."
    )


def _cdp_login(name: str, recipe: Recipe, profile: Path, timeout_s: float) -> bool:
    """Headed sign-in in the standalone browser, then park it off-screen for
    the regular fetches."""
    _ensure_cdp_browser(name, recipe, profile, on_screen=True)
    pw, _browser, context = _connect(name)
    captured: dict[str, list[dict[str, Any]]] = {}

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    page = context.new_page()
    try:
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        remaining = timeout_s
        while remaining > 0:
            page.wait_for_timeout(1000)
            remaining -= 1
            try:
                recipe.parse(captured)
                break
            except ProviderError:
                continue
    finally:
        try:
            page.close()
        except Exception:
            pass
        pw.stop()

    try:
        items = recipe.parse(captured)
    except ProviderError as exc:
        print(f"[{name}] login not completed ({exc}); run login again", file=sys.stderr)
        return False

    # The window stays open (the session lives in it); park it off-screen.
    try:
        pw2 = sync_playwright().start()
        browser = pw2.chromium.connect_over_cdp(f"http://127.0.0.1:{_cdp_port(name)}")
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else \
            browser.contexts[0].new_page()
        _park_offscreen(browser.contexts[0], page)
        pw2.stop()
    except Exception:
        pass
    print(f"[{name}] signed in ({len(items)} item(s)); browser parked off-screen "
          "and left running")
    return True


def _login(name: str, recipe: Recipe, profile: Path, timeout_s: float) -> bool:
    """Headed sign-in for providers whose cookies persist: the window closes
    once the parser accepts the captured data."""
    captured: dict[str, list[dict[str, Any]]] = {}

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    print(f"Opening {recipe.start_url}")
    print(f"-> {recipe.login_hint}")
    print(f"(完成登录后窗口自动关闭，最长等 {timeout_s:.0f}s)")
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        str(profile), channel=DEFAULT_CHANNEL, headless=False,
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()
    page.on("response", on_response)
    page.goto(recipe.start_url, wait_until="domcontentloaded")

    remaining = timeout_s
    while remaining > 0:
        page.wait_for_timeout(1000)
        remaining -= 1
        try:
            recipe.parse(captured)
            break
        except ProviderError:
            continue

    try:
        items = recipe.parse(captured)
    except ProviderError as exc:
        context.close()
        pw.stop()
        print(f"Profile: {profile} -> login not completed ({exc}); run login again",
              file=sys.stderr)
        return False
    context.close()
    pw.stop()
    print(f"Profile: {profile} -> signed in ({len(items)} item(s))")
    return True


def open_login(provider_type: str, timeout_s: float = 540.0,
               headless: bool = True) -> bool:
    """Open the provider's page headed and wait for the user to sign in.

    Headless providers close the window once signed in; session-cookie
    consoles keep their standalone browser running and park it off-screen.
    """
    recipe = RECIPES.get(provider_type)
    if recipe is None:
        raise ProviderError(f"unknown web provider '{provider_type}'; "
                            f"known: {sorted(RECIPES)}")
    if headless:
        return _PW_POOL.submit(_login, provider_type, recipe,
                               _profile_dir(provider_type), timeout_s).result()
    if _port_open(_cdp_port(provider_type)):
        # A standalone browser is already running; verify its session still works.
        return _cdp_fetch(recipe, provider_type, timeout_s)
    return _cdp_login(provider_type, recipe,
                      _profile_dir(provider_type), timeout_s)


class _WebProviderBase(ProviderBase):
    """Shared fetch for the recipe-driven web providers."""

    recipe: Recipe

    async def fetch(self) -> list[SubscriptionItem]:
        recipe = self.recipe
        name = self.provider_type
        profile = _profile_dir(name)
        wait_s = float(self._cfg.get("wait_seconds", DEFAULT_WAIT_S))
        if str(self._cfg.get("auth", "")).strip().lower() == "token":
            # Pilot: one browser login captures the SPA's in-memory Bearer
            # token (+ cookies); every fetch after that is plain httpx.
            return await _token_fetch(name, self._cfg)
        headless = bool(self._cfg.get("headless", True))
        if headless:
            return await _pw_run(_capture_and_parse, recipe, profile, wait_s)
        return await _pw_run(_cdp_fetch, recipe, name, wait_s)


def _make(name: str) -> type[ProviderBase]:
    impl = RECIPES[name]

    @register
    class _Provider(_WebProviderBase):
        provider_type = name
        recipe = impl

    _Provider.__name__ = f"{name.replace('-', '_')}_provider"
    return _Provider


for _type in RECIPES:
    _make(_type)

# ----------------------------------------------------------------------
# Kimi token mode (pilot)
# ----------------------------------------------------------------------
# The SPA keeps its real credential in the JS realm as an in-memory
# ``Authorization: Bearer`` header on every membership call.  One browser
# session captures that header (plus the request cookies and the UA), and
# ``auth = "token"`` then fetches the exact same endpoints with httpx - no
# Playwright, no profile lock, sub-second polls.  The playwright path stays
# the default until token lifetime is proven; ``auth = "token"`` opts in.
KIMI_ORIGIN = "https://www.kimi.com"
KIMI_MEMBERSHIP_API = (KIMI_ORIGIN
                       + "/apiv2/kimi.gateway.membership.v2.MembershipService")
KIMI_STATS_URL = KIMI_MEMBERSHIP_API + "/GetSubscriptionStats"
KIMI_SUBSCRIPTION_URL = KIMI_MEMBERSHIP_API + "/GetSubscription"

_TOKEN_FIELDS = ("authorization", "cookie", "user_agent",
                 "get_subscription_body", "captured_at")


def _token_store(name: str, root: Path | None = None) -> Path:
    """Where the captured header set lives; under profiles/ so it is gitignored."""
    return (root or PROFILE_ROOT) / name / "token.json"


def _mask_secret(value: str) -> str:
    """Show enough to tell tokens apart, never enough to use one."""
    if not value:
        return ""
    if len(value) <= 18:
        return "***"
    return f"{value[:12]}...{value[-4:]}"


def _save_token(name: str, data: dict[str, Any],
                root: Path | None = None) -> Path:
    path = _token_store(name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {k: data[k] for k in _TOKEN_FIELDS if k in data and data[k]}
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _load_token(name: str, root: Path | None = None) -> dict[str, str]:
    path = _token_store(name, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def _drop_token(name: str, root: Path | None = None) -> None:
    path = _token_store(name, root)
    try:
        path.unlink()
    except OSError:
        pass


def _capture_membership_headers(provider_type: str, recipe: Recipe,
                                page, auth: dict[str, str],
                                subscription_bodies: list[str],
                                capped_s: float) -> None:
    """Wait for the SPA's Bearer header (bounded), then for parseable data.

    ``capped_s`` bounds the header wait only; the caller owns the overall
    sign-in timeout.  Data may still be racing the bundle boot, so the caller
    keeps polling until ``recipe.parse`` accepts the captures.
    """
    deadline = time.monotonic() + capped_s
    while "authorization" not in auth and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    if provider_type == "kimi-web" and subscription_bodies:
        auth.setdefault("get_subscription_body", subscription_bodies[-1])


def token_capture_login(provider_type: str, timeout_s: float = 540.0,
                        *, headed: bool = True) -> bool:
    """Headed sign-in that also persists the SPA credential for token mode.

    Like ``_login`` but additionally records the in-memory ``Authorization``
    header, the request cookies and the UA on the membership API, then saves
    them to ``profiles/<type>/token.json`` (gitignored, 0600 best-effort).
    Return True when a token was stored.  With ``headed=False`` the existing
    profile is reused headless, for re-capturing a still-live session.
    """
    recipe = RECIPES.get(provider_type)
    if recipe is None:
        raise ProviderError(f"unknown web provider '{provider_type}'; "
                            f"known: {sorted(RECIPES)}")
    if provider_type != "kimi-web":
        raise ProviderError(
            f"[{provider_type}] token capture is wired for kimi-web only "
            "(pilot) - keep using the playwright path for now")
    profile = _profile_dir(provider_type)
    captured: dict[str, list[dict[str, Any]]] = {}
    auth: dict[str, str] = {}
    subscription_bodies: list[str] = []

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    def on_request(req) -> None:
        headers = req.headers
        # Only trust credentials observed on the membership endpoints the card
        # parses - the page fires other requests with different, shorter-lived
        # tokens, and saving the first one found turns into a stale 401.
        if any(fragment in req.url for fragment in recipe.expect):
            if "authorization" in headers:
                auth.setdefault("authorization", headers["authorization"])
            cookie = headers.get("cookie")
            if cookie and "cookie" not in auth:
                auth["cookie"] = cookie
            if ("GetSubscription" in req.url and "Stats" not in req.url
                    and req.method == "POST" and req.post_data):
                subscription_bodies.append(req.post_data)

    print(f"Opening {recipe.start_url}")
    if headed:
        print(f"-> {recipe.login_hint}  (token capture mode)")
        print(f"(完成登录后窗口自动关闭，最长等 {timeout_s:.0f}s)")
    else:
        print("-> reusing the existing logged-in profile (headless, no login)")
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        str(profile), channel=DEFAULT_CHANNEL, headless=not headed,
        viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.on("response", on_response)
    page.on("request", on_request)
    user_agent = ""
    try:
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        _capture_membership_headers(provider_type, recipe, page, auth,
                                    subscription_bodies, capped_s=8.0)
        remaining = timeout_s
        while remaining > 0:
            page.wait_for_timeout(500)
            remaining -= 0.5
            if "authorization" not in auth:
                continue
            try:
                recipe.parse(captured)
                break
            except ProviderError:
                continue
        try:
            user_agent = page.evaluate("navigator.userAgent")
        except Exception:
            user_agent = ""
    finally:
        try:
            page.close()
        except Exception:
            pass
        context.close()
        pw.stop()

    token = auth.get("authorization") or auth.get("cookie", "")
    if not token or not auth.get("authorization"):
        print(f"[{provider_type}] no Authorization header seen - not signed in? "
              "Run login again with headed login", file=sys.stderr)
        return False
    body = subscription_bodies[-1] if subscription_bodies else ""
    auth["user_agent"] = user_agent
    auth["get_subscription_body"] = body
    auth["captured_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    path = _save_token(provider_type, auth)
    print(f"[{provider_type}] token saved to {path} "
          f"(auth {_mask_secret(auth['authorization'])}, "
          f"cookie {_mask_secret(auth.get('cookie', ''))}, "
          f"sub_body {len(body)} bytes)")
    return True


async def _kimi_post(client: httpx.AsyncClient, url: str,
                     body: str) -> tuple[int, dict[str, Any] | None]:
    """POST one membership endpoint; (status, parsed dict) - never raises on
    an HTTP-level refusal so the caller can tell 401 from other failures."""
    try:
        resp = await client.post(url, content=body)
    except httpx.HTTPError as exc:
        raise ProviderError(f"[kimi-web] {url.rsplit('/', 1)[-1]} request "
                            f"failed: {exc}") from exc
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        data = resp.json()
    except ValueError:
        return resp.status_code, {}
    return resp.status_code, data if isinstance(data, dict) else {}


async def _token_fetch(name: str, cfg: dict[str, Any], *,
                       root: Path | None = None,
                       transport: httpx.AsyncBaseTransport | None = None,
                       ) -> list[SubscriptionItem]:
    """Fetch the card with pure httpx using the captured credential.

    Opt in with ``auth = "token"`` in the provider config.  On a 401 the
    saved token is dropped and the card reports the re-login command - the
    panel never shows stale numbers.
    """
    if name != "kimi-web":
        raise ProviderError(
            f"[{name}] token mode is wired for kimi-web only (pilot)")
    recipe = RECIPES[name]
    token = _load_token(name, root)
    authorization = token.get("authorization", "")
    if not authorization:
        raise ProviderError(
            f"[{name}] no saved token - run "
            "'python epd_monitor.py login --provider kimi-web' once "
            "(headed; it captures the SPA credential for auth=token mode)")
    timeout = float(cfg.get("timeout", 20) or 20)
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": KIMI_ORIGIN,
        "Referer": KIMI_ORIGIN + "/",
        "Authorization": authorization,
        "User-Agent": (token.get("user_agent")
                       or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36"),
    }
    cookie = token.get("cookie", "")
    if cookie:
        headers["Cookie"] = cookie

    async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                 transport=transport) as client:
        status, stats = await _kimi_post(client, KIMI_STATS_URL, "{}")
        if stats is None:
            if status == 401:
                _drop_token(name, root)
                raise ProviderError(
                    f"[{name}] the saved token expired (HTTP 401) - token "
                    "cleared, run 'python epd_monitor.py login "
                    "--provider kimi-web' again")
            raise ProviderError(f"[{name}] GetSubscriptionStats HTTP {status}")
        # The title/credits live in GetSubscription; a wrong body just loses
        # the title - the stats response carries subscriptionBalance on the
        # quota tab (2026-09-01 captures), so the bar still renders.
        sub = stats
        if status == 200:
            _, sub = await _kimi_post(client, KIMI_SUBSCRIPTION_URL,
                                      token.get("get_subscription_body") or "{}")
            if not isinstance(sub, dict) or not sub:
                sub = stats

    bodies: dict[str, Any] = {KIMI_STATS_FRAGMENT: [stats],
                              KIMI_SUBSCRIPTION_FRAGMENT: [sub]}
    return recipe.parse(bodies)
