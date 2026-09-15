"""Stable typed interface consumed by workflows."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Company(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    name: str


class CapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edition: str
    version: int
    transport: str
    modules: dict[str, bool]


class OdooAdapter(Protocol):
    """Odoo-only adapter protocol; transport details stay in its implementation."""

    async def get_capabilities(self) -> CapabilitySnapshot: ...

    async def get_companies(self) -> list[Company]: ...
