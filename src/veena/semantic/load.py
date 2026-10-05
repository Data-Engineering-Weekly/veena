"""Read a semantic model from YAML."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from veena.errors import SemanticRefError, SpecError
from veena.semantic.model import SemanticModel

if TYPE_CHECKING:
    from pathlib import Path


def load_semantic_model(path: Path) -> SemanticModel:
    """Load a semantic model from a YAML file and check that every internal name resolves.

    Args:
        path: File holding one semantic model document.

    Returns:
        The validated model.

    Raises:
        SpecError: The file cannot be read or fails validation.
        SemanticRefError: A definition names a source or entity that does not exist.
    """
    try:
        raw = yaml.safe_load(path.read_text())
        model = SemanticModel.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        msg = f"semantic model {path} is invalid: {exc}"
        raise SpecError(msg) from exc
    _check_internal_names(model)
    return model


def _check_internal_names(model: SemanticModel) -> None:
    for entity in model.entities:
        source = model.source(entity.source)
        if entity.key not in source.columns:
            msg = f"entity {entity.name} key {entity.key!r} is not a column of {source.name}"
            raise SemanticRefError(msg)
    for calc in model.calculations:
        model.source(calc.source)
    for rule in model.time_rules:
        source = model.source(rule.source)
        if rule.timestamp_column not in source.columns:
            msg = f"time rule {rule.name} column {rule.timestamp_column!r} is not in {source.name}"
            raise SemanticRefError(msg)
    for policy in model.identity_policies:
        model.entity(policy.entity)
        fact = model.source(policy.fact_source)
        mapping = model.source(policy.mapping_source)
        for column, source in (
            (policy.fact_key, fact),
            (policy.fact_timestamp, fact),
            (policy.mapping_fact_key, mapping),
            (policy.mapping_entity_key, mapping),
            (policy.valid_from, mapping),
            (policy.valid_to, mapping),
        ):
            if column not in source.columns:
                msg = f"identity policy {policy.name} column {column!r} is not in {source.name}"
                raise SemanticRefError(msg)
