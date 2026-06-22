from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from cheesemonger.schemas.dataset import (
    DatasetCreated,
    DatasetDeleted,
    DatasetDetail,
    DatasetIn,
    DatasetListOut,
    DatasetNotEmpty,
)
from cheesemonger.services.dataset import DatasetService

from .deps import get_dataset_service

router = APIRouter(prefix="/datasets", tags=["datasets"])

# Note: unsafe names in URL path params raise InvalidName inside the service
# layer (at path construction), which the app's global handler maps to 400.
# Request-body names are validated by Pydantic (SafeName) and return 422.


@router.post("", response_model=DatasetCreated, status_code=201)
def create_dataset(
    dataset_in: DatasetIn,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> DatasetCreated:
    if ds.exists(dataset_in.name):
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

    return ds.create(dataset_in)


@router.get("", response_model=DatasetListOut)
def list_datasets(
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> DatasetListOut:
    return ds.list_datasets()


@router.get("/{dataset}", response_model=DatasetDetail)
def get_dataset(
    dataset: str,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> DatasetDetail:
    detail = ds.get_detail(dataset)
    if detail is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")
    return detail


@router.delete(
    "/{dataset}",
    response_model=DatasetDeleted,
    responses={409: {"model": DatasetNotEmpty, "description": "Dataset still has blocks"}},
)
def delete_dataset(
    dataset: str,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> DatasetDeleted | JSONResponse:
    if not ds.exists(dataset):
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    blocks = ds.list_block_names(dataset)
    if blocks:
        body = DatasetNotEmpty(
            message=f"Dataset '{dataset}' still has {len(blocks)} block(s). Delete all blocks before deleting the dataset.",
            blocks=blocks,
        )
        return JSONResponse(status_code=409, content=body.model_dump())

    ds.delete_dataset(dataset)
    return DatasetDeleted(dataset=dataset)
