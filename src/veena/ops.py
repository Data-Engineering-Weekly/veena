"""The operator view: unmet commitments, who they affect, what changed, what comes next.

Everything here is computed from the registry's events. There is no health flag to trust. The
functions return structured records and text; the CLI prints them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import CoordinationError
from veena.models.protocol import input_hash
from veena.registry.events import DraftProduced, Event
from veena.registry.state import ProposalStatus

if TYPE_CHECKING:
    from veena.models.protocol import Drafter
    from veena.registry.registry import Registry
    from veena.registry.state import Deployment, Proposal, RegistryState

Condition = Literal["freshness", "verification", "schema", "cost"]


class UnmetCommitment(BaseModel):
    """One commitment the platform is not keeping, and the event-derived reason."""

    model_config = ConfigDict(frozen=True)

    commitment_id: str
    asset_id: str
    consumer: str
    condition: Condition
    reason: str
    degraded: bool = Field(
        default=False,
        description="The live check failed and no later deployment has activated a fix.",
    )


class Lag(BaseModel):
    """Source lag and incorporation lag for one asset and source, kept apart on purpose.

    Source lag is how far the source itself is behind now. Incorporation lag is how far the asset
    is behind the source. Only the second one is the engine's to fix.
    """

    model_config = ConfigDict(frozen=True)

    asset_id: str
    source: str
    source_position: datetime | None
    incorporated: datetime | None
    source_lag_seconds: int | None
    incorporation_lag_seconds: int | None


class Brake(BaseModel):
    """An active freeze or pin with its age."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["freeze", "pin"]
    asset_id: str
    detail: str
    actor: str
    authority: str
    applied_at: datetime
    age_seconds: int


class Impact(BaseModel):
    """A rollback whose readers have not been assessed yet."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    consumers: tuple[str, ...]
    interval_start: datetime | None
    interval_end: datetime | None
    reason: str


class Degraded(BaseModel):
    """An asset under repair: what consumers read and how far it was verified."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    reads: str | None
    verified_implementation: str | None
    verified_position: dict[str, datetime]


class OperatorView(BaseModel):
    """The four questions in order, then lags, brakes, impact, and degraded assets."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    unmet: tuple[UnmetCommitment, ...]
    affected_consumers: dict[str, tuple[str, ...]]
    recent_deployments: tuple[str, ...]
    proposals_next: tuple[str, ...]
    lags: tuple[Lag, ...]
    brakes: tuple[Brake, ...]
    impact: tuple[Impact, ...]
    degraded: tuple[Degraded, ...]


def unmet_commitments(state: RegistryState, *, as_of: datetime) -> tuple[UnmetCommitment, ...]:
    """Every open commitment that one of the four conditions says is not kept."""
    found: list[UnmetCommitment] = []
    for commitment in sorted(state.commitments.values(), key=lambda c: c.seq):
        if commitment.released:
            continue
        asset = state.assets.get(commitment.asset_id)
        if asset is None:
            continue
        interface = state.interface_of(asset.asset_id)

        for lag in lags_for(state, asset.asset_id):
            if (
                lag.incorporation_lag_seconds is not None
                and lag.incorporation_lag_seconds > commitment.incorporate_within_seconds
            ):
                found.append(
                    UnmetCommitment(
                        commitment_id=commitment.commitment_id,
                        asset_id=asset.asset_id,
                        consumer=commitment.consumer,
                        condition="freshness",
                        reason=(
                            f"{lag.source} reached {_iso(lag.source_position)} but the asset "
                            f"incorporated {_iso(lag.incorporated)}: "
                            f"{lag.incorporation_lag_seconds}s behind, bound "
                            f"{commitment.incorporate_within_seconds}s"
                        ),
                    )
                )

        failed = _latest_failed_verification(state, asset.asset_id, interface)
        if failed is not None:
            seq, verification_id, detail = failed
            later = state.latest_deployment_for(asset.asset_id)
            fixed = later is not None and later.seq > seq
            if not fixed:
                found.append(
                    UnmetCommitment(
                        commitment_id=commitment.commitment_id,
                        asset_id=asset.asset_id,
                        consumer=commitment.consumer,
                        condition="verification",
                        reason=f"{verification_id} failed: {detail}",
                        degraded=True,
                    )
                )

        if commitment.schema_stable and commitment.interface_columns and interface is not None:
            columns = state.implementations[interface].plan.output_columns
            if columns != commitment.interface_columns:
                found.append(
                    UnmetCommitment(
                        commitment_id=commitment.commitment_id,
                        asset_id=asset.asset_id,
                        consumer=commitment.consumer,
                        condition="schema",
                        reason=(
                            f"interface columns are {list(columns)}; the commitment recorded "
                            f"{list(commitment.interface_columns)} as stable"
                        ),
                    )
                )

        if (
            commitment.cost_ceiling_rows is not None
            and asset.rows_processed is not None
            and asset.rows_processed > commitment.cost_ceiling_rows
        ):
            found.append(
                UnmetCommitment(
                    commitment_id=commitment.commitment_id,
                    asset_id=asset.asset_id,
                    consumer=commitment.consumer,
                    condition="cost",
                    reason=(
                        f"last refresh processed {asset.rows_processed} fact rows; the ceiling is "
                        f"{commitment.cost_ceiling_rows}"
                    ),
                )
            )
    del as_of
    return tuple(found)


def lags_for(state: RegistryState, asset_id: str) -> tuple[Lag, ...]:
    """Source lag and incorporation lag per source the asset's implementation reads."""
    asset = state.assets.get(asset_id)
    if asset is None:
        return ()
    # Only sources with a timestamp have a position: those the asset incorporated, plus any the
    # implementation reads that the engine has observed. Entity lookups have neither.
    sources = set(asset.source_progress)
    interface = state.interface_of(asset_id)
    if interface is not None:
        sources |= {
            d.name
            for d in state.implementations[interface].dependencies
            if d.kind == "source" and d.name in state.source_positions
        }
    lags: list[Lag] = []
    for source in sorted(sources):
        observed = state.source_positions.get(source)
        incorporated = asset.source_progress.get(source)
        source_lag = None
        incorporation_lag = None
        if observed is not None:
            source_lag = max(0, int((observed.observed_at - observed.position).total_seconds()))
            if incorporated is not None:
                incorporation_lag = max(0, int((observed.position - incorporated).total_seconds()))
        lags.append(
            Lag(
                asset_id=asset_id,
                source=source,
                source_position=observed.position if observed else None,
                incorporated=incorporated,
                source_lag_seconds=source_lag,
                incorporation_lag_seconds=incorporation_lag,
            )
        )
    return tuple(lags)


def operator_view(state: RegistryState, *, as_of: datetime, recent_days: int = 7) -> OperatorView:
    """Compute the whole view at one instant."""
    unmet = unmet_commitments(state, as_of=as_of)
    unmet_assets = {u.asset_id for u in unmet}
    affected: dict[str, list[str]] = {}
    for u in unmet:
        affected.setdefault(u.consumer, [])
        if u.commitment_id not in affected[u.consumer]:
            affected[u.consumer].append(u.commitment_id)
    since = as_of - timedelta(days=recent_days)
    recent = tuple(
        d.deployment_id
        for d in sorted(state.deployments.values(), key=lambda d: -d.seq)
        if d.effective_at >= since and (not unmet_assets or unmet_assets & set(d.activated))
    )
    proposals = tuple(
        p.proposal_id
        for p in sorted(state.proposals.values(), key=lambda p: p.seq)
        if p.status in {ProposalStatus.OPEN, ProposalStatus.APPROVED}
        and (not unmet_assets or unmet_assets & set(p.affected_assets))
    )
    questions = tuple(
        q.question_id
        for q in sorted(state.questions.values(), key=lambda q: q.seq)
        if q.decision_id is None and (not unmet_assets or q.asset_id in unmet_assets)
    )
    lags = tuple(
        lag
        for asset in sorted(state.assets.values(), key=lambda a: a.asset_id)
        if asset.active_implementation is not None
        for lag in lags_for(state, asset.asset_id)
    )
    brakes = [
        Brake(
            kind="freeze",
            asset_id=f.asset_id,
            detail=("asset and dependents" if f.include_dependents else "asset") + f": {f.reason}",
            actor=f.actor,
            authority=f.authority,
            applied_at=f.applied_at,
            age_seconds=max(0, int((as_of - f.applied_at).total_seconds())),
        )
        for f in sorted(state.freezes.values(), key=lambda f: f.seq)
    ] + [
        Brake(
            kind="pin",
            asset_id=p.asset_id,
            detail=f"to {p.implementation_id}: {p.reason}",
            actor=p.actor,
            authority=p.authority,
            applied_at=p.applied_at,
            age_seconds=max(0, int((as_of - p.applied_at).total_seconds())),
        )
        for p in sorted(state.pins.values(), key=lambda p: p.seq)
    ]
    impact = tuple(
        Impact(
            deployment_id=d.deployment_id,
            consumers=d.affected_consumers,
            interval_start=d.interval_start,
            interval_end=d.interval_end,
            reason=d.rollback_reason,
        )
        for d in sorted(state.deployments.values(), key=lambda d: d.seq)
        if d.rolled_back and not d.impact_acknowledged
    )
    degraded_assets = sorted({u.asset_id for u in unmet if u.degraded})
    degraded: list[Degraded] = []
    for degraded_id in degraded_assets:
        verified = state.last_verified(degraded_id)
        degraded.append(
            Degraded(
                asset_id=degraded_id,
                reads=state.interface_of(degraded_id),
                verified_implementation=verified[0] if verified else None,
                verified_position=dict(verified[1]) if verified else {},
            )
        )
    return OperatorView(
        as_of=as_of,
        unmet=unmet,
        affected_consumers={k: tuple(v) for k, v in affected.items()},
        recent_deployments=recent,
        proposals_next=(*proposals, *questions),
        lags=lags,
        brakes=tuple(brakes),
        impact=impact,
        degraded=tuple(degraded),
    )


def render_view(view: OperatorView, state: RegistryState) -> str:
    """The view as text, four sections first and in order."""
    lines = [f"operator view as of {_iso(view.as_of)}", "", "unmet commitments"]
    if not view.unmet:
        lines.append("  none")
    for u in view.unmet:
        flag = " [degraded]" if u.degraded else ""
        lines.append(f"  {u.commitment_id:<34} {u.condition:<12}{flag} {u.reason}")
    lines += ["", "affected consumers"]
    if not view.affected_consumers:
        lines.append("  none")
    for consumer, commitments in view.affected_consumers.items():
        lines.append(f"  {consumer:<24} {', '.join(commitments)}")
    lines += ["", "recent deployments touching them"]
    if not view.recent_deployments:
        lines.append("  none")
    for deployment_id in view.recent_deployments:
        d = state.deployments[deployment_id]
        status = "rolled back" if d.rolled_back else "active"
        switched = ", ".join(f"{a} -> {i}" for a, i in d.activated.items())
        lines.append(
            f"  {d.deployment_id:<30} {_iso(d.effective_at)} {status:<11} {switched} "
            f"by {d.actor} ({d.authority})"
        )
    lines += ["", "what the engine proposes next"]
    if not view.proposals_next:
        lines.append("  nothing open")
    for ref in view.proposals_next:
        proposal = state.proposals.get(ref)
        if proposal is not None:
            lines.append(
                f"  proposal {proposal.proposal_id} ({proposal.kind_of_change}, "
                f"{proposal.status}): {proposal.summary}"
            )
        else:
            q = state.questions[ref]
            later = state.latest_deployment_for(q.asset_id)
            note = (
                f" [{later.deployment_id} activated after this question]"
                if later is not None and later.seq > q.seq
                else ""
            )
            lines.append(f"  question {q.question_id} ({q.slot or 'general'}){note}: {q.question}")
    if view.degraded:
        lines += ["", "degraded assets"]
        for item in view.degraded:
            position = ", ".join(
                f"{s} {_iso(p)}" for s, p in sorted(item.verified_position.items())
            )
            lines.append(
                f"  {item.asset_id}: consumers read {item.reads or '-'}; last verified "
                f"{item.verified_implementation or '-'} at source position {position or 'unknown'}"
            )
    lines += ["", "lag per asset and source (source lag | incorporation lag)"]
    if not view.lags:
        lines.append("  no active assets")
    for lag in view.lags:
        lines.append(
            f"  {lag.asset_id:<26} {lag.source:<18} {_seconds(lag.source_lag_seconds):>10} | "
            f"{_seconds(lag.incorporation_lag_seconds):>10}"
        )
    lines += ["", "active brakes"]
    if not view.brakes:
        lines.append("  none")
    for b in view.brakes:
        lines.append(
            f"  {b.kind:<6} {b.asset_id:<26} age {_seconds(b.age_seconds):>8} "
            f"by {b.actor} ({b.authority}) {b.detail}"
        )
    if view.impact:
        lines += ["", "assess impact"]
        for i in view.impact:
            lines.append(
                f"  {i.deployment_id}: {', '.join(i.consumers) or 'no committed consumer'} "
                f"may have read rolled-back data between {_iso(i.interval_start)} and "
                f"{_iso(i.interval_end)} ({i.reason}); acknowledge with "
                f"`veena ops ack {i.deployment_id}`"
            )
    return "\n".join(lines)


# ---- trail ----


class TrailSection(BaseModel):
    """One section of a trail: a title and lines that each carry actor and authority."""

    model_config = ConfigDict(frozen=True)

    title: str
    lines: tuple[str, ...]


class Trail(BaseModel):
    """Deployment, proposal, verification, specification, decisions, in that order."""

    model_config = ConfigDict(frozen=True)

    commitment_id: str
    asset_id: str
    sections: tuple[TrailSection, ...]

    def render(self) -> str:
        """Plain text, one section after another."""
        out: list[str] = []
        for section in self.sections:
            out.append(section.title)
            out.extend(f"  {line}" for line in section.lines)
        return "\n".join(out)


class TrailNarrationRequest(BaseModel):
    """What the drafter receives: the structured trail, never anything else."""

    model_config = ConfigDict(frozen=True)

    commitment_id: str
    asset_id: str
    sections: tuple[TrailSection, ...]


def trail(state: RegistryState, commitment_id: str) -> Trail:
    """Follow one commitment back through the records that explain what it reads.

    Raises:
        CoordinationError: The commitment is unknown.
    """
    commitment = state.commitments.get(commitment_id)
    if commitment is None:
        msg = f"unknown commitment {commitment_id!r}"
        raise CoordinationError(msg)
    asset_id = commitment.asset_id
    asset = state.assets[asset_id]
    sections: list[TrailSection] = []

    deployment = _latest_deployment_touching(state, asset_id)
    if deployment is None:
        sections.append(TrailSection(title="deployment", lines=("none yet",)))
        proposal: Proposal | None = None
    else:
        status = "rolled back" if deployment.rolled_back else "active"
        lines = [
            f"{deployment.deployment_id} ({status}) at {_iso(deployment.effective_at)} by "
            f"{deployment.actor} ({deployment.authority}); assumed revision "
            f"{deployment.assumed_revision}",
            *(
                f"{a} -> {i} (replaced {deployment.replaced.get(a) or 'nothing'})"
                for a, i in deployment.activated.items()
            ),
        ]
        if deployment.rolled_back:
            lines.append(f"rolled back: {deployment.rollback_reason}")
        sections.append(TrailSection(title="deployment", lines=tuple(lines)))
        proposal = state.proposals.get(deployment.proposal_id)

    if proposal is None:
        sections.append(TrailSection(title="proposal", lines=("none",)))
    else:
        lines = [
            f"{proposal.proposal_id} ({proposal.kind_of_change}, {proposal.status}) by "
            f"{proposal.actor} ({proposal.authority}): {proposal.summary}",
            *(f"change: {c}" for c in proposal.changes),
        ]
        if proposal.reason:
            lines.append(f"decided: {proposal.reason}")
        sections.append(TrailSection(title="proposal", lines=tuple(lines)))

    lines = []
    if deployment is not None:
        vid = deployment.verifications.get(asset_id, deployment.verification_id)
        v = state.verifications.get(vid)
        if v is not None:
            lines.append(
                f"{v.verification_id} ({'passed' if v.passed else 'failed'}, grade {v.grade}) by "
                f"{v.actor} ({v.authority}): {sum(c.passed for c in v.checks)}/{len(v.checks)} "
                "checks"
            )
    live = state.latest_live_verification(asset_id)
    if live is not None:
        lines.append(
            f"{live.verification_id} live ({'passed' if live.passed else 'failed'}) at "
            f"{_iso(live.as_of)} by {live.actor} ({live.authority})"
        )
        lines.extend(f"FAIL {c.name}: {c.detail}" for c in live.checks if not c.passed)
    sections.append(TrailSection(title="verification", lines=tuple(lines) or ("none",)))

    if asset.spec is None:
        sections.append(TrailSection(title="specification", lines=("none",)))
    else:
        s = asset.spec
        lines = [
            f"{s.ref} resolved by {asset.spec_actor or '?'} ({asset.spec_authority or '?'}): "
            f"one row per {', '.join(str(k) for k in s.grain.keys)}, identity {s.identity_policy}",
            *(f"{key} <- {origin}" for key, origin in s.provenance.items()),
        ]
        sections.append(TrailSection(title="specification", lines=tuple(lines)))

    lines = []
    for q in sorted(state.questions.values(), key=lambda q: q.seq):
        if q.asset_id != asset_id or q.decision_id is None:
            continue
        d = state.decisions[q.decision_id]
        lines.append(
            f"{d.decision_id} on {q.slot or 'general'}: {d.selected} by {d.actor} "
            f"({d.authority}), scope {d.scope}: {d.rationale or 'no rationale'}"
        )
    for key, origin in asset.spec.provenance.items() if asset.spec else ():
        if origin.origin_kind == "decision":
            lines.append(f"{key} set by {origin}")
    sections.append(TrailSection(title="decisions", lines=tuple(lines) or ("none recorded",)))
    return Trail(commitment_id=commitment_id, asset_id=asset_id, sections=tuple(sections))


def narrate(
    registry: Registry,
    trail_record: Trail,
    drafter: Drafter,
    *,
    actor: str = "engine",
    authority: str = "policy:narration@1",
) -> str:
    """Have the drafter write the narration an on-call engineer reads, and record the draft.

    The structured trail stays the source of truth; the text is stored beside it as a
    `DraftProduced` event and never replaces a section.
    """
    request = TrailNarrationRequest(
        commitment_id=trail_record.commitment_id,
        asset_id=trail_record.asset_id,
        sections=trail_record.sections,
    )
    text = drafter.draft(request)
    model_id = str(getattr(drafter, "model_id", type(drafter).__name__))
    registry.append(
        Event(
            actor=actor,
            authority=authority,
            causes=(f"commitment:{trail_record.commitment_id}",),
            payload=DraftProduced(
                site="narrate_trail",
                asset_id=trail_record.asset_id,
                model_id=model_id,
                input_hash=input_hash(request),
                text=text,
            ),
        )
    )
    return text


# ---- helpers ----


def _latest_failed_verification(
    state: RegistryState, asset_id: str, interface: str | None
) -> tuple[int, str, str] | None:
    newest: tuple[int, str, str, bool] | None = None
    for live in state.live_verifications.values():
        if live.asset_id == asset_id and (newest is None or live.seq > newest[0]):
            detail = "; ".join(f"{c.name}: {c.detail}" for c in live.checks if not c.passed)
            newest = (live.seq, live.verification_id, detail, live.passed)
    if newest is None and interface is not None:
        for v in state.verifications.values():
            if (
                v.implementation_id == interface
                and not v.replay
                and (newest is None or v.seq > newest[0])
            ):
                detail = "; ".join(f"{c.name}: {c.detail}" for c in v.checks if not c.passed)
                newest = (v.seq, v.verification_id, detail, v.passed)
    if newest is None or newest[3]:
        return None
    return newest[0], newest[1], newest[2]


def _latest_deployment_touching(state: RegistryState, asset_id: str) -> Deployment | None:
    candidates = [d for d in state.deployments.values() if asset_id in d.activated]
    return max(candidates, key=lambda d: d.seq) if candidates else None


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "unknown"


def _seconds(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 86400:
        return f"{value / 86400:.1f}d"
    if value >= 3600:
        return f"{value / 3600:.1f}h"
    return f"{value}s"


__all__ = [
    "Brake",
    "Degraded",
    "Impact",
    "Lag",
    "OperatorView",
    "Trail",
    "TrailNarrationRequest",
    "TrailSection",
    "UnmetCommitment",
    "lags_for",
    "narrate",
    "operator_view",
    "render_view",
    "trail",
    "unmet_commitments",
]
