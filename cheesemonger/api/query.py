# TODO: Validate selection labels against schema coordinates in the router
# (before calling the engine) to give clean 422s with useful messages,
# rather than relying on xarray KeyError strings.

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cheesemonger.config import Settings, get_settings
from cheesemonger.crud import dataset as ds_crud
from cheesemonger.db import get_db
from cheesemonger.schemas.query import QueryIn, QueryOut
from cheesemonger.services import dataset as ds_paths
from cheesemonger.services.query import QueryError, QueryService

from .deps import get_query_service

router = APIRouter(prefix="/datasets/{dataset}", tags=["query"])


@router.post("/query", response_model=QueryOut)
def query_data(
    dataset: str,
    query: QueryIn,
    db: Annotated[Session, Depends(get_db)],
    qs: Annotated[QueryService, Depends(get_query_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> QueryOut:
    schema = ds_crud.get_schema_dict(db, dataset)
    if schema is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    last_dim = schema["last_dimension"]
    dim_names = {d["name"] for d in schema["dimensions"]}
    dt_names = {d["name"] for d in schema["datatypes"]}
    valid_dims = dim_names | {last_dim}

    datatypes = query.datatype if isinstance(query.datatype, list) else [query.datatype]
    for dt in datatypes:
        if dt not in dt_names:
            raise HTTPException(status_code=400, detail=f"Unknown datatype: {dt}")

    dt_dims = {d["name"]: d["dimensions"] for d in schema["datatypes"]}
    batch_dims = dt_dims[datatypes[0]]
    for dt in datatypes[1:]:
        if dt_dims[dt] != batch_dims:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Datatypes in a batch must share the same dimensions: "
                    f"'{dt}' has {dt_dims[dt]}, '{datatypes[0]}' has {batch_dims}"
                ),
            )

    for sel in query.select:
        if sel.dimension not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in select: {sel.dimension}",
            )

    if query.diagonal:
        if query.aggregate:
            raise HTTPException(
                status_code=422,
                detail="Cannot combine 'diagonal' with 'aggregate' in one query",
            )
        for d in query.diagonal:
            if d not in batch_dims:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"diagonal dimension '{d}' is not a dimension "
                        f"of datatype '{datatypes[0]}'"
                    ),
                )

    if query.aggregate:
        over = query.aggregate.over
        if over not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in aggregate.over: {over}",
            )
        if query.aggregate.type == "count_lt" and query.aggregate.threshold is None:
            raise HTTPException(status_code=422, detail="count_lt requires a threshold")
        selected_dims = {s.dimension for s in query.select}
        if over in selected_dims:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot aggregate over '{over}': it is fixed by select",
            )
        if over != last_dim and over not in batch_dims:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot aggregate over '{over}': not a dimension "
                    f"of datatype '{datatypes[0]}'"
                ),
            )

    block_names = ds_crud.list_block_names(db, dataset)
    block_sel = next((s for s in query.select if s.dimension == last_dim), None)
    if block_sel:
        block_name = str(block_sel.value)
        if block_name not in block_names:
            raise HTTPException(
                status_code=404,
                detail=f"Block '{block_name}' not found in dataset '{dataset}'",
            )

    try:
        return qs.execute(
            query=query,
            schema=schema,
            block_names=block_names,
            get_block_path=lambda b: ds_paths.block_dir(settings.data_dir, dataset, b),
        )
    except QueryError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
