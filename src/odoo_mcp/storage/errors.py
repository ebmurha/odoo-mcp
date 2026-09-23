"""Safe storage failures without database or secret detail."""


class StorageError(RuntimeError):
    """Base class for server-owned persistence failures."""


class MigrationError(StorageError):
    """The ordered schema could not be verified or migrated."""


class AuditIntegrityError(StorageError):
    """A tenant audit chain did not verify."""


class ProposalTransitionError(StorageError):
    """A proposal state transition was invalid or lost a race."""


class IdempotencyPayloadMismatch(StorageError):
    """An idempotency key was reused for a different request."""


class IdempotencyTransitionError(StorageError):
    """An idempotency outcome could not be recorded safely."""


class ConnectionDecryptionError(StorageError):
    """An encrypted connection could not be safely resolved."""


class StorageCorruptionError(StorageError):
    """A database or restore candidate failed integrity validation."""
