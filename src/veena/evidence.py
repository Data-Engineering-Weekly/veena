"""Grades and check results shared by verification and the registry."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Grade(StrEnum):
    """Strength of evidence a check provides, weakest first."""

    SAMPLE = "sample"
    TEST = "test"
    RECONCILIATION = "reconciliation"
    PROOF = "proof"

    @property
    def rank(self) -> int:
        """Position in the strength order; higher is stronger."""
        return _RANK[self]


_RANK = {Grade.SAMPLE: 0, Grade.TEST: 1, Grade.RECONCILIATION: 2, Grade.PROOF: 3}


class CheckResult(BaseModel):
    """What one check concluded."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    grade: Grade
    structural: bool
    passed: bool
    detail: str = ""
    uncertainty: str = ""


def verification_grade(results: tuple[CheckResult, ...]) -> Grade:
    """The strongest grade held by a passing value check, or `sample` when none passed."""
    best = Grade.SAMPLE
    for result in results:
        if result.passed and not result.structural and result.grade.rank > best.rank:
            best = result.grade
    return best


__all__ = ["CheckResult", "Grade", "verification_grade"]
