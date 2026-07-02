"""Sanitized on-disk path helpers for dataset block directories.

Metadata (dimensions, datatypes, blocks) now lives in SQLite via the crud
layer. This module only builds the Zarr block directory paths — and it is the
single place that does so, routing every dataset/block name through
``sanitize_name`` so no request can escape ``data_dir`` via ``..`` or ``/``.
The API layer stores metadata in SQLite (which is safe from path traversal),
but the *filesystem* paths must still be sanitized here.
"""

from __future__ import annotations

from pathlib import Path

from cheesemonger.schemas.common import sanitize_name


def dataset_dir(data_dir: str, name: str) -> Path:
    return Path(data_dir) / sanitize_name(name)


def blocks_dir(data_dir: str, name: str) -> Path:
    return dataset_dir(data_dir, name) / "blocks"


def block_dir(data_dir: str, dataset: str, block: str) -> Path:
    return blocks_dir(data_dir, dataset) / sanitize_name(block)
