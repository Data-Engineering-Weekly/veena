"""Veena: the self-organizing data stack.

Consumers declare outcomes and commitments; the engine maintains the asset graph. The most used
entry points are re-exported here. See `veena.cli` for the command line.
"""

from importlib.metadata import PackageNotFoundError, version

from veena.coordinator import Coordinator
from veena.errors import VeenaError
from veena.registry import Registry, SqliteEventStore
from veena.resolver import Resolver
from veena.semantic import SemanticModel, load_semantic_model
from veena.spec import AssetSpec, load_spec

try:
    __version__ = version("veena")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"

__all__ = [
    "AssetSpec",
    "Coordinator",
    "Registry",
    "Resolver",
    "SemanticModel",
    "SqliteEventStore",
    "VeenaError",
    "__version__",
    "load_semantic_model",
    "load_spec",
]
