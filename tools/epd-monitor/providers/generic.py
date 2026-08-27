"""Generic HTTP provider — fully config-driven.

Supports any provider whose usage or balance can be retrieved with a
single authenticated GET request returning JSON.

Example config:

    [[providers]]
    type        = "generic"
    name        = "Claude"
    url         = "https://api.anthropic.com/v1/credits"
    auth_header = "x-api-key"        # header used for auth
    api_key     = "sk-ant-..."
    # JSONPath-like dotted key into the response object
    balance_field      = "credits_remaining"   # float/string → balance
    balance_scale      = 100                   # multiply to get integer cents
    quota_total_field  = ""                    # optional
    quota_used_field   = ""                    # optional
    unit               = "USD"

Nested fields use dot notation, e.g. ``data.balance``.
"""
from __future__ import annotations

from typing import Any

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register


def _get_nested(obj: Any, dotted_key: str) -> Any:
    """Traverse a nested dict/list using dotted-key notation."""
    if not dotted_key:
        return None
    for part in dotted_key.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if obj is None:
            return None
    return obj


@register
class GenericProvider(ProviderBase):
    provider_type = "generic"

    async def fetch(self) -> list[SubscriptionItem]:
        url = self._cfg.get("url", "")
        if not url:
            raise ProviderError(f"[{self.name}] 'url' is required")

        auth_header = self._cfg.get("auth_header", "Authorization")
        auth_prefix = self._cfg.get("auth_prefix", "Bearer")
        headers: dict[str, str] = {}
        if auth_prefix:
            headers[auth_header] = f"{auth_prefix} {self.api_key}"
        else:
            headers[auth_header] = self.api_key

        # Optional extra headers (dict in config)
        for k, v in self._cfg.get("extra_headers", {}).items():
            headers[k] = str(v)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()

        balance_field = self._cfg.get("balance_field", "")
        balance_scale = float(self._cfg.get("balance_scale", 100))
        quota_total_field = self._cfg.get("quota_total_field", "")
        quota_used_field = self._cfg.get("quota_used_field", "")
        unit = str(self._cfg.get("unit", "CNY"))[:3]

        raw_balance = _get_nested(data, balance_field)
        balance_cents = round(float(raw_balance or 0) * balance_scale)

        raw_total = _get_nested(data, quota_total_field)
        raw_used = _get_nested(data, quota_used_field)
        quota_total = int(float(raw_total or 0))
        quota_used = int(float(raw_used or 0))

        return [
            SubscriptionItem(
                plan_name=self._truncate(self.name, 15),
                quota_total=quota_total,
                quota_used=quota_used,
                balance=balance_cents,
                unit=unit,
            )
        ]
