"""Aliyun resource-package (资源包) provider, via BSS OpenAPI.

API reference: QueryResourcePackageInstances, BssOpenApi 2017-12-14
Endpoint: GET https://bssopenapi.aliyuncs.com/?Action=QueryResourcePackageInstances
Auth: ACS3-HMAC-SHA256 request signing with an AccessKey pair.
RAM permission: bss:DescribeInstances (get). Use a read-only RAM key: this pair
can read your billing, so do not reuse your primary account key.

The response reports TotalAmount and RemainingAmount but no "used" field, so
used is derived by subtraction. Token packages report amounts per their own unit
("万Token", "M token", …); those are normalised to plain tokens so the panel can
group the digits.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register

_ENDPOINT = "https://bssopenapi.aliyuncs.com"
_HOST = "bssopenapi.aliyuncs.com"
_VERSION = "2017-12-14"
_ACTION = "QueryResourcePackageInstances"

# Units token packages report, normalised to plain tokens.  Anything not in
# this table is passed through untouched, so a new unit degrades to a raw
# number rather than a wrong one.
_UNIT_SCALE = {
    "万token": 10_000,
    "万tokens": 10_000,
    "k token": 1_000,
    "m token": 1_000_000,
    "mtokens": 1_000_000,
    "百万token": 1_000_000,
    "亿token": 100_000_000,
}


def _percent_encode(value: str) -> str:
    """RFC3986 unreserved characters only - Aliyun rejects the '+' and '%20'
    forms urllib's defaults would produce."""
    return urllib.parse.quote(value, safe="-_.~")


def canonical_query(params: dict[str, str]) -> str:
    """Sort by name, percent-encode both sides, join with &."""
    return "&".join(
        f"{_percent_encode(name)}={_percent_encode(value)}"
        for name, value in sorted(params.items())
    )


def signed_headers(params: dict[str, str], access_key_id: str, access_key_secret: str,
                   now: datetime | None = None, nonce: str | None = None) -> dict[str, str]:
    """Build the headers for one ACS3-HMAC-SHA256 signed RPC call.

    Kept pure (the timestamp and nonce are parameters) so the signature can be
    asserted offline against known inputs.
    """
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = nonce or uuid.uuid4().hex
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_headers = "\n".join([
        f"host:{_HOST}",
        f"x-acs-action:{params['Action']}",
        f"x-acs-content-sha256:{payload_hash}",
        f"x-acs-date:{timestamp}",
        f"x-acs-signature-nonce:{nonce}",
        f"x-acs-version:{params['Version']}",
    ])
    signed = ";".join(canonical_headers.split("\n")).replace(":", ":")

    canonical_request = "\n".join([
        "GET", "/", canonical_query(params), canonical_headers, signed, payload_hash,
    ])
    to_sign = f"ACS3-HMAC-SHA256\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    signature = hmac.new(access_key_secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "host": _HOST,
        "x-acs-action": params["Action"],
        "x-acs-version": params["Version"],
        "x-acs-date": timestamp,
        "x-acs-signature-nonce": nonce,
        "x-acs-content-sha256": payload_hash,
        "Authorization": f"ACS3-HMAC-SHA256 Credential={access_key_id},Signature={signature}",
    }


def _normalise(amount: float, unit: str) -> tuple[int, str]:
    scale = _UNIT_SCALE.get(unit.strip().lower())
    if scale is None:
        return int(round(amount)), unit
    return int(round(amount * scale)), "token"


def parse_packages(data: dict[str, Any], keyword: str) -> tuple[list[SubscriptionItem], list[str]]:
    """Turn a QueryResourcePackageInstances response into items.

    Returns (items, labels seen) so a caller can report what the account owns
    when the keyword filters everything out.
    """
    instances = (data.get("Data") or {}).get("Instances") or {}
    entries = instances.get("Instance") or []
    if isinstance(entries, dict):          # a single instance comes back unwrapped
        entries = [entries]

    keyword = keyword.lower()
    items: list[SubscriptionItem] = []
    seen: list[str] = []
    for entry in entries:
        if entry.get("Status") != "Available":
            continue
        label = entry.get("Remark") or entry.get("PackageType") or "resource package"
        seen.append(label)
        if keyword and keyword not in label.lower():
            continue

        total, unit = _normalise(float(entry.get("TotalAmount", 0)),
                                 entry.get("TotalAmountUnit", ""))
        remaining, _unit = _normalise(float(entry.get("RemainingAmount", 0)),
                                      entry.get("RemainingAmountUnit", ""))
        items.append(SubscriptionItem(
            plan_name=label,
            quota_total=total,
            quota_used=max(total - remaining, 0),
            balance=0,
            unit=unit,
        ))
    return items, seen


@register
class AliyunProvider(ProviderBase):
    provider_type = "aliyun"

    async def fetch(self) -> list[SubscriptionItem]:
        key_id = str(self._cfg.get("access_key_id", "")).strip()
        key_secret = str(self._cfg.get("access_key_secret", "")).strip()
        if not key_id or not key_secret:
            raise ProviderError(
                f"[{self.name}] access_key_id / access_key_secret are not configured. "
                "Aliyun bills by AccessKey, not by an api_key."
            )

        params = {
            "Action": _ACTION,
            "Version": _VERSION,
            "Format": "JSON",
            "PageNum": "1",
            "PageSize": "300",
        }
        headers = signed_headers(params, key_id, key_secret)
        url = f"{_ENDPOINT}/?{canonical_query(params)}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data: dict[str, Any] = resp.json()
        if data.get("Code"):
            raise ProviderError(f"[{self.name}] API error {data['Code']}: "
                                f"{data.get('Message', '')}")

        items, seen = parse_packages(data, str(self._cfg.get("keyword", "")))
        if not items:
            hint = f" matching '{self._cfg.get('keyword', '')}'" \
                if self._cfg.get("keyword") else ""
            raise ProviderError(
                f"[{self.name}] no Available resource package{hint}. "
                f"Seen: {seen or 'none'}"
            )
        return items
