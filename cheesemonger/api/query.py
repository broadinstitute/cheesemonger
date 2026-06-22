# TODO: Validate selection labels against schema coordinates in the router
# (before calling the engine) to give clean 422s with useful messages,
# rather than relying on xarray KeyError strings.

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cheesemonger.schemas.query import QueryIn, QueryOut
from cheesemonger.services.dataset import DatasetService
from cheesemonger.services.query import QueryError, QueryService

from .deps import get_dataset_service, get_query_service

router = APIRouter(prefix="/datasets/{dataset}", tags=["query"])


@router.post("/query", response_model=QueryOut)
def query_data(
    dataset: str,
    query: QueryIn,
    ds: Annotated[DatasetService, Depends(get_dataset_service)],
    qs: Annotated[QueryService, Depends(get_query_service)],
) -> QueryOut:
    # An unsafe dataset name raises InvalidName from get_schema (path
    # construction), which the app's global handler maps to 400.
    schema = ds.get_schema(dataset)
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

    for sel in query.select:
        if sel.dimension not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in select: {sel.dimension}",
            )

    if query.aggregate:
        if query.aggregate.over not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in aggregate.over: {query.aggregate.over}",
            )
        if query.aggregate.type == "count_lt" and query.aggregate.threshold is None:
            raise HTTPException(
                status_code=422, detail="count_lt requires a threshold",
            )
        selected_dims = {s.dimension for s in query.select}
        if query.aggregate.over in selected_dims:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot aggregate over '{query.aggregate.over}': it is fixed by select",
            )

    block_names = ds.list_block_names(dataset)
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
            get_block_path=lambda b: ds.get_block_zarr_path(dataset, b),
        )
    except QueryError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
