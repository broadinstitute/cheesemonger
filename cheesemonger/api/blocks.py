from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cheesemonger.config import Settings, get_settings
from cheesemonger.crud import dataset as ds_crud
from cheesemonger.db import get_db
from cheesemonger.schemas.dataset import BlockDeleted, BlockLoaded, BlockLoadIn
from cheesemonger.services import dataset as ds_paths
from cheesemonger.services.loader import LoaderError, load_block

router = APIRouter(prefix="/datasets/{dataset}/blocks", tags=["blocks"])


@router.post("", response_model=BlockLoaded, status_code=201)
def load_block_endpoint(
    dataset: str,
    body: BlockLoadIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BlockLoaded:
    try:
        summary = load_block(
            source=body.source,
            dataset=dataset,
            block=body.block,
            data_dir=settings.data_dir,
            db=db,
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
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BlockDeleted:
    if not ds_crud.dataset_exists(db, dataset):
        raise HTTPException(status_code=404, detail="Dataset does not exist")
    if not ds_crud.block_exists(db, dataset, block):
        raise HTTPException(status_code=404, detail="Block does not exist")

    ds_crud.delete_block(db, dataset, block)

    # Remove block data from disk (path sanitized via ds_paths)
    block_path = ds_paths.block_dir(settings.data_dir, dataset, block)
    if block_path.exists():
        shutil.rmtree(block_path)

    db.commit()
    return BlockDeleted(block=block)
