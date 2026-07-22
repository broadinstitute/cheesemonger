"""Query engine — reads xarray-exported Zarr stores and applies selections.

The source data is written by xarray.Dataset.to_zarr(), which embeds
dimension names and coordinate labels inside the Zarr store. We read
with xarray.open_zarr() to get label-based .sel() indexing for free,
rather than doing manual label→integer index lookups on raw Zarr arrays.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.models.dataset import DatatypeDict, SchemaDict
from cheesemonger.schemas.query import (
    AggregateSpec,
    IndexLevel,
    QueryIn,
    QueryOut,
)

logger = logging.getLogger(__name__)


class QueryError(Exception):
    """Raised for query-time validation errors (bad labels, etc.)."""


def _numpy_to_json(arr: np.ndarray) -> list | float | int | None:
    """Convert a numpy array to JSON-serializable Python types.

    Uses arr.tolist() for fast conversion, then replaces NaN with None.
    """
    if arr.ndim == 0:
        val = arr.item()
        if isinstance(val, float) and np.isnan(val):
            return None
        return val

    if np.issubdtype(arr.dtype, np.floating) and np.any(np.isnan(arr)):
        obj_arr = arr.astype(object)
        obj_arr[np.isnan(arr)] = None
        return obj_arr.tolist()

    return arr.tolist()


def _aggregate_array(arr: np.ndarray, axis: int, aggregate: AggregateSpec) -> np.ndarray:
    """Reduce ``arr`` along ``axis`` per the aggregation spec.

    Shared by within-block reduction (a datatype's own axis) and cross-block
    reduction (the stacked-blocks axis), so both paths behave identically. NaNs
    are ignored by mean/median/min/max/count, and never satisfy the threshold
    predicates.
    """
    t = aggregate.type
    if t == "mean":
        return np.nanmean(arr, axis=axis)
    if t == "median":
        return np.nanmedian(arr, axis=axis)
    if t == "min":
        return np.nanmin(arr, axis=axis)
    if t == "max":
        return np.nanmax(arr, axis=axis)
    if t == "count":
        # Count of non-NaN values (integer arrays have no NaN → count all).
        if np.issubdtype(arr.dtype, np.floating):
            valid = ~np.isnan(arr)
        else:
            valid = np.ones(arr.shape, dtype=bool)
        return np.sum(valid, axis=axis)
    if aggregate.threshold is None:
        raise QueryError(f"{t} requires a threshold")
    if t == "count_lt":
        return np.sum(arr < aggregate.threshold, axis=axis)
    if t == "count_gt":
        return np.sum(arr > aggregate.threshold, axis=axis)
    if t == "abs_gt":
        return np.sum(np.abs(arr) > aggregate.threshold, axis=axis)
    raise QueryError(f"Unknown aggregation type: {t}")


def _read_datatype_from_ds(
    ds: xr.Dataset,
    datatype: str,
    array_selections: dict[str, int | str],
    aggregate: AggregateSpec | None,
    diagonal: tuple[str, str] | None,
) -> np.ndarray:
    """Read one datatype from an already-opened xarray Dataset.

    Selects the datatype variable, applies .sel() for label-based indexing,
    and optionally aggregates. The caller is responsible for opening and
    closing the Dataset.
    """
    if datatype not in ds:
        raise QueryError(f"Datatype '{datatype}' not found in block")

    da = ds[datatype]

    if diagonal:
        return _read_diagonal(da, array_selections, diagonal)

    # Only apply selections for dims this datatype actually has. Reduced-rank
    # datatypes (the storage-efficient "unbroadcasted" form, e.g. CtrlMean over
    # ["Timepoint"]) simply don't vary along the dims they omit, so fixing such
    # a dim is a no-op for them rather than an error.
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    try:
        if applicable:
            da = da.sel(applicable)
    except KeyError as e:
        # Pinpoint which value(s) aren't valid labels so the error is
        # actionable — xarray's default only says "not all values found".
        missing = [
            f"{dim}={val!r}"
            for dim, val in applicable.items()
            if str(val) not in {str(x) for x in da.coords[dim].values.tolist()}
        ]
        if missing:
            raise QueryError(
                f"Selection value(s) not found in dataset: {', '.join(missing)}"
            ) from e
        raise QueryError(f"Selection error: {e}") from e

    arr = da.values

    if aggregate and aggregate.over in da.dims:
        agg_axis = list(da.dims).index(aggregate.over)
        arr = _aggregate_array(arr, agg_axis, aggregate)

    return arr


def _read_diagonal(
    da: xr.DataArray,
    array_selections: dict[str, int | str],
    diagonal: tuple[str, str],
) -> np.ndarray:
    """Extract diagonal values where two dimensions share coordinate labels.

    For each label that exists in both diagonal dimensions, reads the value
    at [dim_a=label, dim_b=label] (plus any other fixed selections).
    """
    # TODO(perf): Replace the per-label loop with xarray vectorized pointwise
    # selection: da.sel(dim_a=xr.DataArray(common), dim_b=xr.DataArray(common))
    # The current loop does ~8,500 individual .sel() calls for a typical
    # diagonal query and will be slower.
    dim_a, dim_b = diagonal

    # Same reduced-rank tolerance as the main read path: skip selections for
    # dims this datatype doesn't have.
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    if applicable:
        da = da.sel(applicable)

    labels_a = [str(lbl) for lbl in da.coords[dim_a].values]
    labels_b = [str(lbl) for lbl in da.coords[dim_b].values]
    common = sorted(set(labels_a) & set(labels_b))

    values = []
    for label in common:
        val = da.sel({dim_a: label, dim_b: label}).values
        values.append(float(val) if np.ndim(val) == 0 else float(val.flat[0]))

    return np.array(values, dtype=np.float32)


class QueryService:
    """Executes queries against xarray-exported Zarr stores on disk.

    Handles single-block and multi-block reads, within-block and cross-block
    aggregation, and parallel I/O via a thread pool. Each block is an
    independent xarray Dataset stored as Zarr, so concurrent reads are safe.
    """

    def __init__(self, thread_pool_size: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=thread_pool_size)

    def shutdown(self) -> None:
        """Shut down the thread pool, waiting for in-flight reads to finish.

        Called from the app's lifespan handler so the executor's worker
        threads are joined cleanly on shutdown rather than being abandoned.
        """
        self.executor.shutdown(wait=True)

    def execute(
        self,
        query: QueryIn,
        schema: SchemaDict,
        block_names: list[str],
        get_block_path: Callable[[str], Path],
    ) -> QueryOut:
        last_dim = schema["last_dimension"]

        # Separate last_dimension selection (folder routing) from array selections
        block_selection: str | None = None
        array_selections: dict[str, int | str] = {}
        for sel in query.select:
            if sel.dimension == last_dim:
                block_selection = str(sel.value)
            else:
                array_selections[sel.dimension] = sel.value

        target_blocks = [block_selection] if block_selection else block_names

        if not target_blocks:
            return QueryOut(blocks=[], shape=[], index=[], data={})

        agg_over_last_dim = (
            query.aggregate is not None and query.aggregate.over == last_dim
        )
        within_block_agg = (
            query.aggregate is not None and not agg_over_last_dim
        )

        # Read all blocks in parallel. Each _read_block call reads the queried
        # datatype for one block. Blocks are dispatched to the thread pool for
        # parallel I/O.
        all_results: dict[str, np.ndarray] = {}

        def _read_block(block_name: str) -> tuple[str, np.ndarray]:
            block_path = get_block_path(block_name)
            ds = xr.open_zarr(str(block_path))
            try:
                arr = _read_datatype_from_ds(
                    ds, query.datatype, array_selections,
                    query.aggregate if within_block_agg else None,
                    query.diagonal,
                )
            finally:
                ds.close()
            return block_name, arr

        if len(target_blocks) == 1:
            name, res = _read_block(target_blocks[0])
            all_results[name] = res
        else:
            futures = [self.executor.submit(_read_block, b) for b in target_blocks]
            for future in as_completed(futures):
                name, res = future.result()
                all_results[name] = res

        # Determine the queried datatype's spec for building the index
        dt_spec = None
        for dt in schema["datatypes"]:
            if dt["name"] == query.datatype:
                dt_spec = dt
                break

        if agg_over_last_dim:
            assert query.aggregate is not None  # guaranteed by agg_over_last_dim
            return self._aggregate_across_blocks(
                all_results, target_blocks, query.datatype, schema,
                query.aggregate, array_selections, query.diagonal,
                dt_spec,
            )

        if len(target_blocks) == 1:
            return self._single_block_response(
                all_results, target_blocks, query.datatype, schema,
                array_selections, within_block_agg, query,
                dt_spec,
            )

        return self._multi_block_response(
            all_results, target_blocks, query.datatype, schema,
            array_selections, within_block_agg, query, last_dim,
            dt_spec,
        )

    def _aggregate_across_blocks(
        self,
        all_results: dict[str, np.ndarray],
        target_blocks: list[str],
        datatype: str,
        schema: SchemaDict,
        aggregate: AggregateSpec,
        array_selections: dict[str, int | str],
        diagonal: tuple[str, str] | None,
        dt_spec: DatatypeDict | None,
    ) -> QueryOut:
        """Aggregate raw values across blocks.

        Collects raw per-block arrays, stacks them along axis 0, then applies the
        aggregation once — never mean-of-means (or median-of-medians, etc.).
        """
        stacked = np.stack([all_results[b] for b in target_blocks])
        sample_arr = _aggregate_array(stacked, 0, aggregate)
        data: dict[str, list | float | int | None] = {datatype: _numpy_to_json(sample_arr)}

        index = self._build_index(dt_spec, schema, array_selections, aggregate, diagonal)
        shape = list(sample_arr.shape) if sample_arr.ndim > 0 else []

        return QueryOut(
            blocks=target_blocks,
            aggregation=aggregate.type,
            shape=shape,
            index=index,
            data=data,
        )

    def _single_block_response(
        self,
        all_results: dict[str, np.ndarray],
        target_blocks: list[str],
        datatype: str,
        schema: SchemaDict,
        array_selections: dict[str, int | str],
        within_block_agg: bool,
        query: QueryIn,
        dt_spec: DatatypeDict | None,
    ) -> QueryOut:
        block = target_blocks[0]
        sample_arr = all_results[block]
        data: dict[str, list | float | int | None] = {datatype: _numpy_to_json(sample_arr)}

        index = self._build_index(
            dt_spec, schema, array_selections,
            query.aggregate if within_block_agg else None,
            query.diagonal,
        )
        shape = list(sample_arr.shape) if sample_arr.ndim > 0 else []

        return QueryOut(
            blocks=target_blocks,
            shape=shape,
            index=index,
            data=data,
        )

    def _multi_block_response(
        self,
        all_results: dict[str, np.ndarray],
        target_blocks: list[str],
        datatype: str,
        schema: SchemaDict,
        array_selections: dict[str, int | str],
        within_block_agg: bool,
        query: QueryIn,
        last_dim: str,
        dt_spec: DatatypeDict | None,
    ) -> QueryOut:
        """Build response for multi-block queries without cross-block aggregation.

        The last_dimension appears in the index as a regular dimension.
        Data arrays gain an extra leading dimension for blocks.
        """
        stacked = np.stack([all_results[b] for b in target_blocks])
        sample_arr = stacked
        data: dict[str, list | float | int | None] = {datatype: _numpy_to_json(stacked)}

        block_index = IndexLevel(dimension=last_dim, labels=target_blocks)
        inner_index = self._build_index(
            dt_spec, schema, array_selections,
            query.aggregate if within_block_agg else None,
            query.diagonal,
        )
        index = [block_index] + inner_index

        shape = list(sample_arr.shape)

        return QueryOut(
            blocks=target_blocks,
            shape=shape,
            index=index,
            data=data,
        )

    def _build_index(
        self,
        dt_spec: DatatypeDict | None,
        schema: SchemaDict,
        array_selections: dict[str, int | str],
        aggregate: AggregateSpec | None,
        diagonal: tuple[str, str] | None,
    ) -> list[IndexLevel]:
        """Build the response index (free dimensions and their labels).

        Uses the queried datatype's dimension list — not the full dataset
        dimensions — so reduced-rank datatypes (e.g. nCtrlCells with only
        timepoint) get the correct index.
        """
        if diagonal:
            dim_a, dim_b = diagonal
            labels_a = self._get_dim_labels(schema, dim_a)
            labels_b = self._get_dim_labels(schema, dim_b)
            common = sorted({str(lbl) for lbl in labels_a} & {str(lbl) for lbl in labels_b})
            return [IndexLevel(dimension="label", labels=common)]

        agg_dim = aggregate.over if aggregate else None
        dt_dims = dt_spec["dimensions"] if dt_spec else [d["name"] for d in schema["dimensions"]]

        index: list[IndexLevel] = []
        for dim_name in dt_dims:
            if dim_name in array_selections:
                continue
            if dim_name == agg_dim:
                continue
            labels = self._get_dim_labels(schema, dim_name)
            index.append(IndexLevel(dimension=dim_name, labels=labels))
        return index

    @staticmethod
    def _get_dim_labels(schema: SchemaDict, dim_name: str) -> list:
        for d in schema["dimensions"]:
            if d["name"] == dim_name:
                return d["labels"]
        raise QueryError(f"Unknown dimension: {dim_name}")
