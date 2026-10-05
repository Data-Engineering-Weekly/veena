"""Find overlap between a new spec and the graph, and derive a shared intermediate.

Everything here works on specifications. Similarity never comes from SQL or data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.refs import OriginRef, SemanticRef
from veena.spec.model import AssetSpec, Grain, Population, TimeBucket, Window
from veena.spec.subsumption import SubsumptionResult, check

if TYPE_CHECKING:
    from collections.abc import Iterable


class OverlapReport(BaseModel):
    """What subsumption said about a new spec against every existing spec."""

    model_config = ConfigDict(frozen=True)

    new_spec: str
    served_by: tuple[SubsumptionResult, ...]
    could_serve: tuple[SubsumptionResult, ...]
    considered: tuple[str, ...]

    @property
    def provider(self) -> SubsumptionResult | None:
        """The first existing spec that can serve the new one, if any."""
        return next((r for r in self.served_by if r.can_serve), None)


def find_overlap(new_spec: AssetSpec, existing: Iterable[AssetSpec]) -> OverlapReport:
    """Run subsumption in both directions between the new spec and each existing spec."""
    served_by: list[SubsumptionResult] = []
    could_serve: list[SubsumptionResult] = []
    considered: list[str] = []
    for other in existing:
        if other.asset_id == new_spec.asset_id:
            continue
        forward = check(other, new_spec)
        backward = check(new_spec, other)
        served_by.append(forward)
        could_serve.append(backward)
        considered.append(_line(forward))
        considered.append(_line(backward))
    return OverlapReport(
        new_spec=new_spec.ref,
        served_by=tuple(served_by),
        could_serve=tuple(could_serve),
        considered=tuple(considered),
    )


def derive_shared(a: AssetSpec, b: AssetSpec, *, asset_id: str, origin: str) -> AssetSpec | None:
    """Derive a time-bucketed spec that could serve both windowed specs, or None.

    Both specs must agree on population entity, identity policy, access policy, timezone, and
    every measure must be re-aggregable. The result takes a daily bucket and enough history for
    the longer window. It omits inactive members; consumers add them back with a population join.

    Args:
        a: One windowed spec.
        b: Another windowed spec.
        asset_id: Identity for the derived asset.
        origin: Provenance ref for the derived requirements, for example `proposal:p28`.
    """
    if a.population.entity != b.population.entity:
        return None
    if a.identity_policy != b.identity_policy:
        return None
    if a.service.access_policy != b.service.access_policy:
        return None
    if a.window.timezone != b.window.timezone:
        return None
    if a.grain.time_bucket is not TimeBucket.NONE or b.grain.time_bucket is not TimeBucket.NONE:
        return None
    if set(a.grain.keys) != set(b.grain.keys) or len(a.grain.keys) != 1:
        return None
    if a.filters or b.filters:
        return None
    measures = {(m.calculation, m.aggregation): m for m in (*a.measures, *b.measures)}
    if any(not agg.reaggregable for _, agg in measures):
        return None
    history = max(a.window.span_days, b.window.span_days)
    provenance = {
        "grain.time_bucket": OriginRef.model_validate(origin),
        "window.history_days": OriginRef.model_validate(origin),
        "population.include_inactive": OriginRef.model_validate(origin),
    }
    for source in (a, b):
        provenance[f"serves.{source.asset_id}"] = OriginRef(
            name=source.asset_id, version=source.spec_version, origin_kind="asset"
        )
    for key in ("identity_policy", "measures.spend_usd.calculation"):
        if key in a.provenance:
            provenance[key] = a.provenance[key]
    return AssetSpec(
        asset_id=asset_id,
        spec_version=1,
        population=Population(entity=a.population.entity, include_inactive=False),
        grain=Grain(keys=a.grain.keys, time_bucket=TimeBucket.DAY),
        measures=tuple(measures.values()),
        window=Window(history_days=history, timezone=a.window.timezone),
        identity_policy=a.identity_policy,
        provenance=provenance,
        service=a.service.model_copy(
            update={
                "incorporate_within": min(
                    a.service.incorporate_within, b.service.incorporate_within
                )
            }
        ),
    )


def shared_asset_id(a: AssetSpec, b: AssetSpec) -> str:
    """Name for the derived asset: the common prefix of both ids plus the bucket."""
    prefix = _common_prefix(a.asset_id, b.asset_id).rstrip("_")
    return f"{prefix}_daily" if prefix else f"{a.asset_id}_{b.asset_id}_daily"


def _common_prefix(x: str, y: str) -> str:
    out = []
    for cx, cy in zip(x, y, strict=False):
        if cx != cy:
            break
        out.append(cx)
    return "".join(out)


def _line(result: SubsumptionResult) -> str:
    text = f"{result.provider} -> {result.consumer}: {result.verdict}"
    if result.operations:
        text += " needs " + ", ".join(result.operations)
    if result.reasons:
        text += " because " + "; ".join(result.reasons)
    return text


__all__ = ["OverlapReport", "SemanticRef", "derive_shared", "find_overlap", "shared_asset_id"]
