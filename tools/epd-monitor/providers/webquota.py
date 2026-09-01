"""Subscription providers that read the logged-in web console via Playwright.

The vendor APIs for these subscriptions either do not exist (Bailian Token
Plan, Kimi membership) or report far less than the console shows (DeepSeek's
API key endpoint has no total-spend figure).  The console pages themselves call
internal endpoints whose responses carry the real numbers, so this module
drives a persistent, logged-in browser profile to the page, captures those
responses, and parses them.

The recipes were written from responses captured on 2026-09-01 (see
test_providers.py, which replays those payloads offline).  The auth token for
these pages lives in the page's JS realm - replaying cookies from a plain HTTP
client gets a 401 on Kimi - so the browser is the integration point, not a
shortcut.

Profiles live in ``profiles/<provider>/`` next to the tool; a profile starts
empty, so run ``epd_monitor.py login <provider>`` once per provider to sign in
headed.  Afterwards fetches run headless in the same profile.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from providers import ProviderBase, ProviderError, SubscriptionItem, register

PROFILE_ROOT = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_CHANNEL = "msedge"          # drive the locally installed Edge: no download
DEFAULT_WAIT_S = 45.0


@dataclass(frozen=True)
class Recipe:
    """What to open, which responses to capture, and how to read them."""

    start_url: str
    expect: tuple[str, ...]                       # URL fragments to capture
    parse: Callable[[dict[str, Any]], list[SubscriptionItem]]
    login_hint: str


def _cents(amount: str | float) -> int:
    return int(round(float(amount) * 100))


def _md(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%m-%d")


DEEPSEEK_SUMMARY_FRAGMENT = "users/get_user_summary"
KIMI_SUBSCRIPTION_FRAGMENT = "MembershipService/GetSubscription"
ALIYUN_USAGE_FRAGMENT = "tokenplan/personal/api/v2/usage"
ALIYUN_SUBSCRIPTION_FRAGMENT = "tokenplan/personal/api/v2/subscription"


def parse_deepseek(responses: dict[str, list[dict[str, Any]]]) -> list[SubscriptionItem]:
    for body in reversed(responses.get(DEEPSEEK_SUMMARY_FRAGMENT) or []):
        biz = (body.get("data") or {}).get("biz_data") or {}
        wallets = biz.get("normal_wallets") or []
        if not wallets:
            continue
        note = ""
        costs = biz.get("total_costs") or []
        if costs:
            note = f"累计 ¥{float(costs[0].get('amount', 0)):,.2f}"
        return [SubscriptionItem(plan_name="DeepSeek", quota_total=0, quota_used=0,
                                 balance=_cents(wallets[0].get("balance", 0)),
                                 unit="CNY", note=note)]
    raise ProviderError("[DeepSeek] usage page returned no wallet data")


def parse_kimi(responses: dict[str, list[dict[str, Any]]]) -> list[SubscriptionItem]:
    bodies = responses.get(KIMI_SUBSCRIPTION_FRAGMENT) or []

    title, rates, credits = "", {}, None
    for body in reversed(bodies):
        if not title and (body.get("subscription") or {}).get("goods"):
            title = body["subscription"]["goods"].get("title", "")
        if "ratelimitCode7d" in body:
            rates = body
        if credits is None and (body.get("subscriptionBalance") or {}).get("amountUsedRatio") is not None:
            credits = body["subscriptionBalance"]
        if credits is None:
            for b in body.get("balances", []):
                if "amountUsedRatio" in b:
                    credits = b
                    break

    if not rates and credits is None:
        raise ProviderError("[Kimi] subscription page returned no quota balance")

    # The bar tracks the 7-day coding window: it is the quota that actually
    # gates usage, and the reason the quota tab exists.
    if rates:
        used, total = round(float(rates["ratelimitCode7d"]["ratio"]) * 1000), 1000
    elif credits:
        used, total = round(float(credits["amountUsedRatio"]) * 1000), 1000

    # Note budget fits one small line, so it carries credits, the 7-day reset
    # and expiry; the 5-hour window is too volatile for a 30-min display.
    note = []
    if credits is not None:
        note.append(f"credits {float(credits['amountUsedRatio']) * 100:.1f}%")
    if rates:
        reset7d = (rates["ratelimitCode7d"] or {}).get("resetTime")
        if reset7d:
            note.append(f"7天重置 {_md(reset7d)}")
    if credits is not None and credits.get("expireTime"):
        note.append(f"{_md(credits['expireTime'])} 到期")
    return [SubscriptionItem(plan_name=f"Kimi {title}".strip(), quota_total=total,
                             quota_used=used, balance=0, unit="%", note="·".join(note))]


ALIYUN_USAGE_FRAGMENT = "tokenplan/personal/api/v2/usage"
ALIYUN_SUBSCRIPTION_FRAGMENT = "tokenplan/personal/api/v2/subscription"


def parse_aliyun(responses: dict[str, list[dict[str, Any]]]) -> list[SubscriptionItem]:
    def last_with(fragment: str, key: str) -> dict[str, Any]:
        for body in reversed(responses.get(fragment) or []):
            inner = (((body.get("data") or {}).get("DataV2") or {}).get("data") or {}).get("data") or {}
            if key in inner:
                return inner
        return {}

    usage = last_with(ALIYUN_USAGE_FRAGMENT, "per1WeekPercentage")
    sub = last_with(ALIYUN_SUBSCRIPTION_FRAGMENT, "specCode")
    if not usage:
        raise ProviderError("[Aliyun] token-plan page returned no usage data")

    used = round(float(usage["per1WeekPercentage"]) * 1000)
    spec = sub.get("specCode", "")
    days = sub.get("remainingDays")
    note = f"{spec}·剩{days}天" if days is not None else spec
    return [SubscriptionItem(plan_name="Aliyun TokenPlan", quota_total=1000,
                             quota_used=used, balance=0, unit="%", note=note)]


RECIPES: dict[str, Recipe] = {
    "deepseek-web": Recipe(
        start_url="https://platform.deepseek.com/usage",
        expect=(DEEPSEEK_SUMMARY_FRAGMENT,),
        parse=parse_deepseek,
        login_hint="登录 DeepSeek 开放平台（手机验证码或微信扫码）",
    ),
    "kimi-web": Recipe(
        start_url="https://www.kimi.com/membership/subscription?tab=quota",
        expect=(KIMI_SUBSCRIPTION_FRAGMENT,),
        parse=parse_kimi,
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

    try:
        import socket as s
        with s.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _ensure_cdp_browser(name: str, recipe: Recipe, profile: Path,
                        on_screen: bool) -> int:
    """Start the provider's standalone Edge if it is not already running."""
    import subprocess

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


_PW_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="epd-playwright")


async def _pw_run(fn, *args) -> list[SubscriptionItem]:
    """Run one Playwright operation on the single browser thread: sync
    Playwright objects are bound to the thread that created them, and the
    standalone browser must always be touched from the same one."""
    return await asyncio.wrap_future(_PW_POOL.submit(fn, *args))


def _headless_fetch(recipe: Recipe, profile: Path, wait_s: float) -> list[SubscriptionItem]:
    """Launch headless and poll until the parser accepts the captured bodies.

    Counting captured fragments is not enough: on a cold start the SPA can hit
    the endpoint before its session refresh and produce an empty
    unauthenticated shape, so the parser - not the fragment count - decides
    when the data is real, and one reload is thrown in midway.
    """
    captured: dict[str, Any] = {}
    last_error: ProviderError | None = None

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:       # non-JSON or empty body: keep waiting
                    pass

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), channel=DEFAULT_CHANNEL, headless=True,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")

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


def _cdp_fetch(recipe: Recipe, name: str, wait_s: float) -> list[SubscriptionItem]:
    """One fetch through the resident standalone browser."""
    _ensure_cdp_browser(name, recipe, _profile_dir(name), on_screen=False)
    pw, _browser, context = _connect(name)
    captured: dict[str, list[dict[str, Any]]] = {}
    last_error: ProviderError | None = None

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

    On success the window parks itself off-screen and stays running for later
    fetches.  If a resident browser is already signed in, this is a no-op.
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
        headless = bool(self._cfg.get("headless", True))
        wait_s = float(self._cfg.get("wait_seconds", DEFAULT_WAIT_S))
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
