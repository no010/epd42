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
import threading
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


def parse_deepseek(data: dict[str, Any]) -> list[SubscriptionItem]:
    biz = (data.get("data") or {}).get("biz_data") or {}
    wallets = biz.get("normal_wallets") or []
    if not wallets:
        raise ProviderError("[DeepSeek] usage page returned no wallet data")
    balance = _cents(wallets[0].get("balance", 0))
    note = ""
    costs = biz.get("total_costs") or []
    if costs:
        note = f"累计 ¥{float(costs[0].get('amount', 0)):,.2f}"
    return [SubscriptionItem(plan_name="DeepSeek", quota_total=0, quota_used=0,
                             balance=balance, unit="CNY", note=note)]


def parse_kimi(data: dict[str, Any]) -> list[SubscriptionItem]:
    sub = data.get("subscription") or {}
    title = (sub.get("goods") or {}).get("title", "")
    balances = [b for b in data.get("balances", []) if "amountUsedRatio" in b]
    if not balances:
        raise ProviderError("[Kimi] subscription page returned no quota balance")
    quota = balances[0]
    used = round(float(quota["amountUsedRatio"]) * 10_000)
    note = f"{_md(quota['expireTime'])} 到期" if quota.get("expireTime") else ""
    return [SubscriptionItem(plan_name=f"Kimi {title}".strip(), quota_total=10_000,
                             quota_used=used, balance=0, unit="%", note=note)]


ALIYUN_USAGE_FRAGMENT = "tokenplan/personal/api/v2/usage"
ALIYUN_SUBSCRIPTION_FRAGMENT = "tokenplan/personal/api/v2/subscription"


def parse_aliyun(responses: dict[str, Any]) -> list[SubscriptionItem]:
    def inner(fragment: str) -> dict[str, Any]:
        body = responses.get(fragment) or {}
        return (((body.get("data") or {}).get("DataV2") or {}).get("data") or {}).get("data") or {}

    usage = inner(ALIYUN_USAGE_FRAGMENT)
    sub = inner(ALIYUN_SUBSCRIPTION_FRAGMENT)
    if "per1WeekPercentage" not in usage:
        raise ProviderError("[Aliyun] token-plan page returned no usage data")

    used = round(float(usage["per1WeekPercentage"]) * 10_000)
    spec = sub.get("specCode", "")
    days = sub.get("remainingDays")
    note = f"{spec}·剩{days}天" if days is not None else spec
    return [SubscriptionItem(plan_name="Aliyun TokenPlan", quota_total=10_000,
                             quota_used=used, balance=0, unit="%", note=note)]


RECIPES: dict[str, Recipe] = {
    "deepseek-web": Recipe(
        start_url="https://platform.deepseek.com/usage",
        expect=("users/get_user_summary",),
        parse=parse_deepseek,
        login_hint="登录 DeepSeek 开放平台（手机验证码或微信扫码）",
    ),
    "kimi-web": Recipe(
        start_url="https://www.kimi.com/membership/subscription?tab=quota",
        expect=("MembershipService/GetSubscription",),
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


def _capture_responses(recipe: Recipe, profile: Path, headless: bool,
                       wait_s: float) -> dict[str, Any]:
    """Open the page and collect the JSON bodies of the expected responses."""
    from playwright.sync_api import sync_playwright

    captured: dict[str, Any] = {}
    lock = threading.Lock()

    def on_response(resp) -> None:
        url = resp.url
        for fragment in recipe.expect:
            if fragment in url and fragment not in captured:
                try:
                    with lock:
                        captured[fragment] = resp.json()
                except Exception:       # non-JSON or empty body: keep waiting
                    pass

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), channel=DEFAULT_CHANNEL, headless=headless,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")

        deadline = wait_s
        while len(captured) < len(recipe.expect) and deadline > 0:
            page.wait_for_timeout(500)
            deadline -= 0.5
        if len(captured) < len(recipe.expect):
            page.reload(wait_until="domcontentloaded")
            deadline = wait_s / 2
            while len(captured) < len(recipe.expect) and deadline > 0:
                page.wait_for_timeout(500)
                deadline -= 0.5

        context.close()

    missing = [f for f in recipe.expect if f not in captured]
    if missing:
        raise ProviderError(
            "did not see the expected responses on the page "
            f"({', '.join(missing)}). If this keeps failing, run "
            f"'epd_monitor.py login' again - the session may have expired."
        )
    return captured


def open_login(provider_type: str, timeout_s: float = 300.0) -> bool:
    """Open the provider's page headed and wait for the user to sign in.

    Completion is detected by the page itself: the login is usable exactly when
    the expected endpoints start answering, so no keypress is needed - an agent
    can run this on the user's behalf.
    """
    recipe = RECIPES.get(provider_type)
    if recipe is None:
        raise ProviderError(f"unknown web provider '{provider_type}'; "
                            f"known: {sorted(RECIPES)}")
    from playwright.sync_api import sync_playwright

    profile = _profile_dir(provider_type)
    captured: dict[str, Any] = {}
    lock = threading.Lock()

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url and fragment not in captured:
                try:
                    with lock:
                        if fragment in resp.url and resp.url:
                            captured[fragment] = resp.json()
                except Exception:
                    pass

    print(f"Opening {recipe.start_url}\n-> {recipe.login_hint}\n"
          f"(the window closes itself once the page loads your data, max {timeout_s:.0f}s)")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), channel=DEFAULT_CHANNEL, headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")

        try:
            remaining = timeout_s
            while len(captured) < len(recipe.expect) and remaining > 0:
                page.wait_for_timeout(1000)
                remaining -= 1
        finally:
            context.close()

    done = len(captured) == len(recipe.expect)
    print(f"Profile: {profile} -> {'signed in' if done else 'timed out; run login again'}")
    return done


class _WebProviderBase(ProviderBase):
    """Shared fetch for the recipe-driven web providers."""

    recipe: Recipe

    async def fetch(self) -> list[SubscriptionItem]:
        recipe = self.recipe
        profile = _profile_dir(self.provider_type)
        headless = not bool(self._cfg.get("headed", False))
        wait_s = float(self._cfg.get("wait_seconds", DEFAULT_WAIT_S))
        return await asyncio.to_thread(
            _capture_responses, recipe, profile, headless, wait_s
        )


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
