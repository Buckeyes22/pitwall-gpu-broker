"""Pitwall package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pitwall-gpu-broker")
except PackageNotFoundError:  # source tree before installation
    __version__ = "0+unknown"

__all__ = ["__version__"]
