"""Claude adapters: a classifier constrained to a closed set, and a drafter for prose and SQL.

The classifier is the interim stand-in for a System One model. Its probabilities are the
model's self-report and are not calibrated until the calibration report says they are.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, create_model

from veena.errors import ModelError, ModelNotConfiguredError
from veena.models.protocol import Classification

if TYPE_CHECKING:
    import anthropic

DEFAULT_MODEL = "claude-fable-5-1"

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
    import anthropic

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # The SDK also reads an `ant auth login` profile; let it try, but name the fix if it fails.
        try:
            return anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            msg = "set ANTHROPIC_API_KEY or run `ant auth login` to use the Claude adapters"
            raise ModelNotConfiguredError(msg) from exc
    return anthropic.Anthropic()


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

        Raises:
            ModelError: Claude refused, stopped early, or returned a value outside the set.
            ModelNotConfiguredError: No credentials are available.
        """
        names = tuple(o.value for o in options)
        output = create_model(
            "ClosedChoice",
            value=(Literal[names], ...),
            probability=(float, Field(ge=0.0, le=1.0)),
            alternatives=(dict[str, float], Field(default_factory=dict)),
        )
        client = self._client or _client()
        response = client.messages.parse(
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
        )
        if getattr(response, "stop_reason", None) == "refusal":
            msg = f"{self.model_id} refused the classification"
            raise ModelError(msg)
        parsed: Any = response.parsed_output
        if parsed is None:
            msg = f"{self.model_id} returned no structured output"
            raise ModelError(msg)
        value = str(parsed.value)
        if value not in names:
            msg = f"{self.model_id} returned {value!r}, not one of {list(names)}"
            raise ModelError(msg)
        alternatives = tuple(
            (options(k), float(v))
            for k, v in dict(parsed.alternatives).items()
            if k in names and k != value
        )
        return Classification(
            value=options(value),
            probability=float(parsed.probability),
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
            ModelError: Claude refused or returned no text.
            ModelNotConfiguredError: No credentials are available.
        """
        client = self._client or _client()
        response = client.messages.create(
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
