"""Verification: checks generated from the spec and the semantic model, never from a candidate.

This package must not import `veena.planner` or `veena.backends`. It receives the name of the
output relation and a way to run SQL, and builds every query itself.
"""

from veena.verification.checks import Check, VerificationOutcome, build_checks, verify

__all__ = ["Check", "VerificationOutcome", "build_checks", "verify"]
