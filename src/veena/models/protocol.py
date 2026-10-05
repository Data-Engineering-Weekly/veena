"""The two model protocols and scripted adapters for tests."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable


class Classification[T: StrEnum](BaseModel):
    """A value from a closed set and the probability the model assigns to it."""

    model_config = ConfigDict(frozen=True)

    value: T
    probability: float = Field(ge=0.0, le=1.0)
    alternatives: tuple[tuple[T, float], ...] = ()
    model_id: str


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


class ScriptedClassifier:
    """Returns answers from a script keyed by context type name, for offline tests."""

    def __init__(self, script: dict[str, tuple[str, float]], model_id: str = "scripted") -> None:
        """Map each context class name to the option value and probability to return."""
        self._script = script
        self._model_id = model_id

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Look up the context type in the script and coerce the value into `options`.

        Raises:
            KeyError: The context type has no scripted answer.
            ValueError: The scripted value is not a member of `options`.
        """
        value, probability = self._script[type(context).__name__]
        return Classification(
            value=options(value), probability=probability, model_id=self._model_id
        )


class ScriptedDrafter:
    """Returns text from a function of the request, for offline tests."""

    def __init__(self, render: Callable[[BaseModel], str]) -> None:
        """Use `render` to produce the draft for each request."""
        self._render = render

    def draft(self, request: BaseModel) -> str:
        """Return the scripted text."""
        return self._render(request)
