"""OpenAI / ChatGPT provider.

API reference: https://platform.openai.com/docs/api-reference/usage
Endpoint: GET /v1/organization/costs   (requires Admin API key)

Note: Requires an Admin API key (sk-admin-...) with api.usage.read scope.
Regular project keys do not have permission to query usage/billing.

This provider fetches the past N days of cost data and sums it to produce
a "used" figure. The "total" is derived from the configured budget, if any.
"""
from __future__ import annotations

import time

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register


@register
class OpenAIProvider(ProviderBase):
    provider_type = "openai"

    _DEFAULT_BASE = "https://api.openai.com"

    async def fetch(self) -> list[SubscriptionItem]:
        base = self._cfg.get("base_url", self._DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"******",
            "Accept": "application/json",
        }

        # Fetch cost for the current calendar month
        now = int(time.time())
        # Start of current month (approximate: now - 30 days)
        month_start = now - 30 * 86400

        params: dict[str, str | int] = {
            "start_time": month_start,
            "bucket_width": "1d",
            "limit": 31,
        }

        url = f"{base}/v1/organization/costs"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code == 403:
            raise ProviderError(
                f"[{self.name}] Permission denied. "
                "Ensure you are using an Admin API key (sk-admin-...) with "
                "api.usage.read scope. See: platform.openai.com/settings/"
                "organization/api-keys"
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        total_cost_usd = 0.0
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                total_cost_usd += float(result.get("cost", 0))

        # Optional monthly budget from config (for progress bar)
        budget_usd: float = float(self._cfg.get("monthly_budget_usd", 0))
        used_cents = round(total_cost_usd * 100)
        total_cents = round(budget_usd * 100)

        return [
            SubscriptionItem(
                plan_name=self._truncate(self.name, 15),
                quota_total=total_cents,
                quota_used=used_cents,
                balance=0,
                unit="USD",
            )
        ]
