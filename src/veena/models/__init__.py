"""Model protocols: a classifier that picks from closed sets, a drafter that writes text.

Call sites are listed in prd/00-vision.md. Nothing here authorizes anything.
"""

from veena.models.protocol import (
    Classification,
    Classifier,
    Drafter,
    ScriptedClassifier,
    ScriptedDrafter,
)

__all__ = ["Classification", "Classifier", "Drafter", "ScriptedClassifier", "ScriptedDrafter"]
