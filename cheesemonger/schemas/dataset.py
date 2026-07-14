from __future__ import annotations

from pydantic import BaseModel, Field

from .common import (
    MAX_DATATYPES,
    MAX_DIMENSIONS,
    ChunkDim,
    DatatypeSpec,
    Dimension,
    SafeName,
)


class DatasetIn(BaseModel):
    """A dataset schema. Built by the loader (from an inferred source store),
    not accepted over HTTP — the API is read-only."""

    name: SafeName
    last_dimension: SafeName
    dimensions: list[Dimension] = Field(max_length=MAX_DIMENSIONS)
    datatypes: list[DatatypeSpec] = Field(max_length=MAX_DATATYPES)
    # Omitted dimensions use their full extent (a single chunk). An empty list
    # means no dimension is explicitly chunked.
    chunk_shape: list[ChunkDim] = []


class DatasetSummary(BaseModel):
    name: str
    blocks: int
    datatypes: int


class DatasetListOut(BaseModel):
    datasets: list[DatasetSummary]


class BlockInfo(BaseModel):
    name: str
    loaded_at: str


class DimensionInfo(BaseModel):
    name: str
    size: int
    labels: list[int] | list[str] | None = None
    labels_truncated: bool = False
    labels_sample: list[int] | list[str] | None = None


class DimensionLabelsOut(BaseModel):
    name: str
    size: int  # total number of labels (before any offset/limit paging)
    labels: list[int] | list[str]


class DatasetDetail(BaseModel):
    name: str
    last_dimension: str
    dimensions: list[DimensionInfo]
    blocks: list[BlockInfo]
    datatypes: list[DatatypeSpec]
    chunk_shape: list[ChunkDim]
