from __future__ import annotations

from pydantic import BaseModel

from .common import ChunkDim, DatatypeSpec, Dimension


class DatasetIn(BaseModel):
    name: str
    last_dimension: str
    dimensions: list[Dimension]
    datatypes: list[DatatypeSpec]
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
