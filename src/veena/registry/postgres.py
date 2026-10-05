"""A Postgres event store for workspaces with more than one writer.

Needs the optional `psycopg` dependency (`uv sync --group postgres`). The revision check runs
inside a serializable transaction, so two writers racing on the same revision cannot both win.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veena.errors import RegistryError, RevisionConflictError
from veena.registry.events import Event
from veena.registry.store import StoredEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class PostgresEventStore:
    """Append-only events in one Postgres table."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS veena_events (
        seq         BIGSERIAL PRIMARY KEY,
        event_id    TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        body        JSONB NOT NULL
    );
    """

    def __init__(self, dsn: str, *, connect: Callable[[str], Any] | None = None) -> None:
        """Connect with psycopg, or with `connect(dsn)` for tests.

        Raises:
            RegistryError: psycopg is not installed.
        """
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                msg = "PostgresEventStore needs psycopg; run `uv sync --group postgres`"
                raise RegistryError(msg) from exc
            connect = psycopg.connect
        self._conn = connect(dsn)
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(self._SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def revision(self) -> int:
        """The `seq` of the newest event, or 0 when empty."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM veena_events")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Append one event under a serializable transaction.

        Raises:
            RevisionConflictError: The store moved past `expected_revision`, or a concurrent
                writer committed first.
        """
        self._conn.autocommit = False
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
                cur.execute("SELECT COALESCE(MAX(seq), 0) FROM veena_events")
                row = cur.fetchone()
                current = int(row[0]) if row else 0
                if expected_revision is not None and expected_revision != current:
                    cur.execute("ROLLBACK")
                    raise RevisionConflictError(expected_revision, current)
                cur.execute(
                    "INSERT INTO veena_events (event_id, kind, recorded_at, body) "
                    "VALUES (%s, %s, %s, %s::jsonb) RETURNING seq",
                    (event.event_id, str(event.kind), event.recorded_at, event.model_dump_json()),
                )
                inserted = cur.fetchone()
                cur.execute("COMMIT")
        except RevisionConflictError:
            raise
        except Exception as exc:
            try:
                self._conn.rollback()
            finally:
                self._conn.autocommit = True
            # A serialization failure means another writer won; anything else is a real error.
            if "serializ" in str(exc).lower() or type(exc).__name__ == "SerializationFailure":
                raise RevisionConflictError(
                    expected_revision if expected_revision is not None else -1, self.revision()
                ) from exc
            msg = f"cannot append event {event.event_id}: {exc}"
            raise RegistryError(msg) from exc
        finally:
            self._conn.autocommit = True
        return int(inserted[0]) if inserted else 0

    def read(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Yield events with `seq` greater than `after`, in order."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT seq, body FROM veena_events WHERE seq > %s ORDER BY seq", (after,))
            rows = cur.fetchall()
        for seq, body in rows:
            text = body if isinstance(body, str) else __import__("json").dumps(body)
            yield StoredEvent(seq=int(seq), event=Event.model_validate_json(text))
