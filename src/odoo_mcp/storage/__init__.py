"""Durable, tenant-scoped server state."""

from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage.connections import EncryptionKeyring
from odoo_mcp.storage.errors import (
    AuditIntegrityError,
    ConnectionDecryptionError,
    IdempotencyPayloadMismatch,
    IdempotencyTransitionError,
    MigrationError,
    ProposalTransitionError,
    StorageCorruptionError,
)
from odoo_mcp.storage.migrations import Migration
from odoo_mcp.storage.models import (
    AuditEvent,
    IdempotencyDisposition,
    IdempotencyState,
    ProposalState,
)
from odoo_mcp.storage.service import Storage

__all__ = [
    "AuditEvent",
    "AuditIntegrityError",
    "ConnectionDecryptionError",
    "EncryptionKeyring",
    "IdempotencyDisposition",
    "IdempotencyPayloadMismatch",
    "IdempotencyState",
    "IdempotencyTransitionError",
    "Migration",
    "MigrationError",
    "ProposalState",
    "ProposalTransitionError",
    "Storage",
    "StorageCorruptionError",
    "new_request_id",
]
