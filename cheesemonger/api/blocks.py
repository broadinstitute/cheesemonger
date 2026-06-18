from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cheesemonger.schemas.dataset import BlockDeleted
from cheesemonger.services.dataset import DatasetService

from .deps import get_dataset_service

router = APIRouter(prefix="/datasets/{dataset}/blocks", tags=["blocks"])


@router.delete("/{block}", response_model=BlockDeleted)
def delete_block(
    dataset: str,
    block: str,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> BlockDeleted:
    if not ds.exists(dataset):
        raise HTTPException(status_code=404, detail="Dataset does not exist")
    if not ds.block_exists(dataset, block):
        raise HTTPException(status_code=404, detail="Block does not exist")

    ds.delete_block(dataset, block)
    return BlockDeleted(block=block)
