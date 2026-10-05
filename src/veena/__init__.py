"""Veena: the self-organizing data stack."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("veena")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
