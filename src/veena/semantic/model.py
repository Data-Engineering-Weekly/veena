"""Pydantic models for the semantic layer."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veena.errors import SemanticRefError
from veena.refs import SemanticRef
from veena.spec.model import validate_timezone

_IDENT = r"^[a-z][a-z0-9_]*$"
# Identifiers reach SQL unquoted, so they must be plain names; a relation may be qualified.
_COLUMN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_RELATION = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$"


class _Versioned(BaseModel):
    """A named definition with an integer version."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=_IDENT)
    version: int = Field(ge=1)


class Source(BaseModel):
    """A physical table the workspace trusts as authoritative."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=_IDENT)
    table: str = Field(pattern=_RELATION)
    primary_key: tuple[Annotated[str, Field(pattern=_COLUMN)], ...] = Field(min_length=1)
    columns: tuple[Annotated[str, Field(pattern=_COLUMN)], ...] = Field(min_length=1)


class Entity(_Versioned):
    """A thing rows can be about, with the source that lists every instance."""

    model_config = ConfigDict(frozen=True)

    source: str
    key: str = Field(pattern=_COLUMN)
    description: str = ""


class Calculation(_Versioned):
    """An approved expression over one source's columns, with its unit."""

    model_config = ConfigDict(frozen=True)

    source: str
    expression: str
    unit: str
    description: str = ""


class TimeRule(_Versioned):
    """Which timestamp on a source attributes a row to a date, and in which timezone."""

    model_config = ConfigDict(frozen=True)

    source: str
    timestamp_column: str = Field(pattern=_COLUMN)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        return validate_timezone(value)


class IdentityPolicy(_Versioned):
    """How a fact row attaches to an entity instance.

    `kind` is `point_in_time` when a mapping source holds rows with a validity interval and the
    fact's timestamp must fall inside `[valid_from, valid_to)`, a null `valid_to` meaning still
    valid. It is `direct` when the fact row carries the entity key itself in `fact_key`; the
    mapping fields are then empty.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["point_in_time", "direct"] = "point_in_time"
    entity: SemanticRef
    fact_source: str
    fact_key: str = Field(pattern=_COLUMN)
    fact_timestamp: str = Field(pattern=_COLUMN)
    mapping_source: str = ""
    mapping_fact_key: str = Field(default="", pattern=rf"(?:{_COLUMN[1:-1]})?")
    mapping_entity_key: str = Field(default="", pattern=rf"(?:{_COLUMN[1:-1]})?")
    valid_from: str = Field(default="", pattern=rf"(?:{_COLUMN[1:-1]})?")
    valid_to: str = Field(default="", pattern=rf"(?:{_COLUMN[1:-1]})?")
    description: str = ""

    @model_validator(mode="after")
    def _mapping_fields_match_kind(self) -> IdentityPolicy:
        mapping = (
            self.mapping_source,
            self.mapping_fact_key,
            self.mapping_entity_key,
            self.valid_from,
            self.valid_to,
        )
        if self.kind == "point_in_time" and not all(mapping):
            msg = f"identity policy {self.name} is point_in_time and needs every mapping field"
            raise ValueError(msg)
        if self.kind == "direct" and any(mapping):
            msg = f"identity policy {self.name} is direct and must not name a mapping source"
            raise ValueError(msg)
        return self


class SemanticModel(BaseModel):
    """Every definition the engine may rely on, by name and version."""

    model_config = ConfigDict(frozen=True)

    sources: tuple[Source, ...] = ()
    entities: tuple[Entity, ...] = ()
    calculations: tuple[Calculation, ...] = ()
    time_rules: tuple[TimeRule, ...] = ()
    identity_policies: tuple[IdentityPolicy, ...] = ()

    def fingerprint(self) -> str:
        """A short stable hash of every definition, used as the version a plan assumed."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]

    def source(self, name: str) -> Source:
        """Look up a source by name.

        Raises:
            SemanticRefError: No source has that name.
        """
        for source in self.sources:
            if source.name == name:
                return source
        msg = f"unknown source {name!r}"
        raise SemanticRefError(msg)

    def entity(self, ref: SemanticRef) -> Entity:
        """Resolve an entity ref.

        Raises:
            SemanticRefError: No entity matches the name and version.
        """
        return _resolve(self.entities, ref, "entity")

    def calculation(self, ref: SemanticRef) -> Calculation:
        """Resolve a calculation ref.

        Raises:
            SemanticRefError: No calculation matches the name and version.
        """
        return _resolve(self.calculations, ref, "calculation")

    def time_rule(self, ref: SemanticRef) -> TimeRule:
        """Resolve a time rule ref.

        Raises:
            SemanticRefError: No time rule matches the name and version.
        """
        return _resolve(self.time_rules, ref, "time rule")

    def identity_policy(self, ref: SemanticRef) -> IdentityPolicy:
        """Resolve an identity policy ref.

        Raises:
            SemanticRefError: No identity policy matches the name and version.
        """
        return _resolve(self.identity_policies, ref, "identity policy")


def _resolve[T: _Versioned](items: tuple[T, ...], ref: SemanticRef, kind: str) -> T:
    for item in items:
        if item.name == ref.name and item.version == ref.version:
            return item
    known = sorted(f"{i.name}@{i.version}" for i in items if i.name == ref.name)
    hint = f"; known versions: {known}" if known else ""
    msg = f"unknown {kind} {ref}{hint}"
    raise SemanticRefError(msg)
