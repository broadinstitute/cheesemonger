"""cheesypy — Python client for the Cheesemonger perturb-seq API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .client import GENE_DATATYPES, GENE_DIMENSIONS, Cheesemonger
from .exceptions import CheesemongerError, DatasetNotFound, QueryError

__all__ = [
    "GENE_DATATYPES",
    "GENE_DIMENSIONS",
    "Cheesemonger",
    "CheesemongerError",
    "DatasetNotFound",
    "QueryError",
]

# Single source of truth is the `version` in pyproject.toml; read it back from
# the installed package metadata so it never has to be duplicated here.
try:
    __version__ = _version("cheesypy")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0+unknown"
