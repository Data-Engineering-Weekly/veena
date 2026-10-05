import os

import pytest

from veena.errors import RevisionConflictError
from veena.registry import AssetRequested, Event
from veena.registry.postgres import PostgresEventStore


class FakeCursor:
    """Enough of a psycopg cursor to exercise the store's SQL flow in memory."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split()).lower()
        if text.startswith("create table"):
            return
        if text.startswith("select coalesce(max(seq), 0)"):
            self.rows = [(len(self.conn.events),)]
        elif text.startswith("insert into veena_events"):
            self.conn.events.append((len(self.conn.events) + 1, params[3]))
            self.rows = [(len(self.conn.events),)]
        elif text.startswith("select seq, body"):
            after = params[0]
            self.rows = [(seq, body) for seq, body in self.conn.events if seq > after]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self):
        self.events = []
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass

    def close(self):
        pass


def _event(asset_id="a"):
    return Event(
        actor="x",
        authority="y",
        payload=AssetRequested(asset_id=asset_id, consumer="c", request_text="t"),
    )


def test_store_flow_with_a_fake_connection():
    store = PostgresEventStore("postgresql://fake", connect=lambda dsn: FakeConnection())
    assert store.revision() == 0
    assert store.append(_event()) == 1
    assert store.append(_event("b"), expected_revision=1) == 2
    with pytest.raises(RevisionConflictError):
        store.append(_event("c"), expected_revision=0)
    read = list(store.read(after=1))
    assert len(read) == 1 and read[0].seq == 2
    assert getattr(read[0].event.payload, "asset_id", None) == "b"


@pytest.mark.skipif(
    not os.environ.get("VEENA_POSTGRES_DSN"), reason="needs a live Postgres via VEENA_POSTGRES_DSN"
)
def test_live_postgres_roundtrip():
    store = PostgresEventStore(os.environ["VEENA_POSTGRES_DSN"])
    before = store.revision()
    seq = store.append(_event(f"live_{before}"))
    assert seq == before + 1
    assert any(s.seq == seq for s in store.read(after=before))
