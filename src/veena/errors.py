"""Typed exceptions raised by library code."""


class VeenaError(Exception):
    """Base class for every error veena raises on purpose."""


class SpecError(VeenaError):
    """A specification is malformed or cannot be loaded."""


class SemanticRefError(SpecError):
    """A semantic ref does not resolve in the semantic model."""


class RegistryError(VeenaError):
    """The registry refused an operation."""


class RevisionConflictError(RegistryError):
    """An append assumed a registry revision that is no longer current."""

    def __init__(self, expected: int, actual: int) -> None:
        """Record the revision the caller assumed and the one the store holds.

        Args:
            expected: Revision the caller assumed.
            actual: Revision the store holds.
        """
        super().__init__(f"expected revision {expected}, store is at {actual}")
        self.expected = expected
        self.actual = actual


class ScenarioError(VeenaError):
    """The synthetic scenario could not be built or read."""


class PlanError(VeenaError):
    """The planner cannot compile or render a specification."""


class BackendError(VeenaError):
    """A backend could not materialize, publish, or query."""


class CoordinationError(VeenaError):
    """The coordinator cannot proceed with an activation."""


class VerificationError(VeenaError):
    """Verification could not build its checks."""
