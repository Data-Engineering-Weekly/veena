"""Read and write specifications as YAML."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from veena.errors import SpecError
from veena.spec.model import AssetSpec

if TYPE_CHECKING:
    from pathlib import Path


def load_spec(path: Path) -> AssetSpec:
    """Load one specification from a YAML file.

    Args:
        path: File holding a single spec document.

    Returns:
        The validated spec.

    Raises:
        SpecError: The file is missing, is not a mapping, or fails validation.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        msg = f"cannot read spec {path}: {exc}"
        raise SpecError(msg) from exc
    return parse_spec(raw, source=str(path))


def parse_spec(raw: Any, *, source: str = "<memory>") -> AssetSpec:
    """Validate an already-parsed YAML/JSON mapping as a spec.

    Args:
        raw: The decoded document.
        source: Label used in error messages.

    Returns:
        The validated spec.

    Raises:
        SpecError: The document is not a mapping or fails validation.
    """
    if not isinstance(raw, dict):
        msg = f"spec {source} must be a mapping, got {type(raw).__name__}"
        raise SpecError(msg)
    try:
        return AssetSpec.model_validate(raw)
    except ValidationError as exc:
        msg = f"spec {source} is invalid:\n{exc}"
        raise SpecError(msg) from exc


def dump_spec(spec: AssetSpec) -> str:
    """Render a spec as YAML with refs in their text form."""
    data = spec.model_dump(mode="json")
    return yaml.safe_dump(_textify(spec, data), sort_keys=False)


def _textify(spec: AssetSpec, data: dict[str, Any]) -> dict[str, Any]:
    data["population"]["entity"] = str(spec.population.entity)
    data["grain"]["keys"] = [str(k) for k in spec.grain.keys]
    for measure, out in zip(spec.measures, data["measures"], strict=True):
        out["calculation"] = str(measure.calculation)
    for flt, out in zip(spec.filters, data["filters"], strict=True):
        out["attribute"] = str(flt.attribute)
    data["identity_policy"] = str(spec.identity_policy)
    data["provenance"] = {k: str(v) for k, v in spec.provenance.items()}
    data["service"]["access_policy"] = str(spec.service.access_policy)
    data["service"]["budget_policy"] = str(spec.service.budget_policy)
    data["service"]["incorporate_within"] = int(spec.service.incorporate_within.total_seconds())
    return data
