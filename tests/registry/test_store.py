import pytest

from veena.errors import RevisionConflictError
from veena.registry import AssetRequested, Event, MemoryEventStore, SqliteEventStore


def _event(asset_id: str = "customer_spend_30d") -> Event:
    return Event(
        actor="app:sales",
        authority="request:app:sales",
        payload=AssetRequested(asset_id=asset_id, consumer="app:sales", request_text="30d"),
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryEventStore()
    return SqliteEventStore(tmp_path / "registry.sqlite")


def test_append_assigns_increasing_seq(store):
    assert store.revision() == 0
    assert store.append(_event()) == 1
    assert store.append(_event("b")) == 2
    assert store.revision() == 2
    assert [s.seq for s in store.read()] == [1, 2]
    assert [s.seq for s in store.read(after=1)] == [2]


def test_expected_revision_guards_append(store):
    store.append(_event())
    with pytest.raises(RevisionConflictError) as info:
        store.append(_event("b"), expected_revision=0)
    assert info.value.expected == 0
    assert info.value.actual == 1
    assert store.revision() == 1
    assert store.append(_event("b"), expected_revision=1) == 2


def test_sqlite_store_persists_across_connections(tmp_path):
    path = tmp_path / "registry.sqlite"
    first = SqliteEventStore(path)
    event = _event()
    first.append(event)
    first.close()
    second = SqliteEventStore(path)
    stored = list(second.read())
    assert len(stored) == 1
    assert stored[0].event == event
