from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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
