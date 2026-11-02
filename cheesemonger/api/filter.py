"""Value-filtering endpoint: POST /datasets/{dataset}/filter.

Returns the individual cells of a datatype that pass a predicate (e.g.
Correlation > 0.75) plus their coordinate labels, filtering inside xarray on
the server instead of shipping the whole hypercube to the client.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.openapi.models import Example
from sqlalchemy.orm import Session

from cheesemonger.config import Settings, get_settings
from cheesemonger.crud import dataset as ds_crud
from cheesemonger.db import get_db
from cheesemonger.schemas.filter import FilterIn, FilterOut
from cheesemonger.services import dataset as ds_paths
from cheesemonger.services.query import QueryError, QueryService

from .deps import get_query_service

router = APIRouter(prefix="/datasets/{dataset}", tags=["query"])

# Named request bodies shown in the Swagger UI "Try it out" dropdown. Labels are
# illustrative; real ones come from GET /datasets/{dataset}.
_FILTER_EXAMPLES: dict[str, Example] = {
    "threshold": {
        "summary": "Significant responses for one target, with effect size",
        "value": {
            "filter": {"datatype": "FDR", "op": "lt", "value": 0.05},
            "datatypes": ["L2FC"],
            "select": [
                {"dimension": "Screen", "value": "PS-SC-1"},
                {"dimension": "Timepoint", "value": "D4"},
                {"dimension": "Target", "value": "23293"},
            ],
        },
    },
    "correlation": {
        "summary": "Strong correlations in every screen, with the partner gene",
        "value": {
            "filter": {"datatype": "Correlation", "op": "gt", "value": 0.9},
            "datatypes": ["CorrelateResponse"],
            "limit": 1000,
        },
    },
    "membership": {
        "summary": "Correlates that are in a gene set (entrez IDs)",
        "value": {
            "filter": {"datatype": "CorrelateResponse", "op": "in", "value": ["7157", "4193"]},
            "datatypes": ["Correlation"],
            "select": [{"dimension": "Screen", "value": "PS-SC-1"}],
        },
    },
}

_FILTER_ERRORS: dict[int | str, dict] = {
    400: {"description": "Unknown datatype, or unknown dimension in `select`"},
    404: {"description": "Dataset or block not found"},
    422: {
        "description": (
            "Invalid request: label not found, `datatypes` with different dimensions "
            "from the filtered one, a list value with a non-`in` op (or a scalar with "
            "`in`), empty block-key list, or a malformed body"
        )
    },
}


@router.post(
    "/filter",
    response_model=FilterOut,
    summary="Filter cells by value",
    responses=_FILTER_ERRORS,
)
def filter_data(
    dataset: str,
    spec: Annotated[FilterIn, Body(openapi_examples=_FILTER_EXAMPLES)],
    db: Annotated[Session, Depends(get_db)],
    qs: Annotated[QueryService, Depends(get_query_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FilterOut:
    """Return only the cells of a datatype that pass a test, with their
    coordinates. The comparison runs on the server, so you download the matches
    rather than the whole array.

    **`filter`** is the test: a datatype, an operator (`gt`, `lt`, `eq`, `ge`,
    `le`, or `in` with a list), and a value. String datatypes such as
    `CorrelateResponse` can be tested with `eq` or `in`.

    **`datatypes`** lists extra datatypes to read at each matching cell; they
    must have the same dimensions as the filtered datatype.

    **`select`** narrows the search first, exactly as in `/query`. Omit the
    block key to search every block.

    The response is in long form: one row per matching cell, as parallel lists.
    `coords` holds the labels for each dimension (with the block key last, so
    every row says which block it came from) and `data` the values. Results stop
    at `limit` or at the server cap of 100,000 rows; `truncated` says whether
    that happened.
    """
    schema =ds_crud.get_schema_dict(db, dataset)
    if schema is None:
        raise HTTPException(status_code=404, detail="Dataset does not exist")

    last_dim = schema["last_dimension"]
    dim_names = {d["name"] for d in schema["dimensions"]}
    dt_names = {d["name"] for d in schema["datatypes"]}
    dt_dims = {d["name"]: d["dimensions"] for d in schema["datatypes"]}
    valid_dims = dim_names | {last_dim}

    if spec.filter.datatype not in dt_names:
        raise HTTPException(
            status_code=400, detail=f"Unknown datatype: {spec.filter.datatype}"
        )
    for dt in spec.datatypes:
        if dt not in dt_names:
            raise HTTPException(status_code=400, detail=f"Unknown datatype: {dt}")

    # Every returned datatype is read at the passing cells, so all must share the
    # filtered datatype's dimensions (same rule as multi-datatype queries).
    base_dims = dt_dims[spec.filter.datatype]
    for dt in spec.datatypes:
        if dt != spec.filter.datatype and dt_dims[dt] != base_dims:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Datatypes must share dimensions with the filtered datatype: "
                    f"'{dt}' has {dt_dims[dt]}, "
                    f"'{spec.filter.datatype}' has {base_dims}"
                ),
            )

    for sel in spec.select:
        if sel.dimension not in valid_dims:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown dimension in select: {sel.dimension}",
            )

    block_names = ds_crud.list_block_names(db, dataset)
    block_name_set = set(block_names)
    block_sel = next((s for s in spec.select if s.dimension == last_dim), None)
    if block_sel:
        # The block key may be a single value, a subset (list), or omitted (all).
        # Validate every requested block exists for a clean 404.
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

    try:
        return qs.execute_filter(
            spec=spec,
            schema=schema,
            block_names=block_names,
            get_block_path=lambda b: ds_paths.block_dir(settings.data_dir, dataset, b),
        )
    except QueryError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
