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
    name: SafeName
    last_dimension: SafeName
    dimensions: list[Dimension] = Field(max_length=MAX_DIMENSIONS)
    datatypes: list[DatatypeSpec] = Field(max_length=MAX_DATATYPES)
    # Omitted dimensions use their full extent (a single chunk). An empty list
    # means no dimension is explicitly chunked.
    chunk_shape: list[ChunkDim] = []


class DatasetCreated(BaseModel):
    name: str
    last_dimension: str
    dimensions: int
    datatypes: int
    chunk_shape: list[ChunkDim]


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


class DatasetDetail(BaseModel):
    name: str
    last_dimension: str
    dimensions: list[DimensionInfo]
    blocks: list[BlockInfo]
    datatypes: list[DatatypeSpec]
    chunk_shape: list[ChunkDim]


class DatasetDeleted(BaseModel):
    dataset: str
    deleted: bool = True


class DatasetNotEmpty(BaseModel):
    error: str = "dataset_not_empty"
    message: str
    blocks: list[str]


class BlockDeleted(BaseModel):
    block: str
    deleted: bool = True


class BlockLoadIn(BaseModel):
    """Request to ingest a block from a server-readable Zarr source.

    The source must be reachable by the *server* (a gs:// URL with the server's
    credentials, or a path on the server's filesystem) — the data is not
    uploaded through this request.
    """

    source: str
    block: SafeName
    create_dataset: bool = False
    last_dimension: SafeName = "screen"
    overwrite: bool = False


class BlockLoaded(BaseModel):
    dataset: str
    block: str
    path: str
    dimensions: dict[str, int]
    datatypes: list[str]
