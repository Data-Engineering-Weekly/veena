"""What every backend must do."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from veena.planner.plan import Step

if TYPE_CHECKING:
    from collections.abc import Sequence

    from veena.planner.plan import Plan


class SourceProgress(BaseModel):
    """The newest fact timestamp an implementation has incorporated, per source."""

    model_config = ConfigDict(frozen=True)

    positions: dict[str, datetime]
    row_count: int


class DraftStepRequest(BaseModel):
    """What a drafter receives when a backend cannot render a step itself. Never candidate SQL."""

    model_config = ConfigDict(frozen=True)

    backend: str
    step: Step
    input_relation: str
    input_columns: tuple[str, ...]
    output_columns: tuple[str, ...]
    allowed_tables: tuple[str, ...]


class Backend(Protocol):
    """One engine the coordinator can target."""

    @property
    def name(self) -> str:
        """Short backend name recorded on implementations."""
        ...

    def render(self, plan: Plan, *, as_of: datetime) -> tuple[str, tuple[str, ...]]:
        """Render a plan to one SQL query.

        Returns:
            The SQL and the kinds of any steps a drafter wrote.

        Raises:
            PlanError: A step cannot be rendered and no drafter is available or its draft failed.
        """
        ...

    def materialize(self, implementation_id: str, sql: str, plan: Plan) -> SourceProgress:
        """Create or replace the implementation's table and report source progress."""
        ...

    def publish(self, asset_id: str, implementation_id: str) -> None:
        """Point the asset's interface at an implementation."""
        ...

    def unpublish(self, asset_id: str) -> None:
        """Remove the asset's interface, for a rollback of a first deployment."""
        ...

    @property
    def atomic_publish(self) -> bool:
        """Whether `publish_many` switches every interface in one transaction."""
        ...

    def publish_many(self, switches: dict[str, str]) -> None:
        """Point several interfaces at implementations, atomically when the backend can.

        Raises:
            BackendError: The switch failed; when atomic, nothing changed.
        """
        ...

    def measure_rows_processed(self, plan: Plan, *, as_of: datetime) -> int:
        """Fact rows the plan must attribute and aggregate per refresh.

        For a source-fed plan this is the rows that pass its window. For a provider-fed plan it is
        the pre-aggregated rows that pass its bucket window. Lookups such as the entity source are
        the same either way and are not counted.
        """
        ...

    def source_bounds(self, table: str, timestamp_column: str) -> tuple[datetime, datetime] | None:
        """Oldest and newest timestamps a source holds, or None when empty."""
        ...

    def execute(self, sql: str) -> Sequence[tuple[object, ...]]:
        """Run a read-only query and return its rows."""
        ...
