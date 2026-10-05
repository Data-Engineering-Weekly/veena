"""The registry facade: append events, read the projected state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veena.errors import RevisionConflictError
from veena.registry.state import RegistryState
from veena.registry.store import StoredEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from veena.registry.events import Event
    from veena.registry.store import EventStore

RETRIES = 3


class Registry:
    """Wraps an event store and keeps a projection current with it.

    Every append validates the event against the projection at the revision it will land on,
    and tells the store that revision. A concurrent writer therefore cannot slip an event in
    between validation and storage: the store refuses, the registry catches up, and it validates
    again. The store never holds an event the projection rejects.
    """

    def __init__(self, store: EventStore) -> None:
        """Replay whatever the store already holds."""
        self._store = store
        self._state = RegistryState()
        self._catch_up()

    @property
    def state(self) -> RegistryState:
        """The projection at the store's current revision."""
        self._catch_up()
        return self._state

    @property
    def store(self) -> EventStore:
        """The underlying store, for opening a second registry on the same log."""
        return self._store

    @property
    def revision(self) -> int:
        """The store's current revision."""
        return self._store.revision()

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Validate and store one event.

        Args:
            event: The event to record.
            expected_revision: If given, fail unless the store is at exactly this revision. When
                omitted, the registry still guards the append with its own revision and retries
                after catching up with other writers.

        Returns:
            The `seq` the store assigned.

        Raises:
            RegistryError: The projection rejects the event.
            RevisionConflictError: `expected_revision` was given and the store moved past it, or
                other writers kept winning for `RETRIES` attempts.
        """
        attempts = RETRIES if expected_revision is None else 1
        last: RevisionConflictError | None = None
        for _ in range(attempts):
            self._catch_up()
            if expected_revision is not None and expected_revision != self._state.revision:
                raise RevisionConflictError(expected_revision, self._state.revision)
            trial = self._state.copy()
            trial.apply(StoredEvent(seq=self._state.revision + 1, event=event))
            try:
                seq = self._store.append(event, expected_revision=self._state.revision)
            except RevisionConflictError as exc:
                last = exc
                if expected_revision is not None:
                    raise
                continue
            self._state = trial
            return seq
        assert last is not None
        raise last

    def events(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Read stored events after a revision."""
        return self._store.read(after=after)

    def close(self) -> None:
        """Close the underlying store when it holds a connection."""
        close = getattr(self._store, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> Registry:
        """Support `with Registry(...) as registry:`."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()

    def _catch_up(self) -> None:
        for stored in self._store.read(after=self._state.revision):
            self._state.apply(stored)
