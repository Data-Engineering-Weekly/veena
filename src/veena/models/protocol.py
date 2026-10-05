"""The two model protocols, scripted adapters for tests, and record-and-replay wrappers."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import ModelError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


class Classification[T: StrEnum](BaseModel):
    """A value from a closed set and the probability the model assigns to it."""

    model_config = ConfigDict(frozen=True)

    value: T
    probability: float = Field(ge=0.0, le=1.0)
    alternatives: tuple[tuple[T, float], ...] = ()
    model_id: str

    def ranked(self) -> tuple[tuple[T, float], ...]:
        """Every option the model scored, best first, with the chosen value included."""
        seen = {self.value: self.probability}
        for value, probability in self.alternatives:
            seen.setdefault(value, probability)
        return tuple(sorted(seen.items(), key=lambda item: -item[1]))


class Classifier(Protocol):
    """Picks one option from a closed set with a calibrated probability."""

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Return one member of `options` and its probability.

        Args:
            context: Typed, structured input. Never raw SQL from a candidate.
            options: The closed set to choose from.
        """
        ...


class Drafter(Protocol):
    """Writes text from structured input: SQL for a step, prose for a person."""

    def draft(self, request: BaseModel) -> str:
        """Return text for the structured request."""
        ...


def closed_set(names: Sequence[str]) -> type[StrEnum]:
    """Build the closed option set a classifier must choose from, one member per name."""
    # The functional enum API returns the new class; mypy types the call as an instance.
    return cast("type[StrEnum]", StrEnum("ClosedSet", {name: name for name in names}))


def input_hash(context: BaseModel, options: type[StrEnum] | None = None) -> str:
    """A stable hash of a context and option set, recorded on every model event."""
    payload = {
        "context_type": type(context).__name__,
        "context": context.model_dump(mode="json"),
        "options": [o.value for o in options] if options else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class ScriptedClassifier:
    """Returns answers from a script, for offline tests.

    The script maps a context class name, or `"<ClassName>:<slot>"` when the context carries a
    `slot` field, to a value and probability, plus optional alternatives.
    """

    def __init__(
        self,
        script: dict[str, tuple[str, float] | tuple[str, float, dict[str, float]]],
        model_id: str = "scripted",
    ) -> None:
        """Map each context key to the option value and probability to return."""
        self._script = script
        self._model_id = model_id

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Look up the context in the script and coerce the value into `options`.

        Raises:
            KeyError: The context has no scripted answer.
            ValueError: The scripted value is not a member of `options`.
        """
        key = type(context).__name__
        slot = getattr(context, "slot", None)
        entry = self._script.get(f"{key}:{slot}") if slot else None
        if entry is None:
            entry = self._script[key]
        value, probability = entry[0], entry[1]
        extras = entry[2] if len(entry) > 2 else {}
        return Classification(
            value=options(value),
            probability=probability,
            alternatives=tuple((options(k), v) for k, v in extras.items()),
            model_id=self._model_id,
        )


class ScriptedDrafter:
    """Returns text from a function of the request, for offline tests."""

    def __init__(self, render: Callable[[BaseModel], str]) -> None:
        """Use `render` to produce the draft for each request."""
        self._render = render

    def draft(self, request: BaseModel) -> str:
        """Return the scripted text."""
        return self._render(request)


class RecordingClassifier:
    """Wraps a classifier and appends every answer to a JSONL file keyed by input hash."""

    def __init__(self, inner: Classifier, path: Path) -> None:
        """Record `inner`'s answers to `path`."""
        self._inner = inner
        self._path = path

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Classify with the inner adapter and record the result."""
        result = self._inner.classify(context, options)
        record = {
            "input_hash": input_hash(context, options),
            "value": result.value.value,
            "probability": result.probability,
            "alternatives": [[v.value, p] for v, p in result.alternatives],
            "model_id": result.model_id,
        }
        with self._path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        return result


class ReplayClassifier:
    """Answers from a recording, so a real model's outputs can drive tests offline."""

    def __init__(self, path: Path) -> None:
        """Load every recorded answer from `path`."""
        self._records: dict[str, dict[str, object]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    record = json.loads(line)
                    self._records[str(record["input_hash"])] = record

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Return the recorded answer for this input.

        Raises:
            ModelError: No recording exists for the input.
        """
        key = input_hash(context, options)
        record = self._records.get(key)
        if record is None:
            msg = f"no recorded answer for {type(context).__name__} ({key})"
            raise ModelError(msg)
        raw = record.get("alternatives")
        pairs: list[list[object]] = raw if isinstance(raw, list) else []
        return Classification(
            value=options(str(record["value"])),
            probability=float(str(record["probability"])),
            alternatives=tuple((options(str(v)), float(str(p))) for v, p in pairs),
            model_id=f"replay:{record.get('model_id', 'unknown')}",
        )
