"""Graded retirement: stop refreshing, replace with a view, drop the table, retire.

Retirement weighs commitments, observed usage, retention policy, and reconstruction cost. A
query count alone never retires anything. Every step is an event, and the specification,
decisions, and history stay readable after the physical work is gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.errors import CoordinationError
from veena.policy import RetentionPolicy
from veena.registry.events import AssetRetired, Event, RetirementStep, RetirementStepTaken
from veena.registry.state import AssetStatus

if TYPE_CHECKING:
    from veena.backends.protocol import Backend
    from veena.registry.registry import Registry
    from veena.registry.state import Asset, RegistryState


class RetirementDecision(BaseModel):
    """Whether an asset may take its next retirement step now, and why or why not."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    eligible: bool
    next_step: RetirementStep | None
    reasons: tuple[str, ...]


class StepTaken(BaseModel):
    """One step the retirer carried out."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    step: RetirementStep | None
    detail: str

    @property
    def label(self) -> str:
        """The step name, or `retire` for the final event."""
        return self.step.value if self.step else "retire"


def consider(
    state: RegistryState,
    asset_id: str,
    *,
    as_of: datetime,
    policy: RetentionPolicy | None = None,
) -> RetirementDecision:
    """Decide the next step for one asset without taking it.

    Raises:
        CoordinationError: The asset is unknown.
    """
    policy = policy or RetentionPolicy()
    asset = state.assets.get(asset_id)
    if asset is None:
        msg = f"unknown asset {asset_id!r}"
        raise CoordinationError(msg)
    reasons: list[str] = []
    if asset.status is AssetStatus.RETIRED:
        return RetirementDecision(
            asset_id=asset_id, eligible=False, next_step=None, reasons=("already retired",)
        )
    if asset.commitments:
        consumers = ", ".join(state.commitments[c].consumer for c in asset.commitments)
        reasons.append(f"{len(asset.commitments)} open commitment(s): {consumers}")
    dependents = state.dependents_of(asset_id)
    if dependents:
        reasons.append(f"read by {', '.join(dependents)}")
    if asset_id in state.pins:
        reasons.append("pinned")
    freeze = state.frozen_by(asset_id)
    if freeze is not None:
        reasons.append(f"frozen since {freeze.applied_at.isoformat(timespec='seconds')}")
    quiet_since = as_of - timedelta(days=policy.quiet_days)
    for (aid, consumer), usage in state.usage.items():
        if aid == asset_id and usage.last_seen >= quiet_since:
            reasons.append(
                f"{consumer} read it {usage.queries} time(s), last at "
                f"{usage.last_seen.isoformat(timespec='seconds')}, inside {policy.quiet_days} "
                "quiet days"
            )
    if reasons:
        return RetirementDecision(
            asset_id=asset_id, eligible=False, next_step=None, reasons=tuple(reasons)
        )

    next_step = _next_step(state, asset)
    if next_step is RetirementStep.DROP_MATERIALIZATION:
        waited = _idle_since(state, asset)
        keep = policy.keep_materialization_days
        if asset.rows_processed is not None and asset.rows_processed >= policy.expensive_rows:
            keep = policy.keep_expensive_days
            reasons.append(
                f"rebuilding costs {asset.rows_processed} fact rows, so {policy.ref} keeps the "
                f"table {keep} days"
            )
        if waited is not None and as_of - waited < timedelta(days=keep):
            ready = (waited + timedelta(days=keep)).isoformat(timespec="seconds")
            reasons.append(f"{policy.ref} keeps the materialization until {ready}")
            return RetirementDecision(
                asset_id=asset_id, eligible=False, next_step=next_step, reasons=tuple(reasons)
            )
    return RetirementDecision(
        asset_id=asset_id, eligible=True, next_step=next_step, reasons=tuple(reasons)
    )


class Retirer:
    """Takes graded retirement steps against one registry and one backend."""

    def __init__(
        self,
        registry: Registry,
        backend: Backend,
        *,
        policy: RetentionPolicy | None = None,
        actor: str = "engine",
        authority: str = "policy:retention@1",
    ) -> None:
        """Bind the registry that records steps and the backend that does the physical work."""
        self._registry = registry
        self._backend = backend
        self._policy = policy or RetentionPolicy()
        self._actor = actor
        self._authority = authority

    def plan(self, *, as_of: datetime) -> tuple[RetirementDecision, ...]:
        """Consider every asset that still has physical work or could retire."""
        state = self._registry.state
        return tuple(
            consider(state, a.asset_id, as_of=as_of, policy=self._policy)
            for a in sorted(state.assets.values(), key=lambda a: a.asset_id)
            if a.status in {AssetStatus.ACTIVE, AssetStatus.DORMANT}
        )

    def advance(self, *, as_of: datetime, asset_id: str | None = None) -> tuple[StepTaken, ...]:
        """Take at most one step per eligible asset, dependents before what they read.

        Each call moves the graph one step. Calling again takes the next step where policy
        allows, so a loop that stops when nothing changes retires everything that may retire.
        """
        state = self._registry.state
        candidates = [
            a
            for a in state.assets.values()
            if a.status in {AssetStatus.ACTIVE, AssetStatus.DORMANT}
            and (asset_id is None or a.asset_id == asset_id)
        ]
        candidates.sort(key=lambda a: (bool(state.dependents_of(a.asset_id)), a.asset_id))
        taken: list[StepTaken] = []
        for asset in candidates:
            decision = consider(
                self._registry.state, asset.asset_id, as_of=as_of, policy=self._policy
            )
            if not decision.eligible:
                continue
            taken.append(self._take(self._registry.state.assets[asset.asset_id], decision))
        return tuple(taken)

    def _take(self, asset: Asset, decision: RetirementDecision) -> StepTaken:
        step = decision.next_step
        reason = "; ".join(decision.reasons) or "no commitment, reader, or dependent remains"
        impl_id = asset.active_implementation
        if step is None:
            self._append(AssetRetired(asset_id=asset.asset_id, reason=reason), asset.asset_id)
            return StepTaken(
                asset_id=asset.asset_id,
                step=None,
                detail="retired; specification and history stay readable",
            )
        self._append(
            RetirementStepTaken(
                asset_id=asset.asset_id, step=step, reason=reason, implementation_id=impl_id
            ),
            asset.asset_id,
        )
        # Physical work follows the record, so a crash in between leaves a step to redo.
        if impl_id is None:
            return StepTaken(asset_id=asset.asset_id, step=step, detail="nothing physical")
        match step:
            case RetirementStep.STOP_REFRESH:
                return StepTaken(
                    asset_id=asset.asset_id,
                    step=step,
                    detail=f"{impl_id} keeps its rows and is no longer refreshed",
                )
            case RetirementStep.REPLACE_WITH_VIEW:
                impl = self._registry.state.implementations[impl_id]
                self._backend.dematerialize(impl_id, impl.sql)
                return StepTaken(
                    asset_id=asset.asset_id,
                    step=step,
                    detail=f"{impl_id} is now a view over {', '.join(asset.depends_on)}",
                )
            case RetirementStep.DROP_MATERIALIZATION:
                # Replaced implementations were kept for rollback; dormant means all of them go.
                for old in asset.implementations:
                    if old == impl_id and asset.retirement_step is RetirementStep.REPLACE_WITH_VIEW:
                        self._backend.drop_view(old)
                    else:
                        self._backend.drop(old)
                self._backend.unpublish(asset.asset_id)
                return StepTaken(
                    asset_id=asset.asset_id,
                    step=step,
                    detail=(
                        f"{len(asset.implementations)} implementation(s) and the interface are "
                        "gone; the asset is dormant"
                    ),
                )

    def _append(self, payload: RetirementStepTaken | AssetRetired, asset_id: str) -> None:
        self._registry.append(
            Event(
                actor=self._actor,
                authority=self._authority,
                causes=(f"asset:{asset_id}",),
                payload=payload,
            )
        )


def _next_step(state: RegistryState, asset: Asset) -> RetirementStep | None:
    if asset.status is AssetStatus.DORMANT or asset.active_implementation is None:
        return None
    current = asset.retirement_step
    if current is None:
        return RetirementStep.STOP_REFRESH
    if current is RetirementStep.STOP_REFRESH and asset.depends_on:
        provider = state.assets.get(asset.depends_on[0])
        if provider is not None and provider.active_implementation is not None:
            return RetirementStep.REPLACE_WITH_VIEW
    return RetirementStep.DROP_MATERIALIZATION


def _idle_since(state: RegistryState, asset: Asset) -> datetime | None:
    """When the asset last lost a commitment, or when its refresh stopped if it never had one."""
    releases = [
        c.released_at
        for c in state.commitments.values()
        if c.asset_id == asset.asset_id and c.released_at is not None
    ]
    if releases:
        return max(releases)
    return asset.last_refresh_as_of


__all__ = ["RetirementDecision", "Retirer", "StepTaken", "consider"]
