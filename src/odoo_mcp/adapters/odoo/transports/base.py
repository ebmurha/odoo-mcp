"""Internal protocol shared by Odoo transport implementations."""

from __future__ import annotations

from typing import Any, Protocol


class OdooTransport(Protocol):
    name: str

    async def authenticate(self) -> None: ...

    async def probe_model(self, model: str, *, company_ids: tuple[int, ...]) -> bool: ...

    async def search_count(self, model: str, domain: list[Any]) -> int: ...

    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...
