from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cheesemonger.schemas.dataset import BlockDeleted, BlockLoaded, BlockLoadIn
from cheesemonger.services.dataset import DatasetService
from cheesemonger.services.loader import LoaderError, load_block

from .deps import get_dataset_service

router = APIRouter(prefix="/datasets/{dataset}/blocks", tags=["blocks"])


# Defined with `def` (not `async def`) so FastAPI runs it in a worker thread —
# a slow load won't block the event loop. Synchronous for v1; loads are
# infrequent admin operations.
# TODO(security): this is an unauthenticated ingest path that makes the server
# read an arbitrary source URL/path. Gate it once auth exists.
# TODO(async): for very large sources, switch to a background job + status poll
# instead of a long synchronous request.
@router.post("", response_model=BlockLoaded, status_code=201)
def load_block_endpoint(
    dataset: str,
    body: BlockLoadIn,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
) -> BlockLoaded:
    try:
        summary = load_block(
            source=body.source,
            dataset=dataset,
            block=body.block,
            data_dir=str(ds.data_dir),
            last_dimension=body.last_dimension,
            create_dataset=body.create_dataset,
            overwrite=body.overwrite,
        )
    except LoaderError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return BlockLoaded(**summary)


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
