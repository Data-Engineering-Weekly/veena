"""Append-only event stores. The registry holds nothing that is not in the store."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from veena.errors import RegistryError, RevisionConflictError
from veena.registry.events import Event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class StoredEvent(BaseModel):
    """An event plus the revision the store assigned to it."""

    model_config = ConfigDict(frozen=True)

    seq: int
    event: Event


class EventStore(Protocol):
    """Where events live. Implementations must assign strictly increasing `seq` values."""

    def revision(self) -> int:
        """The `seq` of the newest event, or 0 when empty."""
        ...

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Append one event and return its `seq`.

        Args:
            event: The event to store.
            expected_revision: If given, the append fails unless the store is at this revision.

        Raises:
            RevisionConflictError: The store moved past `expected_revision`.
        """
        ...

    def read(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Yield events with `seq` greater than `after`, in order."""
        ...


class MemoryEventStore:
    """An in-process store for tests and dry runs."""

    def __init__(self) -> None:
        """Start empty."""
        self._events: list[StoredEvent] = []

    def revision(self) -> int:
        """The `seq` of the newest event, or 0 when empty."""
        return len(self._events)

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Append one event and return its `seq`.

        Raises:
            RevisionConflictError: The store moved past `expected_revision`.
        """
        current = self.revision()
        if expected_revision is not None and expected_revision != current:
            raise RevisionConflictError(expected_revision, current)
        seq = current + 1
        self._events.append(StoredEvent(seq=seq, event=event))
        return seq

    def read(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Yield events with `seq` greater than `after`, in order."""
        yield from self._events[after:]


class SqliteEventStore:
    """A single-file store. One writer at a time; `BEGIN IMMEDIATE` serializes appends."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id    TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        body        TEXT NOT NULL
    );
    """

    def __init__(self, path: Path) -> None:
        """Open or create the store at `path`."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self._SCHEMA)
        except sqlite3.Error as exc:
            self._conn.close()
            msg = f"{path} is not a usable registry: {exc}"
            raise RegistryError(msg) from exc

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> SqliteEventStore:
        """Support `with SqliteEventStore(path) as store:`."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()

    def revision(self) -> int:
        """The `seq` of the newest event, or 0 when empty."""
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Append one event and return its `seq`.

        Raises:
            RevisionConflictError: The store moved past `expected_revision`.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            msg = f"cannot lock the registry: {exc}"
            raise RegistryError(msg) from exc
        try:
            current = self.revision()
            if expected_revision is not None and expected_revision != current:
                raise RevisionConflictError(expected_revision, current)
            cursor = self._conn.execute(
                "INSERT INTO events (event_id, kind, recorded_at, body) VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    str(event.kind),
                    event.recorded_at.isoformat(),
                    event.model_dump_json(),
                ),
            )
            self._conn.execute("COMMIT")
        except RevisionConflictError:
            self._conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            self._conn.execute("ROLLBACK")
            msg = f"cannot append event {event.event_id}: {exc}"
            raise RegistryError(msg) from exc
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return int(cursor.lastrowid or 0)

    def read(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Yield events with `seq` greater than `after`, in order."""
        rows = self._conn.execute(
            "SELECT seq, body FROM events WHERE seq > ? ORDER BY seq", (after,)
        )
        for seq, body in rows:
            yield StoredEvent(seq=int(seq), event=Event.model_validate_json(body))
