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
    not accepted over HTTP, the API is read-only."""

    name: SafeName
    last_dimension: SafeName
    dimensions: list[Dimension] = Field(max_length=MAX_DIMENSIONS)
    datatypes: list[DatatypeSpec] = Field(max_length=MAX_DATATYPES)
    # Omitted dimensions use their full extent (a single chunk). An empty list
    # means no dimension is explicitly chunked.
    chunk_shape: list[ChunkDim] = []
    # {dim_name: [label, ...]}, validation superset per gene dimension. A
    # block's labels along a listed dim must be a subset. Empty = no dimension
    # is label-validated.
    gene_universe: dict[str, list[str]] = {}


class DatasetSummary(BaseModel):
    name: str = Field(description="Dataset name, used in `/datasets/{dataset}/...` paths.")
    blocks: int = Field(description="Number of loaded blocks (e.g. screens).")
    datatypes: int = Field(description="Number of datatypes.")


class DatasetListOut(BaseModel):
    datasets: list[DatasetSummary]


class BlockInfo(BaseModel):
    name: str = Field(description="Block name: a label of the dataset's `last_dimension`.")
    loaded_at: str = Field(description="When the block was loaded (ISO 8601).")


class DimensionInfo(BaseModel):
    name: str
    size: int = Field(description="Number of labels across all loaded blocks.")
    labels: list[str] | None = Field(
        default=None, description="All labels; `null` when the dimension is truncated."
    )
    labels_truncated: bool = Field(
        default=False,
        description=(
            "True for dimensions with more than 100 labels. Use "
            "`GET /datasets/{dataset}/dimensions/{dim}` for the full list."
        ),
    )
    labels_sample: list[str] | None = Field(
        default=None, description="The first 5 labels, when truncated."
    )


class DimensionLabelsOut(BaseModel):
    name: str
    size: int = Field(description="Total number of labels, before `offset`/`limit` paging.")
    labels: list[str] = Field(description="The requested page of labels.")


class DatasetDetail(BaseModel):
    name: str
    last_dimension: str = Field(
        description=(
            "Name of the block key (e.g. `Screen`). Use it in `select` like any other "
            "dimension; it is case-sensitive."
        )
    )
    dimensions: list[DimensionInfo] = Field(
        description="Array dimensions within a block (not including `last_dimension`)."
    )
    blocks: list[BlockInfo] = Field(description="Loaded blocks, sorted by name.")
    datatypes: list[DatatypeSpec] = Field(
        description="Datatypes and the dimensions each one spans."
    )
    chunk_shape: list[ChunkDim] = Field(
        description="Storage chunk size per dimension; unlisted dimensions are one chunk."
    )
