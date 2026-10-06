"""Claude adapters: a classifier constrained to a closed set, and a drafter for prose and SQL.

The classifier is the interim stand-in for a System One model. Its probabilities are the
model's self-report and are not calibrated until the calibration report says they are.
Install with `pip install veena[claude]`; credentials come from `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, create_model

from veena.errors import ModelError, ModelNotConfiguredError
from veena.models.protocol import Classification

if TYPE_CHECKING:
    import anthropic

DEFAULT_MODEL = "claude-fable-5-1"
_LITERAL_LIMIT = 100

_CLASSIFY_SYSTEM = (
    "You classify structured requests for a data platform. Choose exactly one option from the "
    "closed set and report an honest probability that it is the intended one. Spread the "
    "remaining probability over the other options in `alternatives`. Never invent an option."
)
_DRAFT_SYSTEM = (
    "You write short, plain text for a data platform from structured input. Say what the input "
    "says and nothing more. No preamble."
)


def _client() -> anthropic.Anthropic:
    try:
        import anthropic
    except ImportError as exc:
        msg = "the Claude adapters need the anthropic package: install veena[claude]"
        raise ModelNotConfiguredError(msg) from exc
    client = anthropic.Anthropic()
    if not (getattr(client, "api_key", None) or getattr(client, "auth_token", None)):
        msg = "set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN to use the Claude adapters"
        raise ModelNotConfiguredError(msg)
    return client


def _call(model_id: str, request: Any) -> Any:
    """Run one SDK call and turn every failure into a typed error."""
    failures: tuple[type[BaseException], ...] = (TypeError, ValidationError)
    try:
        import anthropic

        failures = (*failures, anthropic.AnthropicError)
    except ImportError:
        pass
    try:
        return request()
    except failures as exc:
        msg = f"{model_id} call failed: {type(exc).__name__}: {exc}"
        raise ModelError(msg) from exc


class ClaudeClassifier:
    """Asks Claude for one option plus a probability, using a structured output schema."""

    def __init__(
        self, *, model: str | None = None, client: Any | None = None, max_tokens: int = 1024
    ) -> None:
        """Bind a model id and, for tests, a client-shaped object with `messages.parse`."""
        self._model = model or os.environ.get("VEENA_CLAUDE_MODEL", DEFAULT_MODEL)
        self._client = client
        self._max_tokens = max_tokens
        self.model_id = f"claude:{self._model}"

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Return the option Claude picked and its stated probability.

        Up to a hundred options are enforced by the output schema; beyond that the options are
        listed in the prompt and the answer is checked for membership.

        Raises:
            ModelError: Claude refused, stopped early, failed, or returned a value outside the set.
            ModelNotConfiguredError: No credentials or no SDK.
        """
        names = tuple(o.value for o in options)
        value_type: Any = Literal[names] if len(names) <= _LITERAL_LIMIT else str
        output = create_model(
            "ClosedChoice",
            value=(value_type, ...),
            probability=(float, Field(ge=0.0, le=1.0)),
            alternatives=(dict[str, float], Field(default_factory=dict)),
        )
        client = self._client or _client()
        response = _call(
            self.model_id,
            lambda: client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_CLASSIFY_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Context ({type(context).__name__}):\n"
                            f"{context.model_dump_json(indent=2)}\n\n"
                            f"Options: {', '.join(names)}"
                        ),
                    }
                ],
                output_format=output,
            ),
        )
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            msg = f"{self.model_id} refused the classification"
            raise ModelError(msg)
        if stop == "max_tokens":
            msg = f"{self.model_id} stopped at max_tokens before finishing the answer"
            raise ModelError(msg)
        parsed: Any = response.parsed_output
        if parsed is None:
            msg = f"{self.model_id} returned no structured output"
            raise ModelError(msg)
        value = str(parsed.value)
        if value not in names:
            msg = f"{self.model_id} returned {value!r}, not one of {list(names)}"
            raise ModelError(msg)
        probability = float(parsed.probability)
        if not 0.0 <= probability <= 1.0:
            msg = f"{self.model_id} returned probability {probability}, outside [0, 1]"
            raise ModelError(msg)
        alternatives = tuple(
            (options(k), float(v))
            for k, v in dict(parsed.alternatives).items()
            if k in names and k != value
        )
        return Classification(
            value=options(value),
            probability=probability,
            alternatives=alternatives,
            model_id=self.model_id,
        )


class ClaudeDrafter:
    """Asks Claude for text from a structured request."""

    def __init__(
        self, *, model: str | None = None, client: Any | None = None, max_tokens: int = 4096
    ) -> None:
        """Bind a model id and, for tests, a client-shaped object with `messages.create`."""
        self._model = model or os.environ.get("VEENA_CLAUDE_MODEL", DEFAULT_MODEL)
        self._client = client
        self._max_tokens = max_tokens
        self.model_id = f"claude:{self._model}"

    def draft(self, request: BaseModel) -> str:
        """Return the text Claude wrote.

        Raises:
            ModelError: Claude refused, failed, or returned no text.
            ModelNotConfiguredError: No credentials or no SDK.
        """
        client = self._client or _client()
        response = _call(
            self.model_id,
            lambda: client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_DRAFT_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Write the text this input asks for ({type(request).__name__}):\n"
                            f"{request.model_dump_json(indent=2)}"
                        ),
                    }
                ],
            ),
        )
        if getattr(response, "stop_reason", None) == "refusal":
            msg = f"{self.model_id} refused the draft"
            raise ModelError(msg)
        text = "".join(
            str(getattr(block, "text", ""))
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            msg = f"{self.model_id} returned no text"
            raise ModelError(msg)
        return text
