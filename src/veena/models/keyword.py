"""A deterministic keyword classifier: the offline baseline and the CLI default without keys.

It scores each option by word overlap between the context's text fields and the option's name
plus any description the context supplies. It is honest about its weakness: probabilities are
normalized scores, not calibrated, and the calibration report will say so.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from veena.models.protocol import Classification

if TYPE_CHECKING:
    from pydantic import BaseModel

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "for",
        "per",
        "over",
        "by",
        "in",
        "on",
        "to",
        "with",
        "and",
        "or",
        "each",
        "every",
        "one",
        "row",
        "rows",
        "previous",
        "last",
        "complete",
        "calendar",
        "days",
        "day",
        "return",
        "their",
        "its",
        "our",
        "use",
        "include",
        "including",
        "no",
        "with",
        "purchase",
        "qualifying",
        "settled",
        "refunded",
        "zero",
        "contribute",
        "do",
        "not",
    ]
)


class KeywordClassifier:
    """Picks the option whose words overlap most with the context text."""

    model_id = "keyword"

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Score every option and normalize the scores into probabilities.

        Verdict contexts (screening, judging) have no `slot`; word overlap says nothing useful
        about them, so the baseline abstains with a uniform probability over the options.
        """
        members = list(options)
        if not hasattr(context, "slot"):
            uniform = round(1.0 / len(members), 4)
            return Classification(
                value=members[0],
                probability=uniform,
                alternatives=tuple((m, uniform) for m in members[1:]),
                model_id=self.model_id,
            )
        text_words = _words(_context_text(context))
        descriptions: dict[str, str] = {}
        raw = getattr(context, "option_descriptions", None)
        if isinstance(raw, dict):
            descriptions = {str(k): str(v) for k, v in raw.items()}
        # A word in the option's own name is stronger evidence than one in its description,
        # because descriptions of sibling options often share vocabulary.
        scores: dict[T, float] = {}
        for option in options:
            name_words = _words(option.value.replace("_", " "))
            description_words = _words(descriptions.get(option.value, "")) - name_words
            scores[option] = 3.0 * len(text_words & name_words) + 1.0 * len(
                text_words & description_words
            )
        total = sum(scores.values())
        if total == 0:
            uniform = 1.0 / len(members)
            ranked = [(m, uniform) for m in members]
        else:
            ranked = sorted(((m, s / total) for m, s in scores.items()), key=lambda i: -i[1])
        best, probability = ranked[0]
        return Classification(
            value=best,
            probability=round(probability, 4),
            alternatives=tuple((m, round(p, 4)) for m, p in ranked[1:]),
            model_id=self.model_id,
        )


def _context_text(context: BaseModel) -> str:
    parts: list[str] = []
    for name, value in context.model_dump(mode="json").items():
        if name == "option_descriptions":
            continue
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _words(text: str) -> set[str]:
    out: set[str] = set()
    for word in _WORD.findall(text.lower()):
        stem = word[:-1] if len(word) > 3 and word.endswith("s") else word
        if stem not in _STOP:
            out.add(stem)
    return out
