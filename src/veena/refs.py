"""Versioned references to records veena keeps: semantic definitions, policies, decisions."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

_REF = re.compile(r"^(?P<kind>[a-z]+):(?P<name>[a-z][a-z0-9_]*)(?:@(?P<version>\d+))?$")


class Ref(BaseModel):
    """A reference of the form `<kind>:<name>` or `<kind>:<name>@<version>`.

    Subclasses fix `kind` and decide whether the version is required.
    """

    model_config = ConfigDict(frozen=True)

    kind: ClassVar[str] = ""
    versioned: ClassVar[bool] = True

    name: str
    version: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _parse_text(cls, data: Any) -> Any:
        if not isinstance(data, str):
            return data
        match = _REF.match(data)
        if match is None:
            msg = f"malformed ref {data!r}; expected <kind>:<name>[@<version>]"
            raise ValueError(msg)
        if cls.kind and match["kind"] != cls.kind:
            msg = f"ref {data!r} has kind {match['kind']!r}; expected {cls.kind!r}"
            raise ValueError(msg)
        version = match["version"]
        if cls.versioned and version is None:
            msg = f"ref {data!r} needs a version, for example {data}@1"
            raise ValueError(msg)
        return {"name": match["name"], "version": int(version) if version else None}

    def __str__(self) -> str:
        """Render the ref in its text form."""
        suffix = "" if self.version is None else f"@{self.version}"
        return f"{self.kind}:{self.name}{suffix}"


class SemanticRef(Ref):
    """A versioned pointer into the semantic model, for example `semantic:purchase_amount_usd@4`."""

    kind: ClassVar[str] = "semantic"


class PolicyRef(Ref):
    """A versioned pointer to a workspace policy, for example `policy:approved_spending@4`."""

    kind: ClassVar[str] = "policy"


class OriginRef(Ref):
    """Where a requirement came from: a message, decision, policy, or conversation."""

    kind: ClassVar[str] = ""
    versioned: ClassVar[bool] = False

    origin_kind: str = ""

    @model_validator(mode="before")
    @classmethod
    def _parse_origin(cls, data: Any) -> Any:
        if not isinstance(data, str):
            return data
        match = _REF.match(data)
        if match is None or match["kind"] not in {"message", "decision", "policy", "conversation"}:
            msg = (
                f"malformed origin ref {data!r}; "
                "expected message:, decision:, policy:, or conversation:"
            )
            raise ValueError(msg)
        version = match["version"]
        return {
            "name": match["name"],
            "version": int(version) if version else None,
            "origin_kind": match["kind"],
        }

    def __str__(self) -> str:
        """Render the ref in its text form."""
        suffix = "" if self.version is None else f"@{self.version}"
        return f"{self.origin_kind}:{self.name}{suffix}"
