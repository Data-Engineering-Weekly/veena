"""The specification IR: what an asset means, independent of how it is built."""

from veena.spec.load import dump_spec, load_spec, parse_spec
from veena.spec.model import (
    Aggregation,
    AssetSpec,
    Filter,
    Grain,
    Measure,
    Population,
    ServiceRequirements,
    TimeBucket,
    Window,
)
from veena.spec.subsumption import Operation, SubsumptionResult, Verdict, check, compare

__all__ = [
    "Aggregation",
    "AssetSpec",
    "Filter",
    "Grain",
    "Measure",
    "Operation",
    "Population",
    "ServiceRequirements",
    "SubsumptionResult",
    "TimeBucket",
    "Verdict",
    "Window",
    "check",
    "compare",
    "dump_spec",
    "load_spec",
    "parse_spec",
]
