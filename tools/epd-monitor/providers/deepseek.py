"""DeepSeek provider.

API reference: https://api-docs.deepseek.com/api/get-user-balance/
Endpoint: GET /user/balance
"""
from __future__ import annotations

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register


@register
class DeepSeekProvider(ProviderBase):
    provider_type = "deepseek"

    _DEFAULT_BASE = "https://api.deepseek.com"

    async def fetch(self) -> list[SubscriptionItem]:
        base = self._cfg.get("base_url", self._DEFAULT_BASE).rstrip("/")
        url = f"{base}/user/balance"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if not data.get("is_available", True):
            # Balance is zero / exhausted; still show it
            pass

        items: list[SubscriptionItem] = []
        for info in data.get("balance_infos", []):
            currency = info.get("currency", "CNY")[:3]
            total = float(info.get("total_balance", 0))
            balance_cents = round(total * 100)
            items.append(
                SubscriptionItem(
                    plan_name=self.name,
                    quota_total=0,
                    quota_used=0,
                    balance=balance_cents,
                    unit=currency,
                )
            )
        if not items:
            raise ProviderError(f"[{self.name}] No balance_infos in response")
        return items[:1]  # one item per provider to conserve EPD slots
