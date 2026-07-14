from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.db import get_db
from cheesemonger.schemas.common import ChunkDim, DatatypeSpec
from cheesemonger.schemas.dataset import (
    BlockInfo,
    DatasetDetail,
    DatasetListOut,
    DatasetSummary,
    DimensionInfo,
    DimensionLabelsOut,
)

# Read-only router. Datasets and blocks are created/deleted exclusively through
# the CLI loader (`python -m cheesemonger ...`), not over HTTP — see
# services/loader.py. The API only reads.
router = APIRouter(prefix="/datasets", tags=["datasets"])

_LABEL_TRUNCATION_THRESHOLD = 100


@router.get("", response_model=DatasetListOut)
def list_datasets(
    db: Annotated[Session, Depends(get_db)],
) -> DatasetListOut:
    datasets = ds_crud.list_datasets(db)
    summaries = [
        DatasetSummary(
            name=ds.name,
            blocks=len(ds.blocks),
            datatypes=len(ds.datatypes),
        )
        for ds in datasets
    ]
    return DatasetListOut(datasets=summaries)


@router.get("/{dataset}", response_model=DatasetDetail)
def get_dataset(
    dataset: str,
    db: Annotated[Session, Depends(get_db)],
) -> DatasetDetail:
    ds = ds_crud.get_dataset_by_name(db, dataset)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    dims: list[DimensionInfo] = []
    for d in ds.dimensions:
        labels = d["labels"]
        size = len(labels)
        if size > _LABEL_TRUNCATION_THRESHOLD:
            dims.append(DimensionInfo(
                name=d["name"], size=size, labels_truncated=True, labels_sample=labels[:5],
            ))
        else:
            dims.append(DimensionInfo(name=d["name"], size=size, labels=labels))

    block_infos = [
        BlockInfo(name=b.name, loaded_at=b.loaded_at.isoformat())
        for b in sorted(ds.blocks, key=lambda b: b.name)
    ]

    return DatasetDetail(
        name=ds.name,
        last_dimension=ds.last_dimension,
        dimensions=dims,
        blocks=block_infos,
        datatypes=[DatatypeSpec(**dt) for dt in ds.datatypes],
        chunk_shape=[ChunkDim(**c) for c in ds.chunk_shape],
    )


@router.get("/{dataset}/dimensions/{dim}", response_model=DimensionLabelsOut)
def get_dimension_labels(
    dataset: str,
    dim: str,
    db: Annotated[Session, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(gt=0)] = None,
) -> DimensionLabelsOut:
    """Full coordinate labels for one dimension (unlike GET /datasets/{dataset},
    which truncates large label lists). Pass the ``last_dimension`` name to list
    the loaded blocks. Supports ``offset``/``limit`` paging for large dimensions.
    """
    ds = ds_crud.get_dataset_by_name(db, dataset)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    if dim == ds.last_dimension:
        all_labels: list = sorted(b.name for b in ds.blocks)
    else:
        match = next((d for d in ds.dimensions if d["name"] == dim), None)
        if match is None:
            raise HTTPException(
                status_code=404,
                detail=f"Dimension '{dim}' not found in dataset '{dataset}'",
            )
        all_labels = match["labels"]

    end = None if limit is None else offset + limit
    return DimensionLabelsOut(name=dim, size=len(all_labels), labels=all_labels[offset:end])
