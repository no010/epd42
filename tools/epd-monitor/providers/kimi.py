"""Kimi / Moonshot AI provider.

API reference: https://platform.kimi.ai/docs/api/balance
Endpoint: GET /v1/users/me/balance
"""
from __future__ import annotations

import httpx

from providers import ProviderBase, ProviderError, SubscriptionItem, register


@register
class KimiProvider(ProviderBase):
    provider_type = "kimi"

    # China vs international host
    _DEFAULT_BASE = "https://api.moonshot.cn"

    async def fetch(self) -> list[SubscriptionItem]:
        base = self._cfg.get("base_url", self._DEFAULT_BASE).rstrip("/")
        url = f"{base}/v1/users/me/balance"
        headers = {
            "Authorization": f"******",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if not data.get("status"):
            raise ProviderError(f"[{self.name}] API error: {data}")

        bal_data = data.get("data", {})
        # available_balance is cash + voucher combined
        avail = float(bal_data.get("available_balance", 0))
        balance_fen = round(avail * 100)  # store as integer ×100

        return [
            SubscriptionItem(
                plan_name=self._truncate(self.name, 15),
                quota_total=0,
                quota_used=0,
                balance=balance_fen,
                unit="CNY",
            )
        ]
