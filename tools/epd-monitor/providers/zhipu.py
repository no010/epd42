"""Zhipu AI (智谱AI / BigModel) provider.

Zhipu exposes two distinct data sources depending on plan type:

1. Coding-plan / subscription quota (token window percentage + call count)
   GET /api/monitor/usage/quota/limit
   Returns: TOKENS_LIMIT.percentage, TIME_LIMIT.used/total, level

2. Cash balance — NO public REST API; dashboard only.

This provider fetches the quota-limit endpoint (works for subscription
accounts). For PAYG-only accounts set ``mode = "balance_only"`` and
provide a ``balance`` field directly in config as a fallback placeholder.
"""
from __future__ import annotations

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register


@register
class ZhipuProvider(ProviderBase):
    provider_type = "zhipu"

    _DEFAULT_BASE = "https://open.bigmodel.cn"

    async def fetch(self) -> list[SubscriptionItem]:
        base = self._cfg.get("base_url", self._DEFAULT_BASE).rstrip("/")
        url = f"{base}/api/monitor/usage/quota/limit"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()

        items: list[SubscriptionItem] = []

        # Token sliding-window quota (percentage used)
        tokens = data.get("TOKENS_LIMIT", {})
        if tokens:
            pct = int(tokens.get("percentage", 0))
            items.append(
                SubscriptionItem(
                    plan_name=self._truncate(f"{self.name} Tok", 15),
                    quota_total=100,
                    quota_used=pct,
                    balance=0,
                    unit="%",
                )
            )

        # Tool-call quota (TIME_LIMIT = MCP / search calls)
        time_limit = data.get("TIME_LIMIT", {})
        if time_limit:
            used = int(time_limit.get("used", 0))
            total = int(time_limit.get("total", 0))
            items.append(
                SubscriptionItem(
                    plan_name=self._truncate(f"{self.name} Req", 15),
                    quota_total=total,
                    quota_used=used,
                    balance=0,
                    unit="req",
                )
            )

        if not items:
            raise ProviderError(
                f"[{self.name}] Unexpected response format: {data}"
            )
        return items[:2]  # at most 2 items to conserve EPD slots
