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
from cheesemonger.schemas.query import THRESHOLD_AGGREGATIONS, QueryIn, QueryOut
from cheesemonger.services import dataset as ds_paths
from cheesemonger.services.query import (
    BYTES_PER_RESULT_ELEMENT,
    QueryError,
    QueryService,
    estimate_result_elements,
    human_bytes,
)

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

    for dt in query.datatypes:
        if dt not in dt_names:
            raise HTTPException(status_code=400, detail=f"Unknown datatype: {dt}")

    # All datatypes in one query share the response index, so they must share
    # dimensions (e.g. L2FC + FDR over the same gene axis for a volcano plot).
    dt_dims = {d["name"]: d["dimensions"] for d in schema["datatypes"]}
    queried_dims = dt_dims[query.datatypes[0]]
    for dt in query.datatypes[1:]:
        if dt_dims[dt] != queried_dims:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Datatypes in one query must share dimensions: "
                    f"'{dt}' has {dt_dims[dt]}, "
                    f"'{query.datatypes[0]}' has {queried_dims}"
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
            if d not in queried_dims:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"diagonal dimension '{d}' is not a dimension "
                        f"of datatype '{query.datatypes[0]}'"
                    ),
                )

    if query.aggregate:
        over = query.aggregate.over
        if over not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in aggregate.over: {over}",
            )
        if (
            query.aggregate.type in THRESHOLD_AGGREGATIONS
            and query.aggregate.threshold is None
        ):
            raise HTTPException(
                status_code=422,
                detail=f"{query.aggregate.type} requires a threshold",
            )
        selected_dims = {s.dimension for s in query.select}
        if over in selected_dims:
            # A list on the block key names "this subset of screens"; aggregating
            # over that subset is meaningful (e.g. mean across the chosen screens).
            # Any other fixed selection genuinely pins the dim, so it can't be
            # reduced.
            block_sel_is_list = any(
                s.dimension == last_dim and isinstance(s.value, list)
                for s in query.select
            )
            if not (over == last_dim and block_sel_is_list):
                raise HTTPException(
                    status_code=422,
                    detail=f"Cannot aggregate over '{over}': it is fixed by select",
                )
        if over != last_dim and over not in queried_dims:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot aggregate over '{over}': not a dimension "
                    f"of datatype '{query.datatypes[0]}'"
                ),
            )

    block_names = ds_crud.list_block_names(db, dataset)
    block_name_set = set(block_names)
    block_sel = next((s for s in query.select if s.dimension == last_dim), None)
    if block_sel:
        # The block key may be a single value (one block, dim collapsed) or a
        # list (a subset of blocks, dim kept). Validate every requested block
        # exists so callers get a clean 404 instead of a read failure.
        if isinstance(block_sel.value, list):
            if not block_sel.value:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Empty list for the block key '{last_dim}'; omit it to "
                        f"span all blocks, or list one or more block names."
                    ),
                )
            requested = block_sel.value
        else:
            requested = [str(block_sel.value)]
        missing = [b for b in requested if b not in block_name_set]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Block(s) not found in dataset '{dataset}': "
                    f"{', '.join(repr(b) for b in missing)}"
                ),
            )

    # Reject an oversized result before reading any data. The shape is known from
    # the schema + selections, so we can estimate the serialized size up front.
    elements = estimate_result_elements(query, schema, block_names)
    est_bytes = elements * BYTES_PER_RESULT_ELEMENT
    if est_bytes > settings.max_result_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Query result too large: estimated {human_bytes(est_bytes)} "
                f"(~{elements:,} values) exceeds the "
                f"{human_bytes(settings.max_result_bytes)} limit. Narrow the query — "
                f"fix more dimensions, aggregate over a dimension, or select fewer labels."
            ),
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
