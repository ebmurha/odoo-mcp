"""Backend-neutral transaction boundary used by every repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

import psycopg
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Row = Mapping[str, Any]
Connection = Any
DATABASE_ERRORS = (sqlite3.Error, PsycopgError)
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


class Database(Protocol):
    """Small seam consumed by the existing repositories and services."""

    dialect: str

    def transaction(self, *, write: bool = False) -> Any: ...

    def migration_transaction(self) -> Any: ...

    def lock(self, connection: Connection, key: str) -> None: ...

    def close(self) -> None: ...


class SQLiteDatabase:
    """Open short-lived SQLite connections with explicit transactions."""

    dialect = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migration_transaction(self) -> Any:
        return self.transaction(write=True)

    def lock(self, connection: Connection, key: str) -> None:
        del connection, key

    def close(self) -> None:
        """SQLite owns no connection pool."""


class _PostgresCursor:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> Row | None:
        return cast(Row | None, self._cursor.fetchone())

    def fetchall(self) -> list[Row]:
        return cast(list[Row], self._cursor.fetchall())

    def __iter__(self) -> Any:
        return iter(self._cursor)


class _PostgresConnection:
    """Preserve existing qmark SQL while using psycopg safely."""

    _RETURNING_ID_TABLES = frozenset({"audit_log", "proposals", "artifacts"})

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> _PostgresCursor:
        translated = query.replace("?", "%s")
        words = translated.lstrip().split()
        returning_id = (
            len(words) >= 3
            and words[0].upper() == "INSERT"
            and words[1].upper() == "INTO"
            and words[2].strip('"').casefold() in self._RETURNING_ID_TABLES
            and " RETURNING " not in f" {translated.upper()} "
        )
        if returning_id:
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
        cursor = self._raw.execute(translated, parameters)
        if returning_id:
            row = cursor.fetchone()
            return _PostgresCursor(cursor, lastrowid=None if row is None else int(row["id"]))
        return _PostgresCursor(cursor)

    def executemany(
        self,
        query: str,
        parameter_sets: Sequence[Sequence[object]],
    ) -> _PostgresCursor:
        cursor = self._raw.cursor()
        cursor.executemany(query.replace("?", "%s"), parameter_sets)
        return _PostgresCursor(cursor)


class PostgresDatabase:
    """Pooled runtime PostgreSQL plus a direct migration connection."""

    dialect = "postgresql"

    def __init__(
        self,
        pooled_url: str,
        migration_url: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
    ) -> None:
        self._migration_url = migration_url
        self._pool = ConnectionPool(
            conninfo=pooled_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._pool.wait(timeout=10.0)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[_PostgresConnection]:
        del write
        with self._pool.connection() as raw:
            with raw.transaction():
                yield _PostgresConnection(raw)

    @contextmanager
    def migration_transaction(self) -> Iterator[_PostgresConnection]:
        with psycopg.connect(self._migration_url, row_factory=dict_row) as raw:
            with raw.transaction():
                yield _PostgresConnection(raw)

    def lock(self, connection: Connection, key: str) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (key,))

    def close(self) -> None:
        self._pool.close()
