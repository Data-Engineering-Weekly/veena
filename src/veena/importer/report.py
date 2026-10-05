"""Find duplicates and reuse across the whole graph, and propose consolidations in shadow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.policy import ReorganizationPolicy
from veena.registry.events import Event, ProposalOpened
from veena.spec.subsumption import SubsumptionResult, Verdict, check

if TYPE_CHECKING:
    from datetime import datetime

    from veena.registry.registry import Registry


class AssetLine(BaseModel):
    """One asset's consumers and measured refresh cost."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    spec_ref: str
    owner: str
    consumers: tuple[str, ...]
    refresh_runs: int
    refresh_bytes: int
    refresh_credits: float


class DuplicateGroup(BaseModel):
    """Assets whose specifications mean the same thing."""

    model_config = ConfigDict(frozen=True)

    assets: tuple[AssetLine, ...]
    evidence: tuple[str, ...]

    @property
    def total_credits(self) -> float:
        """Credits spent rebuilding every member over the observation window."""
        return round(sum(a.refresh_credits for a in self.assets), 6)


class ServesEdge(BaseModel):
    """One spec can serve another with the listed operations."""

    model_config = ConfigDict(frozen=True)

    provider: str
    consumer: str
    operations: tuple[str, ...]


class DuplicatesReport(BaseModel):
    """Equivalence sets and serves-with edges across specified assets."""

    model_config = ConfigDict(frozen=True)

    specified: int
    groups: tuple[DuplicateGroup, ...]
    edges: tuple[ServesEdge, ...]

    def render(self) -> str:
        """Plain text for the CLI."""
        lines = [f"{self.specified} assets with confirmed specifications"]
        if not self.groups:
            lines.append("no equivalence sets")
        for index, group in enumerate(self.groups, start=1):
            lines.append(
                f"equivalence set {index}: {len(group.assets)} assets, "
                f"{group.total_credits:.4f} credits rebuilding them over the window"
            )
            for a in group.assets:
                consumers = ", ".join(a.consumers) if a.consumers else "no commitments"
                lines.append(
                    f"  {a.asset_id:<28} {a.spec_ref:<28} owner {a.owner or '-':<22} "
                    f"{a.refresh_runs} rebuilds, {a.refresh_credits:.4f} credits; {consumers}"
                )
            for text in group.evidence:
                lines.append(f"  evidence: {text}")
        if self.edges:
            lines.append("reuse edges:")
            for edge in self.edges:
                lines.append(
                    f"  {edge.provider} -> {edge.consumer} needs {', '.join(edge.operations)}"
                )
        return "\n".join(lines)


def duplicates_report(registry: Registry) -> DuplicatesReport:
    """Run subsumption across every pair of specified assets."""
    state = registry.state
    specs = {a.asset_id: a.spec for a in state.assets.values() if a.spec is not None}
    ids = sorted(specs)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges: list[ServesEdge] = []
    equal_pairs: dict[tuple[str, str], SubsumptionResult] = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            forward = check(specs[a], specs[b])
            if forward.verdict is Verdict.EQUAL:
                parent[find(a)] = find(b)
                equal_pairs[(a, b)] = forward
                continue
            if forward.verdict is Verdict.SERVES_WITH:
                edges.append(
                    ServesEdge(provider=a, consumer=b, operations=tuple(forward.operations))
                )
            backward = check(specs[b], specs[a])
            if backward.verdict is Verdict.SERVES_WITH:
                edges.append(
                    ServesEdge(provider=b, consumer=a, operations=tuple(backward.operations))
                )

    members: dict[str, list[str]] = {}
    for asset_id in ids:
        members.setdefault(find(asset_id), []).append(asset_id)
    groups: list[DuplicateGroup] = []
    for group_ids in sorted(members.values()):
        if len(group_ids) < 2:
            continue
        lines = tuple(_line(registry, asset_id) for asset_id in group_ids)
        spec = specs[group_ids[0]]
        evidence = (
            f"same population {spec.population.entity}, identity policy {spec.identity_policy}, "
            f"measures {', '.join(f'{m.aggregation}({m.calculation})' for m in spec.measures)}, "
            f"window {spec.window.complete_calendar_days or spec.window.history_days} days "
            f"in {spec.window.timezone}",
            "decided by specification equality, not by column names or sample rows",
        )
        groups.append(DuplicateGroup(assets=lines, evidence=evidence))
    return DuplicatesReport(specified=len(ids), groups=tuple(groups), edges=tuple(edges))


def _line(registry: Registry, asset_id: str) -> AssetLine:
    state = registry.state
    asset = state.assets[asset_id]
    cost = state.refresh_costs.get(asset_id)
    return AssetLine(
        asset_id=asset_id,
        spec_ref=asset.spec.ref if asset.spec else "-",
        owner=asset.observed.owner if asset.observed else "",
        consumers=tuple(sorted(state.commitments[c].consumer for c in asset.commitments)),
        refresh_runs=cost.runs if cost else 0,
        refresh_bytes=cost.bytes_scanned if cost else 0,
        refresh_credits=cost.credits if cost else 0.0,
    )


def propose_consolidations(
    registry: Registry,
    report: DuplicatesReport,
    *,
    as_of: datetime,
    policy: ReorganizationPolicy | None = None,
    actor: str = "engine",
) -> list[str]:
    """Open one consolidation proposal per equivalence set. In shadow autonomy nothing is approved.

    Returns:
        Proposal ids opened.
    """
    del as_of  # proposals carry the registry revision; the clock is for later phases
    policy = policy or ReorganizationPolicy()
    opened: list[str] = []
    state = registry.state
    for group in report.groups:
        ids = tuple(a.asset_id for a in group.assets)
        existing = next(
            (
                p
                for p in state.proposals.values()
                if p.kind_of_change == "consolidate" and set(p.affected_assets) == set(ids)
            ),
            None,
        )
        if existing is not None:
            continue
        keeper = max(group.assets, key=lambda a: (len(a.consumers), -a.refresh_credits, a.asset_id))
        proposal_id = f"{keeper.asset_id}_consolidate_p{len(opened) + 1}"
        registry.append(
            Event(
                actor=actor,
                authority=policy.ref,
                causes=tuple(f"asset:{i}" for i in ids),
                payload=ProposalOpened(
                    proposal_id=proposal_id,
                    summary=(
                        f"serve {', '.join(i for i in ids if i != keeper.asset_id)} from one "
                        f"implementation behind their existing interfaces, keeping "
                        f"{keeper.asset_id} as the build"
                    ),
                    changes=(
                        *(
                            f"repoint {a.asset_id} to the shared implementation"
                            for a in group.assets
                            if a.asset_id != keeper.asset_id
                        ),
                        f"keep rebuilding {keeper.asset_id}",
                    ),
                    affected_assets=ids,
                    assumed_revision=registry.revision,
                    kind_of_change="consolidate",
                    alternatives=group.evidence,
                    cost_current_rows=None,
                    cost_proposed_rows=None,
                    evidence_required=tuple(f"verification of {i}" for i in ids),
                    dependents=tuple(i for i in ids if i != keeper.asset_id),
                ),
            )
        )
        # In shadow autonomy the proposal is recorded only; people read it in `veena proposal list`.
        opened.append(proposal_id)
    return opened
