"""The registry facade: append events, read the projected state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veena.registry.state import RegistryState
from veena.registry.store import StoredEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from veena.registry.events import Event
    from veena.registry.store import EventStore


class Registry:
    """Wraps an event store and keeps a projection current with it.

    Every append validates the event against the current projection before it reaches the
    store, so the store never holds an event the projection would reject.
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
    def revision(self) -> int:
        """The store's current revision."""
        return self._store.revision()

    def append(self, event: Event, *, expected_revision: int | None = None) -> int:
        """Validate and store one event.

        Args:
            event: The event to record.
            expected_revision: If given, fail unless the store is at this revision.

        Returns:
            The `seq` the store assigned.

        Raises:
            RegistryError: The projection rejects the event.
            RevisionConflictError: The store moved past `expected_revision`.
        """
        self._catch_up()
        trial = _copy_state(self._state)
        trial.apply(StoredEvent(seq=self._state.revision + 1, event=event))
        seq = self._store.append(event, expected_revision=expected_revision)
        if seq == self._state.revision + 1:
            self._state = trial
        else:
            self._catch_up()
        return seq

    def events(self, *, after: int = 0) -> Iterator[StoredEvent]:
        """Read stored events after a revision."""
        return self._store.read(after=after)

    def _catch_up(self) -> None:
        for stored in self._store.read(after=self._state.revision):
            self._state.apply(stored)


def _copy_state(state: RegistryState) -> RegistryState:
    copy = RegistryState()
    copy.revision = state.revision
    copy.assets = dict(state.assets)
    copy.commitments = dict(state.commitments)
    copy.questions = dict(state.questions)
    copy.decisions = dict(state.decisions)
    copy.proposals = dict(state.proposals)
    copy.implementations = dict(state.implementations)
    copy.classifications = list(state.classifications)
    copy.drafts = list(state.drafts)
    copy.verifications = dict(state.verifications)
    copy.deployments = dict(state.deployments)
    return copy
