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


def main() -> int:
    for test in (test_aliyun_signing, test_aliyun_parsing, test_registry):
        test()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
