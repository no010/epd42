#!/usr/bin/env python3
"""Offline tests for the provider layer - no network, no API keys.

    python test_providers.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
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

    ds_good = {"code": 0, "msg": "", "data": {"biz_code": 0, "biz_msg": "",
        "biz_data": {
            "normal_wallets": [{"currency": "CNY", "balance": "59.1114450400000000",
                                "token_estimation": "0"}],
            "bonus_wallets": [{"currency": "CNY", "balance": "0", "token_estimation": "0"}],
            "total_costs": [{"currency": "CNY", "amount": "271.1589545600000000"}],
        }}}
    ds = parse_deepseek({"users/get_user_summary": [{"code": 0, "data": None}, ds_good]})
    check(ds[0].balance == 5911 and ds[0].unit == "CNY",
          "DeepSeek skips an empty pre-auth body and parses the real wallet")
    check(ds[0].note == "累计 ¥271.16", "and carries the lifetime spend as a note")

    kimi = parse_kimi({"MembershipService/GetSubscription": [
        {"subscription": {"goods": {"title": "Allegretto"}},
         "balances": [{"feature": "FEATURE_OMNI", "unit": "UNIT_CREDIT",
                       "amountUsedRatio": 0.2743,
                       "expireTime": "2026-09-25T01:22:33.648851Z"}]},
        {"ratelimitCode5h": {"enabled": True, "resetTime": "2026-09-01T10:22:33Z"},
         "ratelimitCode7d": {"ratio": 0.3785, "enabled": True,
                             "resetTime": "2026-09-05T01:22:33Z"},
         "subscriptionBalance": {"feature": "FEATURE_OMNI",
                                 "amountUsedRatio": 0.2743,
                                 "kimiCodeUsedRatio": 0.2466,
                                 "expireTime": "2026-09-25T01:22:33.648851Z"}},
    ]})
    check(kimi[0].plan_name == "Kimi Allegretto", "the plan title lands in the name")
    check((kimi[0].quota_total, kimi[0].quota_used, kimi[0].unit) == (1000, 378, "%"),
          "the bar is the 7-day coding window (37.8%)")
    check("credits 27.4%" in kimi[0].note and "09-25 到期" in kimi[0].note
          and "7天重置 09-05" in kimi[0].note,
          f"credits, expiry and window reset land in the note: {kimi[0].note!r}")

    aliyun = parse_aliyun({
        "tokenplan/personal/api/v2/usage": [
            {"code": "200", "data": {"DataV2": {"data": {"code": "SUCCESS", "data": {}}}}},
            {"code": "200", "data": {"DataV2": {"data": {
                "code": "SUCCESS", "data": {"per1WeekResetTime": 1788747060000,
                                            "per1WeekPercentage": 0.009919287124999999}}}}}],
        "tokenplan/personal/api/v2/subscription": [
            {"code": "200", "data": {"DataV2": {"data": {
                "code": "SUCCESS", "data": {"instanceCode": "sfm_tokenplansolo_public_cn-ofv4xapet04",
                                            "specCode": "pro", "remainingDays": 20,
                                            "status": "VALID"}}}}}],
    })
    check((aliyun[0].quota_total, aliyun[0].quota_used, aliyun[0].unit) == (1000, 10, "%"),
          "the weekly percentage becomes the bar at 0.1% resolution")
    check(aliyun[0].note == "pro·剩20天", "the tier and remaining days become a note")


def main() -> int:
    for test in (test_aliyun_signing, test_aliyun_parsing, test_webquota_parsers,
                 test_registry):
        test()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
