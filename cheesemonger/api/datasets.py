from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from cheesemonger.config import Settings, get_settings
from cheesemonger.crud import dataset as ds_crud
from cheesemonger.db import get_db
from cheesemonger.schemas.common import ChunkDim, DatatypeSpec
from cheesemonger.schemas.dataset import (
    BlockInfo,
    DatasetCreated,
    DatasetDeleted,
    DatasetDetail,
    DatasetIn,
    DatasetListOut,
    DatasetNotEmpty,
    DatasetSummary,
    DimensionInfo,
)
from cheesemonger.services import dataset as ds_paths

router = APIRouter(prefix="/datasets", tags=["datasets"])

_LABEL_TRUNCATION_THRESHOLD = 100


@router.post("", response_model=DatasetCreated, status_code=201)
def create_dataset(
    dataset_in: DatasetIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatasetCreated:
    if ds_crud.dataset_exists(db, dataset_in.name):
        raise HTTPException(status_code=409, detail="A dataset with this name already exists")

    dim_names = {d.name for d in dataset_in.dimensions}
    if dataset_in.last_dimension in dim_names:
        raise HTTPException(
            status_code=400,
            detail=f"last_dimension '{dataset_in.last_dimension}' must not appear in dimensions",
        )

    for dt in dataset_in.datatypes:
        for d in dt.dimensions:
            if d not in dim_names:
                raise HTTPException(
                    status_code=400,
                    detail=f"Datatype '{dt.name}' references unknown dimension '{d}'",
                )

    for dim in dataset_in.dimensions:
        if not dim.labels:
            raise HTTPException(
                status_code=400,
                detail=f"Dimension '{dim.name}' has an empty labels list",
            )

    ds = ds_crud.create_dataset(db, dataset_in)

    # Create the blocks directory on disk (path sanitized via ds_paths)
    ds_paths.blocks_dir(settings.data_dir, dataset_in.name).mkdir(parents=True, exist_ok=True)

    db.commit()

    return DatasetCreated(
        name=ds.name,
        last_dimension=ds.last_dimension,
        dimensions=len(ds.dimensions),
        datatypes=len(ds.datatypes),
        chunk_shape=[ChunkDim(**c) for c in ds.chunk_shape],
    )


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


@router.delete(
    "/{dataset}",
    response_model=DatasetDeleted,
    responses={409: {"model": DatasetNotEmpty, "description": "Dataset still has blocks"}},
)
def delete_dataset(
    dataset: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatasetDeleted | JSONResponse:
    ds = ds_crud.get_dataset_by_name(db, dataset)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    block_names = [b.name for b in ds.blocks]
    if block_names:
        body = DatasetNotEmpty(
            message=(
                f"Dataset '{dataset}' still has {len(block_names)} block(s). "
                "Delete all blocks before deleting the dataset."
            ),
            blocks=sorted(block_names),
        )
        return JSONResponse(status_code=409, content=body.model_dump())

    ds_crud.delete_dataset(db, dataset)

    # Remove the dataset directory from disk (path sanitized via ds_paths)
    ds_dir = ds_paths.dataset_dir(settings.data_dir, dataset)
    if ds_dir.exists():
        shutil.rmtree(ds_dir)

    db.commit()
    return DatasetDeleted(dataset=dataset)
