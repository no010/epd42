"""Provider registry and abstract base for AI usage/balance fetching."""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass
class SubscriptionItem:
    """One item to show on the EPD screen.

    Width is the renderer's problem: anything too long is clipped and marked
    there, so names and units arrive as the provider reported them.
    """

    plan_name: str
    quota_total: int    # Total quota (tokens, requests, …); 0 if N/A
    quota_used: int     # Used quota; 0 if N/A
    balance: int        # Balance × 100 (integer cents/fen); 0 if N/A
    unit: str           # "req", "tkn", "CNY", "%", …
    note: str = ""      # extra context for the usage line (expiry, lifetime spend, …)


class ProviderBase(ABC):
    """Abstract base class every provider must implement."""

    #: Registry name — must match the ``type`` value in config.
    provider_type: ClassVar[str]

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self.name: str = cfg.get("name", self.provider_type)

    @abstractmethod
    async def fetch(self) -> list[SubscriptionItem]:
        """Fetch current usage / balance.

        Returns a list of :class:`SubscriptionItem` (usually 1–2 items).
        Raise :class:`ProviderError` on unrecoverable failure.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        key = str(self._cfg.get("api_key", "")).strip()
        if not key:
            raise ProviderError(f"[{self.name}] api_key is not configured")
        return key

    @staticmethod
    def _truncate(s: str, max_bytes: int) -> str:
        encoded = s.encode("utf-8")
        if len(encoded) <= max_bytes:
            return s
        return encoded[:max_bytes].decode("utf-8", errors="ignore")


class ProviderError(Exception):
    """Raised when a provider cannot fetch data."""


# ------------------------------------------------------------------
# Provider registry
# ------------------------------------------------------------------
_REGISTRY: dict[str, type[ProviderBase]] = {}

_BUILTIN_MODULES = {
    "kimi": "providers.kimi",
    "deepseek": "providers.deepseek",
    "zhipu": "providers.zhipu",
    "openai": "providers.openai_provider",
    "aliyun": "providers.aliyun",
    "generic": "providers.generic",
    "deepseek-web": "providers.webquota",
    "kimi-web": "providers.webquota",
    "aliyun-web": "providers.webquota",
}


def _ensure_loaded(provider_type: str) -> None:
    if provider_type not in _REGISTRY and provider_type in _BUILTIN_MODULES:
        importlib.import_module(_BUILTIN_MODULES[provider_type])


def register(cls: type[ProviderBase]) -> type[ProviderBase]:
    """Decorator: register a provider class."""
    _REGISTRY[cls.provider_type] = cls
    return cls


def create(cfg: dict) -> ProviderBase:
    """Instantiate a provider from a config dict."""
    provider_type = cfg.get("type", "")
    _ensure_loaded(provider_type)
    cls = _REGISTRY.get(provider_type)
    if cls is None:
        raise ProviderError(
            f"Unknown provider type '{provider_type}'. "
            f"Available: {sorted(_REGISTRY)}"
        )
    return cls(cfg)
