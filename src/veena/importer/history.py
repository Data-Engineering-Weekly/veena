"""Import warehouse query history: who reads what, how often, and what rebuilds cost."""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from veena.errors import VeenaError
from veena.policy import WorkspaceDefaults
from veena.registry.events import (
    CommitmentProposed,
    Event,
    RefreshCostObserved,
    UnmanagedReadObserved,
    UsageObserved,
)

if TYPE_CHECKING:
    from pathlib import Path

    from veena.models.protocol import Classification, Classifier
    from veena.registry.registry import Registry


class QueryRecord(BaseModel):
    """One row as Snowflake's QUERY_HISTORY view reports it, trimmed to what the importer reads.

    A naive `start_time` is read as UTC, which is how Snowflake exports it.
    """

    model_config = ConfigDict(frozen=True)

    query_id: str
    user_name: str
    role_name: str
    warehouse_name: str
    start_time: datetime
    total_elapsed_ms: int
    bytes_scanned: int
    credits_used_cloud_services: float
    query_type: str
    query_text: str

    @field_validator("start_time")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class ReaderClass(StrEnum):
    """How a reader uses an asset."""

    MANAGED = "managed"
    SCHEDULED = "scheduled"
    AD_HOC = "ad_hoc"
    UNMANAGED = "unmanaged"


class ReaderContext(BaseModel):
    """What the reader classifier sees. Cadence statistics, never query results."""

    model_config = ConfigDict(frozen=True)

    slot: str = "reader_class"
    consumer: str
    role: str
    warehouse: str
    asset_id: str
    layer: str
    internal: bool
    queries: int
    runs_per_day: float
    distinct_hours: int
    median_interval_minutes: float
    sample_query: str
    option_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "managed": "a service reading a published asset steadily through the day",
            "scheduled": "a job reading at fixed times, often once a day",
            "ad_hoc": "a person exploring, irregularly and in business hours",
            "unmanaged": "a reader of an internal relation that is not a published interface",
        }
    )


class HistoryReport(BaseModel):
    """What one history import recorded."""

    model_config = ConfigDict(frozen=True)

    queries: int
    matched: int
    usage_rows: int
    refresh_cost_rows: int
    unmanaged_reads: int
    commitments_proposed: int


_PART = r'"?[A-Za-z0-9_$]+"?'
_NAME = rf"(?:{_PART}\.){{1,2}}{_PART}"
_TABLE = re.compile(rf"\b(?:from|join)\s+({_NAME})", re.I)
_BUILD = re.compile(
    rf"\b(?:create\s+(?:or\s+replace\s+)?(?:transient\s+|temporary\s+|temp\s+)*table\s+"
    rf"|merge\s+into\s+|insert\s+(?:overwrite\s+)?into\s+)({_NAME})",
    re.I,
)
_TEMPORARY = re.compile(r"\b(temporary|temp)\s+table\b|_dbt_tmp\b", re.I)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:[^']|'')*'")
_INTERNAL_TOKENS = frozenset({"staging", "stg", "intermediate", "int", "internal"})


def load_history(path: Path) -> list[QueryRecord]:
    """Read a JSONL file of query history rows.

    Raises:
        VeenaError: The file cannot be read or a row does not parse; the message names the line.
    """
    rows: list[QueryRecord] = []
    try:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(QueryRecord.model_validate(json.loads(line)))
            except ValueError as exc:
                msg = f"query history {path} line {number}: {exc}"
                raise VeenaError(msg) from exc
    except OSError as exc:
        msg = f"cannot read query history {path}: {exc}"
        raise VeenaError(msg) from exc
    return rows


def referenced_relations(query_text: str) -> set[str]:
    """Fully qualified relation names a query reads, normalized to lower case without quotes."""
    cleaned = _STRING.sub("''", _COMMENT.sub(" ", query_text))
    return {_normalize(t) for t in _TABLE.findall(cleaned)}


def built_relation(query_text: str) -> str | None:
    """The relation a statement builds, or None for a read or a temporary table."""
    cleaned = _STRING.sub("''", _COMMENT.sub(" ", query_text))
    match = _BUILD.search(cleaned)
    if match is None:
        return None
    if _TEMPORARY.search(cleaned[: match.end()]) or "_dbt_tmp" in match.group(1).lower():
        return None
    return _normalize(match.group(1))


def _normalize(name: str) -> str:
    return ".".join(part.strip('"').lower() for part in name.split("."))


class RulesReaderClassifier:
    """Deterministic cadence rules: the offline baseline for the reader site.

    It is honest about what it is. Steady round-the-clock service reads are managed, reads at one
    or two fixed hours are scheduled, business-hours irregular reads by people are ad hoc, and any
    read of an internal relation is unmanaged.
    """

    model_id = "rules:reader"

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Apply the cadence rules."""
        from veena.models.protocol import Classification

        if not isinstance(context, ReaderContext):
            members = list(options)
            return Classification(
                value=members[0], probability=round(1 / len(members), 4), model_id=self.model_id
            )
        if context.internal:
            value, p = "unmanaged", 0.95
        elif context.runs_per_day >= 12 and context.distinct_hours >= 18:
            value, p = "managed", 0.9
        elif context.runs_per_day >= 0.5 and context.distinct_hours <= 3:
            value, p = "scheduled", 0.85
        elif context.role.upper() in {"ANALYST", "SYSADMIN"} or context.runs_per_day < 0.5:
            value, p = "ad_hoc", 0.8
        else:
            value, p = "managed", 0.55
        rest = [m for m in options if m.value != value]
        share = round((1 - p) / len(rest), 4) if rest else 0.0
        return Classification(
            value=options(value),
            probability=p,
            alternatives=tuple((m, share) for m in rest),
            model_id=self.model_id,
        )


def is_internal_layer(layer: str, tags: tuple[str, ...]) -> bool:
    """Whether a dbt schema name or tag marks the relation as not a published interface."""
    tokens = set(layer.lower().replace("-", "_").split("_"))
    return bool(tokens & _INTERNAL_TOKENS) or "internal" in tags


def import_history(
    registry: Registry,
    records: list[QueryRecord],
    *,
    classifier: Classifier | None = None,
    defaults: WorkspaceDefaults | None = None,
    propose_above: float = 0.8,
    actor: str = "importer:history",
) -> HistoryReport:
    """Record usage, refresh cost, unmanaged reads, and proposed commitments from history.

    Builds (`create table`, `merge into`, `insert into` on a known relation) count toward
    refresh cost and are not reads. Temporary tables and dbt's `_dbt_tmp` relations are skipped.
    Two-part names resolve against the database when every observed relation shares one.
    """
    classifier = classifier or RulesReaderClassifier()
    defaults = defaults or WorkspaceDefaults()
    state = registry.state
    by_relation: dict[str, str] = {}
    databases: set[str] = set()
    for a in state.assets.values():
        if a.observed is not None and a.observed.relation:
            relation = a.observed.relation.lower().replace('"', "")
            by_relation[relation] = a.asset_id
            if relation.count(".") == 2:
                databases.add(relation.split(".")[0])
    default_database = next(iter(databases)) if len(databases) == 1 else None

    def resolve(name: str) -> str | None:
        if name in by_relation:
            return by_relation[name]
        if default_database and name.count(".") == 1:
            return by_relation.get(f"{default_database}.{name}")
        return None

    if not records:
        return HistoryReport(
            queries=0,
            matched=0,
            usage_rows=0,
            refresh_cost_rows=0,
            unmanaged_reads=0,
            commitments_proposed=0,
        )
    window_start = min(r.start_time for r in records)
    window_end = max(r.start_time for r in records)
    days = max((window_end - window_start).total_seconds() / 86400, 1.0)

    reads: dict[tuple[str, str], list[QueryRecord]] = defaultdict(list)
    builds: dict[str, list[QueryRecord]] = defaultdict(list)
    matched = 0
    for record in records:
        target = built_relation(record.query_text)
        if target is not None or record.query_type.upper().startswith(
            ("CREATE", "MERGE", "INSERT")
        ):
            asset_id = resolve(target) if target else None
            if asset_id:
                builds[asset_id].append(record)
                matched += 1
            continue
        hit = False
        for table in referenced_relations(record.query_text):
            asset_id = resolve(table)
            if asset_id:
                reads[(asset_id, record.user_name)].append(record)
                hit = True
        matched += int(hit)

    authority = "import:query_history"
    refresh_rows = 0
    for asset_id, rows in builds.items():
        registry.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"asset:{asset_id}",),
                payload=RefreshCostObserved(
                    asset_id=asset_id,
                    runs=len(rows),
                    bytes_scanned=sum(r.bytes_scanned for r in rows),
                    credits=round(sum(r.credits_used_cloud_services for r in rows), 6),
                    window_start=window_start,
                    window_end=window_end,
                ),
            )
        )
        refresh_rows += 1

    usage_rows = unmanaged = proposed = 0
    for (asset_id, user), rows in sorted(reads.items()):
        asset = state.assets[asset_id]
        observed = asset.observed
        layer = observed.layer if observed else ""
        internal = is_internal_layer(layer, observed.tags if observed else ())
        times = sorted(r.start_time for r in rows)
        intervals = [(b - a).total_seconds() / 60 for a, b in pairwise(times) if b > a]
        context = ReaderContext(
            consumer=f"user:{user.lower()}",
            role=rows[0].role_name,
            warehouse=rows[0].warehouse_name,
            asset_id=asset_id,
            layer=layer,
            internal=internal,
            queries=len(rows),
            runs_per_day=round(len(rows) / days, 3),
            distinct_hours=len({t.hour for t in times}),
            median_interval_minutes=round(statistics.median(intervals), 1) if intervals else 0.0,
            sample_query=rows[0].query_text[:200],
        )
        verdict = classifier.classify(context, ReaderClass)
        registry.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"asset:{asset_id}",),
                payload=UsageObserved(
                    asset_id=asset_id,
                    consumer=context.consumer,
                    role=context.role,
                    queries=len(rows),
                    bytes_scanned=sum(r.bytes_scanned for r in rows),
                    credits=round(sum(r.credits_used_cloud_services for r in rows), 6),
                    first_seen=times[0],
                    last_seen=times[-1],
                    runs_per_day=context.runs_per_day,
                    distinct_hours=context.distinct_hours,
                    reader_class=verdict.value.value,
                    reader_probability=verdict.probability,
                ),
            )
        )
        usage_rows += 1
        if internal:
            registry.append(
                Event(
                    actor=actor,
                    authority=authority,
                    causes=(f"asset:{asset_id}",),
                    payload=UnmanagedReadObserved(
                        asset_id=asset_id,
                        consumer=context.consumer,
                        queries=len(rows),
                        last_seen=times[-1],
                        reason=(
                            f"{asset_id} is in the {layer or 'internal'} layer and is not a "
                            "published interface"
                        ),
                    ),
                )
            )
            unmanaged += 1
            continue
        has_commitment = any(
            state.commitments[c].consumer == context.consumer for c in asset.commitments
        )
        if (
            verdict.value in {ReaderClass.MANAGED, ReaderClass.SCHEDULED}
            and verdict.probability >= propose_above
            and not has_commitment
        ):
            suggested = (
                int(context.median_interval_minutes * 60)
                if context.median_interval_minutes
                else defaults.incorporate_within_seconds
            )
            registry.append(
                Event(
                    actor=actor,
                    authority=authority,
                    causes=(f"asset:{asset_id}",),
                    payload=CommitmentProposed(
                        asset_id=asset_id,
                        consumer=context.consumer,
                        reader_class=verdict.value.value,
                        probability=verdict.probability,
                        suggested_incorporate_within_seconds=max(60, min(suggested, 86400)),
                        rationale=(
                            f"{context.consumer} read {asset_id} {len(rows)} times over "
                            f"{days:.0f} days ({context.runs_per_day} per day across "
                            f"{context.distinct_hours} hours) and holds no commitment"
                        ),
                    ),
                )
            )
            proposed += 1
    return HistoryReport(
        queries=len(records),
        matched=matched,
        usage_rows=usage_rows,
        refresh_cost_rows=refresh_rows,
        unmanaged_reads=unmanaged,
        commitments_proposed=proposed,
    )
