#!/usr/bin/env python3
"""Offline tests for the provider layer - no network, no API keys.

    python test_providers.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import httpx
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from providers import ProviderError, SubscriptionItem, create
from providers.aliyun import canonical_query, parse_packages, signed_headers


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)
    print(f"  ok  {description}")


def test_aliyun_signing() -> None:
    print("aliyun signing")
    check(canonical_query({"b": "x y", "a": "a/b+c"}) == "a=a%2Fb%2Bc&b=x%20y",
          "query is name-sorted and percent-encoded strictly (space is %20, not +)")

    params = {"Action": "QueryResourcePackageInstances", "Version": "2017-12-14",
              "Format": "JSON", "PageNum": "1"}
    fixed = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    headers = signed_headers(params, "AKID", "secret", now=fixed, nonce="nonce")

    check(headers["x-acs-action"] == params["Action"]
          and headers["x-acs-version"] == params["Version"]
          and headers["x-acs-date"] == "2026-09-01T00:00:00Z",
          "the signed headers carry action, version and the ISO timestamp")
    check(headers["Authorization"].startswith("ACS3-HMAC-SHA256 Credential=AKID,Signature="),
          "Authorization follows the ACS3 credential form")

    again = signed_headers(params, "AKID", "secret", now=fixed, nonce="nonce")
    check(headers["Authorization"] == again["Authorization"],
          "signing is deterministic for identical inputs")

    other = signed_headers(params, "AKID", "SECRET", now=fixed, nonce="nonce")
    check(headers["Authorization"] != other["Authorization"],
          "a different secret produces a different signature")


def test_aliyun_parsing() -> None:
    print("aliyun parsing")
    payload = {
        "Data": {"Instances": {"Instance": [
            {"Status": "Available", "Remark": "Token计划-个人版",
             "TotalAmount": 500, "TotalAmountUnit": "万Token",
             "RemainingAmount": 320.4, "RemainingAmountUnit": "万Token"},
            {"Status": "Expired", "Remark": "Token计划-过期",
             "TotalAmount": 100, "TotalAmountUnit": "万Token",
             "RemainingAmount": 0, "RemainingAmountUnit": "万Token"},
            {"Status": "Available", "Remark": "对象存储 OSS 包",
             "TotalAmount": 100, "TotalAmountUnit": "GB",
             "RemainingAmount": 40, "RemainingAmountUnit": "GB"},
        ]}},
    }

    items, seen = parse_packages(payload, "")
    check(seen == ["Token计划-个人版", "对象存储 OSS 包"],
          "only Available packages are reported and become items")
    check((items[0].quota_total, items[0].quota_used) == (5_000_000, 1_796_000),
          f"万Token amounts are scaled to plain tokens and used is derived "
          f"({items[0].quota_used:,} of {items[0].quota_total:,})")
    check(items[0].unit == "token", "the unit is normalised to plain 'token'")
    check(items[1].unit == "GB" and items[1].quota_used == 60,
          "unknown units pass through with used still derived by subtraction")

    filtered, seen = parse_packages(payload, "token")
    check(len(filtered) == 1 and filtered[0].plan_name == "Token计划-个人版",
          "the keyword filter keeps only matching packages")

    single, _ = parse_packages(
        {"Data": {"Instances": {"Instance": payload["Data"]["Instances"]["Instance"][0]}}}, "")
    check(len(single) == 1, "a single instance, which Aliyun returns unwrapped, still parses")


def test_bailian_gateway() -> None:
    print("bailian gateway")
    from providers.bailian import (API_USAGE, build_form, build_params, gateway_for,
                                   gateway_url)

    check(gateway_for("cn-beijing", "domestic")
          == ("bailian-cs.console.aliyun.com", "BroadScopeAspnGateway"),
          "Beijing/domestic uses the aliyun.com host and the domestic action")
    check(gateway_for("ap-southeast-1", "international")
          == ("bailian-singapore-cs.alibabacloud.com", "IntlBroadScopeAspnGateway"),
          "Singapore/international has its own host and the Intl action")
    check(gateway_for("mars-1", "domestic") == gateway_for("cn-beijing", "domestic"),
          "an unknown region falls back to Beijing, like the CLI does")

    url = gateway_url("bailian-cs.console.aliyun.com", "BroadScopeAspnGateway",
                      API_USAGE)
    check(url == "https://bailian-cs.console.aliyun.com/cli/api.json"
                 "?action=BroadScopeAspnGateway&product=sfm_bailian"
                 "&api=zeldaHttp.apikeyMgr.%2Ftokenplan%2Fpersonal%2Fapi%2Fv2%2Fusage",
          "the api name is percent-encoded into the query (a slash becomes %2F)")

    form = build_form(API_USAGE, {}, "cn-beijing", 11253894)
    check(sorted(form) == ["params", "region"] and form["region"] == "cn-beijing",
          "the urlencoded body is params + region")
    params = json.loads(form["params"])
    check(params["Api"] == API_USAGE and params["V"] == "1.0",
          "params names the API and the gateway version")
    corner = params["Data"]["cornerstoneParam"]
    sent = (corner["protocol"], corner["console"], corner["productCode"],
            corner["switchUserType"], corner["consoleSite"])
    check(sent == ("V2", "ONE_CONSOLE", "p_efm", 3, "BAILIAN_ALIYUN"),
          "cornerstoneParam matches what bailian-cli sends")
    check(corner["switchAgent"] == 11253894, "a delegated switch agent is passed on")
    bare = json.loads(build_params(API_USAGE, {}, None))["Data"]["cornerstoneParam"]
    check("switchAgent" not in bare, "without an agent uid the key is omitted")


def test_bailian_parsing() -> None:
    print("bailian parsing")
    from providers.bailian import parse_token_plan, unwrap

    # Captured 2026-09-07 through 'bl console call', gateway envelope intact.
    usage_body = {
        "code": "200",
        "data": {"DataV2": {"ret": ["SUCCESS::接口调用成功"],
                            "data": {"msg": "Success.", "code": "SUCCESS",
                                     "success": True, "requestId": "4ea79581",
                                     "data": {"per1WeekResetTime": 1789356540000,
                                              "per1WeekPercentage": 0.0670188175}}},
                 "success": True, "httpStatus": 200, "errorCode": "", "errorMsg": ""},
        "httpStatusCode": "200", "successResponse": True}
    usage = unwrap(usage_body, api="usage")
    check(usage["per1WeekPercentage"] == 0.0670188175,
          "unwrap walks data -> DataV2.data -> data")

    sub_body = {"data": {"DataV2": {"data": {"success": True, "code": "SUCCESS",
                                             "data": {"specCode": "pro",
                                                      "remainingDays": 14,
                                                      "status": "VALID"}}},
                         "success": True, "errorCode": ""}}
    check(unwrap(sub_body, api="subscription")["remainingDays"] == 14,
          "the subscription payload keeps remainingDays")

    items = parse_token_plan(usage, unwrap(sub_body, api="subscription"))
    check(items[0].plan_name == "Aliyun TokenPlan" and items[0].unit == "%",
          "the card is the one aliyun-web already draws")
    check((items[0].quota_total, items[0].quota_used) == (100, 7),
          "the bar is weekly usage: 6.70% used rounds to 7")
    for part in ("rst 09-14 11:29", "14d"):
        check(part in items[0].note, f"note carries {part!r}")

    hourly = parse_token_plan({**usage, "per5HourPercentage": 0.42}, {})
    check(hourly[0].extra == "5h 42%", "a 5-hour window rides the metrics line")
    check(hourly[0].note == "rst 09-14 11:29", "no subscription -> no day count")

    expired = "an expired console session points at the bl auth login hint"
    try:
        unwrap({"data": {"success": False, "errorCode": "NotLogined"}}, api="usage")
    except ProviderError as exc:
        check("bl auth login" in str(exc), expired)
    else:
        check(False, expired)

    missing = "a payload without per1WeekPercentage errors instead of showing 0%"
    try:
        parse_token_plan({}, {})
    except ProviderError:
        check(True, missing)
    else:
        check(False, missing)

def test_bailian_login_url() -> None:
    print("bailian login url")
    from providers.bailian import build_login_url, login_page, mask_token, new_state

    check(login_page("domestic") == "https://bailian.console.aliyun.com",
          "domestic signs in at bailian.console.aliyun.com")
    check(login_page("international")
          == "https://modelstudio.console.alibabacloud.com",
          "international signs in at modelstudio.console.alibabacloud.com")
    check(login_page("nonsense") == login_page("domestic"),
          "an unknown site falls back to domestic")

    url = build_login_url("https://bailian.console.aliyun.com", 51234, "ab12")
    check(url == "https://bailian.console.aliyun.com/console-login"
                 "?notice=127.0.0.1:51234?state=ab12",
          "the callback address rides inside 'notice', with a second ? not &")
    with_key = build_login_url("https://x", 1, "s", need_api_key=True)
    check(with_key.endswith("&needapikey=true"),
          "need_api_key appends the same flag the CLI appends")
    check(len(new_state()) == 32 and new_state() != new_state(),
          "state is 32 hex chars and never repeats")
    check(mask_token("bb10abcd1234ef88e7") == "bb10...88e7",
          "a token is masked the way bl auth status masks it")


def test_bailian_callback_parsing() -> None:
    print("bailian login callback")
    from providers.bailian import parse_callback

    got = parse_callback("state=abc&access_token=tok123&console_site=domestic", "", "")
    check(got == {"access_token": "tok123", "console_site": "domestic"},
          "GET query credentials are normalised to snake_case")

    form = parse_callback("", "accessToken=tok1&consoleSwitchAgent=42&workspaceId=ws",
                          "application/x-www-form-urlencoded")
    check(form == {"access_token": "tok1", "console_switch_agent": 42,
                   "workspace_id": "ws"},
          "camelCase form bodies work and the agent uid becomes an int")

    body = parse_callback("", '{"access_token":"tok2","console_region":"cn-beijing"}',
                          "application/json")
    check(body == {"access_token": "tok2", "console_region": "cn-beijing"},
          "a JSON body is read as well")

    check(parse_callback("state=x", "", "") == {},
          "a callback carrying no credentials yields nothing to store")
    bad = parse_callback("", "console_switch_agent=not-a-number",
                         "application/x-www-form-urlencoded")
    check("console_switch_agent" not in bad,
          "an unusable agent uid is dropped, not stored as a string")


def test_bailian_store() -> None:
    print("bailian credential store")
    from providers.bailian import (load_credentials, resolve_credentials,
                                   save_credentials)

    held = os.environ.pop("BAILIAN_ACCESS_TOKEN", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "bailian" / "console.json"
            check(load_credentials(store) == {}, "a missing store reads as empty")

            save_credentials({"access_token": "tok", "console_site": "domestic",
                              "junk": "dropped"}, store)
            check(load_credentials(store) == {"access_token": "tok",
                                              "console_site": "domestic"},
                  "only known credential fields are persisted")

            cred, source = resolve_credentials({"store": str(store)})
            check(cred["access_token"] == "tok" and source.startswith("store:"),
                  "the own store wins over the bl config file")

            inline, label = resolve_credentials({"access_token": "cfg-token",
                                                 "store": str(store)})
            check(inline["access_token"] == "cfg-token" and label == "config",
                  "an explicit access_token beats the store")

            gone = "with no token anywhere the error names both login routes"
            try:
                resolve_credentials({"store": str(Path(tmp) / "nope.json"),
                                     "cli_config": str(Path(tmp) / "none.json")})
            except ProviderError as exc:
                check("login --provider bailian" in str(exc), gone)
            else:
                check(False, gone)
    finally:
        if held is not None:
            os.environ["BAILIAN_ACCESS_TOKEN"] = held



def test_kimi_token() -> None:
    print("kimi token mode")
    from providers.webquota import (_drop_token, _load_token, _mask_secret,
                                    _save_token, _token_fetch, _token_store)

    stats_body = {"ratelimitCode5h": {"ratio": 0.0681, "enabled": True,
                                      "resetTime": "2026-09-01T10:22:33Z"},
                  "ratelimitCode7d": {"ratio": 0.3922, "enabled": True,
                                      "resetTime": "2026-09-05T01:22:33Z"},
                  "subscriptionBalance": {"feature": "FEATURE_OMNI",
                                          "amountUsedRatio": 0.277,
                                          "expireTime":
                                              "2026-09-25T01:22:33.648851Z"}}
    sub_body = {"subscription": {"goods": {"title": "Allegretto"}},
                "balances": [{"feature": "FEATURE_OMNI",
                              "amountUsedRatio": 0.2743,
                              "expireTime": "2026-09-25T01:22:33.648851Z"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetSubscriptionStats"):
            return httpx.Response(200, json=stats_body)
        if request.url.path.endswith("GetSubscription"):
            return httpx.Response(200, json=sub_body)
        return httpx.Response(404, json={})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check(_load_token("kimi-web", root) == {},
              "a missing token store reads as empty")

        saved = {"authorization": "Bearer fake-token-12345", "junk": "x",
                 "cookie": "kimi_token=abc", "user_agent": "UA",
                 "captured_at": "2026-09-07T10:00:00+08:00"}
        _save_token("kimi-web", saved, root)
        restored = _load_token("kimi-web", root)
        check("junk" not in restored and restored["authorization"]
              == "Bearer fake-token-12345",
              "only the whitelisted credential fields are persisted")
        check(_mask_secret("Bearer fake-token-12345").endswith("2345"),
              "tokens print masked, never in full")

        transport = httpx.MockTransport(handler)
        items = asyncio.run(_token_fetch("kimi-web", {"timeout": 5},
                                         root=root, transport=transport))
        check(items[0].plan_name == "Kimi Allegretto",
              "the httpx path produces the same card as the browser path")
        check(items[0].quota_used == 7, "the bar reads the 5h window (6.8%)")
        for part in ("Mo 27%", "Wk 39%", "7d rst 09-05", "exp 09-25"):
            check(part in items[0].note, f"note carries {part!r}")
        check(items[0].extra == "rst 18:22", "the 5h reset rides the metrics line")

        def expired(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={})

        gone = "a 401 clears the stored token and names the re-login command"
        try:
            asyncio.run(_token_fetch("kimi-web", {"timeout": 5,
                                                  "auto_refresh": False},
                                     root=root,
                                     transport=httpx.MockTransport(expired)))
        except ProviderError as exc:
            check("login --provider kimi-web" in str(exc), gone)
        else:
            check(False, gone)
        check(not _token_store("kimi-web", root).exists(),
              "the expired token file is removed after a 401")
        _drop_token("kimi-web", root)

        try:
            asyncio.run(_token_fetch("deepseek-web", {"auto_refresh": False},
                                     root=root))
        except ProviderError as exc:
            check("no saved token" in str(exc) and "deepseek-web" in str(exc),
                  "a wired provider with no stored token gets the login hint")
        else:
            check(False,
                  "a wired provider with no stored token gets the login hint")



def test_deepseek_token() -> None:
    print("deepseek token mode")
    from urllib.parse import parse_qs, urlparse

    from providers.webquota import (_drop_token, _load_token,
                                    _token_fetch, token_capture_login)

    summary = {"code": 0, "data": {"biz_data": {
        "normal_wallets": [{"currency": "CNY",
                            "balance": "59.1114450400000000"}],
        "total_costs": [{"currency": "CNY",
                         "amount": "271.1589545600000000"}]}}}

    def usage_body(start: int) -> dict:
        return {"code": 0, "data": {"biz_data": {"series": [
            {"model": "deepseek-v4-flash", "buckets": [
                {"time": start, "usage": {"RESPONSE_TOKEN": 10_000_000,
                                          "PROMPT_CACHE_HIT_TOKEN": 5_000_000}}]},
            {"model": "deepseek-v4-pro", "buckets": [
                {"time": start, "usage": {"RESPONSE_TOKEN": 5_000_000}}]}]}}}

    def cost_body(start: int) -> dict:
        return {"code": 0, "data": {"biz_data": {"data": [{"series": [
            {"model": "deepseek-v4-flash", "buckets": [
                {"time": start, "cost": "0.5"}]}]}]}}}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        query = parse_qs(urlparse(url).query)
        start = int(query.get("start", ["0"])[0])
        if "get_user_summary" in url:
            return httpx.Response(200, json=summary)
        if "usage/by_api_key/amount" in url:
            return httpx.Response(200, json=usage_body(start))
        if "usage/by_api_key/cost" in url:
            return httpx.Response(200, json=cost_body(start))
        return httpx.Response(404, json={})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check(token_capture_login("deepseek-web", token="sk-ds-token-123",
                                  root=root) is True,
              "a pasted Authorization header is saved without a browser")
        check(_load_token("deepseek-web", root).get("authorization")
              == "Bearer sk-ds-token-123",
              "a bare token is normalised to a Bearer header")

        items = asyncio.run(_token_fetch("deepseek-web", {"timeout": 5},
                                         root=root,
                                         transport=httpx.MockTransport(handler)))
        check(items[0].balance == 5911, "balance parses to cents over httpx")
        check(items[0].extra == "/ ¥271", "lifetime spend rides the metrics line")
        for part in ("tdy ¥0.50", "tok 20.0M", "F75%/P25%"):
            check(part in items[0].note, f"note carries {part!r}")

        def expired(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={})

        gone = "a 401 clears the deepseek token and names the re-login command"
        try:
            asyncio.run(_token_fetch("deepseek-web", {"timeout": 5,
                                                      "auto_refresh": False},
                                     root=root,
                                     transport=httpx.MockTransport(expired)))
        except ProviderError as exc:
            check("login --provider deepseek-web" in str(exc), gone)
        else:
            check(False, gone)
        check(not Path(tmp, "deepseek-web", "token.json").exists(),
              "the expired deepseek token is removed after a 401")
        _drop_token("deepseek-web", root)

    try:
        asyncio.run(_token_fetch("aliyun-web", {}))
    except ProviderError as exc:
        check("kimi-web, deepseek-web" in str(exc),
              "token mode names exactly the providers it is wired for")
    else:
        check(False, "token mode names exactly the providers it is wired for")



def test_token_auto_refresh() -> None:
    print("token auto-refresh")
    from providers import webquota as wq
    from providers.webquota import _save_token, _token_fetch

    stats_body = {"ratelimitCode5h": {"ratio": 0.05, "enabled": True,
                                      "resetTime": "2026-09-07T10:22:33Z"},
                  "ratelimitCode7d": {"ratio": 0.2, "enabled": True,
                                      "resetTime": "2026-09-05T01:22:33Z"},
                  "subscriptionBalance": {"amountUsedRatio": 0.1,
                                          "expireTime":
                                              "2026-09-25T01:22:33Z"}}
    stats_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("GetSubscriptionStats"):
            stats_hits["n"] += 1
            if stats_hits["n"] == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json=stats_body)
        return httpx.Response(404, json={})

    def fake_capture(provider_type: str, timeout_s: float = 540.0, *,
                     headed: bool = True, token: str | None = None,
                     root=None) -> bool:
        _save_token("kimi-web", {"authorization": "Bearer fresh",
                                 "captured_at": "2026-09-07T18:00:00+08:00"},
                    root=root)
        return True

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _save_token("kimi-web", {"authorization": "Bearer stale",
                                 "captured_at": "2026-09-07T17:00:00+08:00"},
                    root=root)
        with mock.patch.object(wq, "token_capture_login", fake_capture):
            items = asyncio.run(_token_fetch("kimi-web", {"timeout": 5},
                                             root=root,
                                             transport=httpx.MockTransport(handler)))
        check(items[0].plan_name.startswith("Kimi"),
              "a 401 re-captures headless and the card still renders")
        check(stats_hits["n"] == 2, "exactly one retry after the 401")



def test_kimi_refresh_flow() -> None:
    print("kimi refresh flow")
    from providers.webquota import (_load_token, _save_token, _token_fetch)

    stats_body = {"ratelimitCode5h": {"ratio": 0.05, "enabled": True,
                                      "resetTime": "2026-09-07T10:22:33Z"},
                  "ratelimitCode7d": {"ratio": 0.2, "enabled": True,
                                      "resetTime": "2026-09-05T01:22:33Z"},
                  "subscriptionBalance": {"amountUsedRatio": 0.1,
                                          "expireTime":
                                              "2026-09-25T01:22:33Z"}}
    stats_hits, refresh_hits = {"n": 0}, {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("GetSubscriptionStats"):
            stats_hits["n"] += 1
            if stats_hits["n"] == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json=stats_body)
        if url.endswith("AuthService/RefreshToken"):
            refresh_hits["n"] += 1
            return httpx.Response(200, json={"accessToken": "fresh-access-1",
                                             "refreshToken": "fresh-refresh-1"})
        return httpx.Response(404, json={})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _save_token("kimi-web", {"authorization": "Bearer expired-access",
                                 "refresh_token": "initial-refresh",
                                 "captured_at": "2026-09-07T17:00:00+08:00"},
                    root=root)
        items = asyncio.run(_token_fetch("kimi-web", {"timeout": 5},
                                         root=root,
                                         transport=httpx.MockTransport(handler)))
        check(items[0].plan_name.startswith("Kimi"),
              "a 401 rotates the access token via RefreshToken and still renders")
        check(refresh_hits["n"] == 1 and stats_hits["n"] == 2,
              "one refresh, then one retry")
        saved = _load_token("kimi-web", root)
        check(saved["authorization"] == "Bearer fresh-access-1"
              and saved["refresh_token"] == "fresh-refresh-1",
              "the refreshed pair is persisted (the refresh token rotates)")


def test_registry() -> None:
    print("registry")
    for provider_type, cls_name in (("kimi", "KimiProvider"), ("deepseek", "DeepSeekProvider"),
                                    ("aliyun", "AliyunProvider"), ("zhipu", None),
                                    ("openai", "OpenAIProvider"), ("generic", None)):
        provider = create({"type": provider_type, "name": provider_type})
        check(provider is not None, f"'{provider_type}' resolves to a provider")

    try:
        create({"type": "aliyun", "name": "Aliyun"}).api_key
    except ProviderError:
        check(True, "an aliyun provider without AccessKeys fails with a config error")
    else:
        check(False, "an aliyun provider without AccessKeys fails with a config error")

    item = SubscriptionItem("n", 0, 0, 0, "u")
    check(item.plan_name == "n", "long names are no longer clamped to 15 bytes")


def test_webquota_parsers() -> None:
    print("web-quota parsers (captured 2026-09-01)")
    from providers.webquota import parse_aliyun, parse_deepseek, parse_kimi

    ds_summary = {"code": 0, "data": {"biz_data": {
        "normal_wallets": [{"currency": "CNY", "balance": "59.1114450400000000"}],
        "total_costs": [{"currency": "CNY", "amount": "271.1589545600000000"}]}}}
    ds_cost = {"code": 0, "data": {"biz_data": {"data": [{"series": [
        {"model": "deepseek-v4-flash", "buckets": [
            {"time": 1788100000, "cost": "1.5"},
            {"time": 1788278400, "cost": "0.5"}]}]}]}}}
    ds_amount = {"code": 0, "data": {"biz_data": {"series": [
        {"model": "deepseek-v4-flash", "buckets": [
            {"time": 1788100000,
             "usage": {"RESPONSE_TOKEN": 60_000_000, "PROMPT_CACHE_HIT_TOKEN": 40_000_000}},
            {"time": 1788278400, "usage": {"RESPONSE_TOKEN": 20_000_000}}]},
        {"model": "deepseek-v4-pro", "buckets": [
            {"time": 1788278400, "usage": {"RESPONSE_TOKEN": 10_000_000}}]}]}}}
    noon = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    ds = parse_deepseek({
        "users/get_user_summary": [{"code": 0, "data": None}, ds_summary],
        "usage/by_api_key/cost": [ds_cost],
        "usage/by_api_key/amount": [ds_amount],
    }, now=noon)
    check(ds[0].balance == 5911, "DeepSeek balance parses to cents")
    check(ds[0].extra == "/ ¥271", "lifetime spend rides the metrics line, ¥ on both")
    for part in ("tdy ¥0.50", "tok 30.0M", "F67%/P33%"):
        check(part in ds[0].note, f"note carries {part!r}")

    kimi_stats = {"ratelimitCode5h": {"ratio": 0.0681, "enabled": True,
                                      "resetTime": "2026-09-01T10:22:33Z"},
                  "ratelimitCode7d": {"ratio": 0.3922, "enabled": True,
                                      "resetTime": "2026-09-05T01:22:33Z"},
                  "subscriptionBalance": {"feature": "FEATURE_OMNI",
                                          "amountUsedRatio": 0.277,
                                          "kimiCodeUsedRatio": 0.2466,
                                          "expireTime": "2026-09-25T01:22:33.648851Z"}}
    kimi = parse_kimi({
        "MembershipService/GetSubscription": [
            {"subscription": {"goods": {"title": "Allegretto"}},
             "balances": [{"feature": "FEATURE_OMNI", "amountUsedRatio": 0.2743,
                           "expireTime": "2026-09-25T01:22:33.648851Z"}]},
            kimi_stats,
        ],
        "MembershipService/GetSubscriptionStats": [kimi_stats],
    })
    check(kimi[0].plan_name == "Kimi Allegretto", "the plan title lands in the name")
    check(kimi[0].quota_used == 7,
          "the bar is the 5-hour usage share (6.8% used)")
    check(kimi[0].extra == "rst 18:22",
          "the 5h reset time shares the metrics line (local time)")
    check(kimi[0].bar_text == "", "the bar runs full width, no text at its right")
    for part in ("Mo 28%", "Wk 39%", "7d rst 09-05", "exp 09-25"):
        check(part in kimi[0].note, f"note carries {part!r}")

    fresh = {**kimi_stats,
             "ratelimitCode5h": {"enabled": True,
                                 "resetTime": "2026-09-01T15:22:33Z"}}
    kimi_fresh = parse_kimi({
        "MembershipService/GetSubscription": [kimi_stats],
        "MembershipService/GetSubscriptionStats": [fresh],
    })
    check(kimi_fresh[0].quota_used == 0,
          "a fresh 5h window (no ratio) reads as 0% used")
    check(kimi_fresh[0].extra == "rst 23:22",
          "the fresh window still shows its reset time")

    aliyun = parse_aliyun({
        "tokenplan/personal/api/v2/usage": [
            {"code": "200", "data": {"DataV2": {"data": {
                "code": "SUCCESS", "data": {"per1WeekResetTime": 1788747060000,
                                            "per1WeekPercentage": 0.009919287124999999}}}}}],
        "tokenplan/personal/api/v2/subscription": [
            {"code": "200", "data": {"DataV2": {"data": {
                "code": "SUCCESS", "data": {"specCode": "pro", "remainingDays": 20}}}}}],
    })
    check(aliyun[0].quota_used == 1, "the bar is usage (0.99% used), not remaining")
    check("left" not in aliyun[0].note, "no remaining share anywhere on the card")
    for part in ("rst 09-07 10:11", "20d"):
        check(part in aliyun[0].note, f"note carries {part!r}")


def main() -> int:
    for test in (test_aliyun_signing, test_aliyun_parsing, test_webquota_parsers,
                 test_bailian_gateway, test_bailian_parsing,
                 test_bailian_login_url, test_bailian_callback_parsing,
                 test_bailian_store, test_kimi_token,
                 test_deepseek_token, test_token_auto_refresh,
                 test_kimi_refresh_flow, test_registry):
        test()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
