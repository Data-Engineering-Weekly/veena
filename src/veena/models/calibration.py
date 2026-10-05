"""Measure whether a classifier's stated probabilities match its observed accuracy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path


class CalibrationRecord(BaseModel):
    """One labeled prediction."""

    model_config = ConfigDict(frozen=True)

    site: str
    slot: str
    model_id: str
    predicted: str
    expected: str
    probability: float = Field(ge=0.0, le=1.0)

    @property
    def correct(self) -> bool:
        """Whether the prediction matched the label."""
        return self.predicted == self.expected


class CalibrationBin(BaseModel):
    """Predictions whose stated probability fell in one range."""

    model_config = ConfigDict(frozen=True)

    lower: float
    upper: float
    count: int
    mean_stated: float
    observed_accuracy: float

    @property
    def gap(self) -> float:
        """Stated minus observed; positive means overconfident."""
        return self.mean_stated - self.observed_accuracy


class CalibrationReport(BaseModel):
    """Reliability table for one call site."""

    model_config = ConfigDict(frozen=True)

    site: str
    model_id: str
    records: int
    accuracy: float
    bins: tuple[CalibrationBin, ...]
    expected_calibration_error: float

    def render(self) -> str:
        """Plain-text table for the CLI."""
        lines = [
            f"calibration of {self.site} ({self.model_id}): {self.records} labeled predictions, "
            f"accuracy {self.accuracy:.2f}, "
            f"expected calibration error {self.expected_calibration_error:.2f}",
            f"  {'stated':<14} {'count':>5} {'mean stated':>12} {'observed':>9} {'gap':>6}",
        ]
        for b in self.bins:
            lines.append(
                f"  {b.lower:.1f} to {b.upper:.1f}{'':<5} {b.count:>5} {b.mean_stated:>12.2f} "
                f"{b.observed_accuracy:>9.2f} {b.gap:>+6.2f}"
            )
        return "\n".join(lines)


class CalibrationLog:
    """Append-only JSONL of labeled predictions, one file per call site."""

    def __init__(self, path: Path) -> None:
        """Use `path` as the log file."""
        self._path = path

    def append(self, record: CalibrationRecord) -> None:
        """Add one labeled prediction."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as handle:
            handle.write(record.model_dump_json() + "\n")

    def records(self) -> tuple[CalibrationRecord, ...]:
        """Every record in the log."""
        if not self._path.exists():
            return ()
        return tuple(
            CalibrationRecord.model_validate(json.loads(line))
            for line in self._path.read_text().splitlines()
            if line.strip()
        )


def calibration_report(
    site: str, records: tuple[CalibrationRecord, ...], *, bins: int = 5
) -> CalibrationReport:
    """Bucket predictions by stated probability and compare with observed accuracy.

    Args:
        site: The call site the records came from.
        records: Labeled predictions.
        bins: Number of equal-width probability ranges.

    Returns:
        A report with one row per non-empty bin and the expected calibration error, which is
        the count-weighted mean absolute gap between stated and observed.
    """
    width = 1.0 / bins
    rows: list[CalibrationBin] = []
    ece = 0.0
    total = len(records)
    for i in range(bins):
        lower, upper = i * width, (i + 1) * width
        members = [
            r
            for r in records
            if lower <= r.probability < upper or (i == bins - 1 and r.probability == 1.0)
        ]
        if not members:
            continue
        stated = sum(r.probability for r in members) / len(members)
        observed = sum(1 for r in members if r.correct) / len(members)
        rows.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_stated=stated,
                observed_accuracy=observed,
            )
        )
        ece += (len(members) / total) * abs(stated - observed) if total else 0.0
    model_ids = sorted({r.model_id for r in records})
    return CalibrationReport(
        site=site,
        model_id=", ".join(model_ids) if model_ids else "-",
        records=total,
        accuracy=(sum(1 for r in records if r.correct) / total) if total else 0.0,
        bins=tuple(rows),
        expected_calibration_error=ece,
    )
