"""Shared authorization and write-safety policy."""

from odoo_mcp.policy.write_safety import (
    AppliedWrite,
    KnownWriteFailure,
    PreparedWrite,
    UnknownWriteOutcome,
    WriteCommand,
    WriteSafetyCoordinator,
    WriteSafetyResponse,
    validate_write_tool_definition,
)

__all__ = [
    "AppliedWrite",
    "KnownWriteFailure",
    "PreparedWrite",
    "UnknownWriteOutcome",
    "WriteCommand",
    "WriteSafetyCoordinator",
    "WriteSafetyResponse",
    "validate_write_tool_definition",
]
