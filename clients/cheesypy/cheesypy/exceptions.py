"""Exceptions raised by the cheesypy client.

HTTP error responses from the server are translated into these so callers
handle Python exceptions carrying the server's ``detail`` message, rather than
raw HTTP objects.
"""

from __future__ import annotations


class CheesemongerError(Exception):
    """Base class for all cheesypy errors."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DatasetNotFound(CheesemongerError):
    """A dataset or block was not found (HTTP 404)."""


class QueryError(CheesemongerError):
    """The request was rejected by the server (HTTP 400/422)."""
