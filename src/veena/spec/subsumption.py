"""Decide whether one asset's spec can serve another's.

The check works on specs only. It never looks at SQL or sample data, because similar
columns and matching samples do not prove the same meaning.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from veena.spec.model import AssetSpec, TimeBucket


class Verdict(StrEnum):
    """Outcome of a subsumption check."""

    EQUAL = "equal"
    SERVES = "serves"
    SERVES_WITH = "serves_with"
    CANNOT = "cannot"


class Operation(StrEnum):
    """Work the planner must add on top of the provider to satisfy the consumer."""

    ROLLUP = "rollup"
    WINDOW = "window"
    POPULATION_JOIN = "population_join"
    FILTER = "filter"


class SubsumptionResult(BaseModel):
    """Verdict plus the operations needed or the reasons it cannot serve."""

    model_config = ConfigDict(frozen=True)

    provider: str
    consumer: str
    verdict: Verdict
    operations: tuple[Operation, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def can_serve(self) -> bool:
        """Whether the provider can serve the consumer at all."""
        return self.verdict is not Verdict.CANNOT


def check(provider: AssetSpec, consumer: AssetSpec) -> SubsumptionResult:
    """Check whether `provider` can serve every row and measure `consumer` asks for.

    Args:
        provider: The spec of an existing or proposed asset.
        consumer: The spec a consumer needs.

    Returns:
        The verdict, the operations the planner must add, and every blocking reason found.
    """
    reasons: list[str] = []
    operations: set[Operation] = set()

    _check_fixed_fields(provider, consumer, reasons)
    needs_aggregation = _check_grain(provider, consumer, reasons, operations)
    _check_time(provider, consumer, reasons, operations)
    _check_measures(provider, consumer, reasons, needs_aggregation or bool(operations))
    _check_filters(provider, consumer, reasons, operations)
    _check_population(provider, consumer, operations)

    if reasons:
        verdict = Verdict.CANNOT
    elif operations:
        verdict = Verdict.SERVES_WITH
    elif _same_meaning(provider, consumer):
        verdict = Verdict.EQUAL
    else:
        verdict = Verdict.SERVES
    return SubsumptionResult(
        provider=provider.ref,
        consumer=consumer.ref,
        verdict=verdict,
        operations=tuple(sorted(operations)),
        reasons=tuple(reasons),
    )


def _check_fixed_fields(provider: AssetSpec, consumer: AssetSpec, reasons: list[str]) -> None:
    if provider.population.entity != consumer.population.entity:
        reasons.append(
            "population entity differs: "
            f"{provider.population.entity} vs {consumer.population.entity}"
        )
    if provider.identity_policy != consumer.identity_policy:
        reasons.append(
            f"identity policy differs: {provider.identity_policy} vs {consumer.identity_policy}"
        )
    if provider.service.access_policy != consumer.service.access_policy:
        reasons.append(
            "access policy differs: "
            f"{provider.service.access_policy} vs {consumer.service.access_policy}"
        )
    if provider.window.timezone != consumer.window.timezone:
        reasons.append(
            f"timezone differs: {provider.window.timezone} vs {consumer.window.timezone}"
        )
    if provider.service.incorporate_within > consumer.service.incorporate_within:
        reasons.append(
            "freshness too slow: provider incorporates within "
            f"{int(provider.service.incorporate_within.total_seconds())}s, consumer needs "
            f"{int(consumer.service.incorporate_within.total_seconds())}s"
        )


def _check_grain(
    provider: AssetSpec,
    consumer: AssetSpec,
    reasons: list[str],
    operations: set[Operation],
) -> bool:
    provider_keys = set(provider.grain.keys)
    consumer_keys = set(consumer.grain.keys)
    missing = consumer_keys - provider_keys
    if missing:
        reasons.append(f"provider lacks grain keys {sorted(str(k) for k in missing)}")
        return False
    if provider_keys != consumer_keys:
        operations.add(Operation.ROLLUP)
        return True
    return False


def _check_time(
    provider: AssetSpec,
    consumer: AssetSpec,
    reasons: list[str],
    operations: set[Operation],
) -> None:
    p_bucket, c_bucket = provider.grain.time_bucket, consumer.grain.time_bucket
    p_window, c_window = provider.window, consumer.window

    if c_bucket is TimeBucket.NONE:
        days = c_window.complete_calendar_days
        if p_bucket is TimeBucket.NONE:
            if p_window.complete_calendar_days != days:
                reasons.append(
                    f"window differs: provider covers {p_window.complete_calendar_days} complete "
                    f"days, consumer needs {days}"
                )
        elif p_bucket is TimeBucket.DAY:
            operations.add(Operation.WINDOW)
            if p_window.span_days < (days or 0):
                reasons.append(
                    f"insufficient history: provider retains {p_window.span_days} days, "
                    f"consumer needs {days}"
                )
        else:
            reasons.append(
                f"time bucket {p_bucket} cannot be cut on complete calendar day boundaries"
            )
        return

    if p_bucket is TimeBucket.NONE:
        reasons.append("a windowed provider cannot serve a time-bucketed consumer")
        return
    if p_bucket is not c_bucket:
        if p_bucket is TimeBucket.DAY and c_bucket is TimeBucket.MONTH:
            operations.add(Operation.ROLLUP)
        else:
            reasons.append(f"time bucket {p_bucket} cannot be rolled up to {c_bucket}")
    if p_window.span_days < c_window.span_days:
        reasons.append(
            f"insufficient history: provider retains {p_window.span_days} days, "
            f"consumer needs {c_window.span_days}"
        )


def _check_measures(
    provider: AssetSpec,
    consumer: AssetSpec,
    reasons: list[str],
    needs_aggregation: bool,
) -> None:
    offered = {(m.calculation, m.aggregation) for m in provider.measures}
    for measure in consumer.measures:
        key = (measure.calculation, measure.aggregation)
        if key not in offered:
            reasons.append(
                f"provider lacks measure {measure.name} "
                f"({measure.aggregation} of {measure.calculation})"
            )
        elif needs_aggregation and not measure.aggregation.reaggregable:
            reasons.append(
                f"measure {measure.name} uses {measure.aggregation}, which cannot be "
                "re-aggregated from a finer grain"
            )


def _check_filters(
    provider: AssetSpec,
    consumer: AssetSpec,
    reasons: list[str],
    operations: set[Operation],
) -> None:
    provider_filters = set(provider.filters)
    consumer_filters = set(consumer.filters)
    dropped = provider_filters - consumer_filters
    if dropped:
        reasons.append(
            "provider filters out rows the consumer needs: "
            + ", ".join(f"{f.attribute} {f.op} {list(f.values)}" for f in sorted(dropped, key=str))
        )
    extra = consumer_filters - provider_filters
    if extra:
        operations.add(Operation.FILTER)
        provider_keys = set(provider.grain.keys)
        for flt in extra:
            if flt.attribute not in provider_keys:
                reasons.append(
                    f"consumer filters on {flt.attribute}, which the provider does not carry"
                )


def _check_population(provider: AssetSpec, consumer: AssetSpec, operations: set[Operation]) -> None:
    if consumer.population.include_inactive and not provider.population.include_inactive:
        operations.add(Operation.POPULATION_JOIN)
    elif provider.population.include_inactive and not consumer.population.include_inactive:
        operations.add(Operation.FILTER)


def _same_meaning(provider: AssetSpec, consumer: AssetSpec) -> bool:
    return (
        provider.population == consumer.population
        and provider.grain == consumer.grain
        and set(provider.measures) == set(consumer.measures)
        and provider.window == consumer.window
        and set(provider.filters) == set(consumer.filters)
        and provider.identity_policy == consumer.identity_policy
        and provider.service == consumer.service
    )
