"""Model protocols and adapters.

Two kinds of model: a classifier picks from a closed set with a probability, a drafter writes
text. Call sites are listed in prd/00-vision.md. Nothing here authorizes anything.
"""

from veena.models.calibration import (
    CalibrationLog,
    CalibrationRecord,
    CalibrationReport,
    calibration_report,
)
from veena.models.claude import ClaudeClassifier, ClaudeDrafter
from veena.models.jev import JevClassifier
from veena.models.keyword import KeywordClassifier
from veena.models.protocol import (
    Classification,
    Classifier,
    Drafter,
    RecordingClassifier,
    ReplayClassifier,
    ScriptedClassifier,
    ScriptedDrafter,
    closed_set,
    input_hash,
)

__all__ = [
    "CalibrationLog",
    "CalibrationRecord",
    "CalibrationReport",
    "Classification",
    "Classifier",
    "ClaudeClassifier",
    "ClaudeDrafter",
    "Drafter",
    "JevClassifier",
    "KeywordClassifier",
    "RecordingClassifier",
    "ReplayClassifier",
    "ScriptedClassifier",
    "ScriptedDrafter",
    "calibration_report",
    "closed_set",
    "input_hash",
]
