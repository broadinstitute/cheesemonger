"""Query engine — reads xarray-exported Zarr stores and applies selections.

The source data is written by xarray.Dataset.to_zarr(), which embeds
dimension names and coordinate labels inside the Zarr store. We read
with xarray.open_zarr() to get label-based .sel() indexing for free,
rather than doing manual label→integer index lookups on raw Zarr arrays.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.models.dataset import SchemaDict
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


# A read returns the values array plus the labels of each free (non-collapsed)
# dimension, IN ARRAY-AXIS ORDER — captured from the block's OWN Zarr coordinates
# so the response index always matches the data length, even when this block's
# label set differs from the dataset-level present union.
BlockRead = tuple[np.ndarray, "list[tuple[str, list[str]]]"]


def _free_coords(da: xr.DataArray, exclude: str | None = None) -> list[tuple[str, list[str]]]:
    """The labels of each of ``da``'s dims (in axis order), from its coordinates.

    ``exclude`` drops one dim (the aggregated axis). Falls back to integer
    positions for a dim with no coordinate array.
    """
    out: list[tuple[str, list[str]]] = []
    for d in da.dims:
        if d == exclude:
            continue
        if d in da.coords:
            labels = [str(v) for v in np.asarray(da.coords[d].values).tolist()]
        else:
            labels = [str(i) for i in range(int(da.sizes[d]))]
        out.append((str(d), labels))
    return out


def _read_datatype_from_ds(
    ds: xr.Dataset,
    datatype: str,
    array_selections: dict[str, str],
    aggregate: AggregateSpec | None,
    diagonal: tuple[str, str] | None,
) -> BlockRead:
    """Read one datatype from an already-opened xarray Dataset.

    Returns the values array and the free-dimension coordinate labels (from this
    block's own coordinates). The caller opens and closes the Dataset.
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

    agg_over = aggregate.over if (aggregate and aggregate.over in da.dims) else None
    if agg_over is not None:
        assert aggregate is not None  # guaranteed by agg_over is not None
        agg_axis = list(da.dims).index(agg_over)
        arr = _aggregate_array(arr, agg_axis, aggregate)

    # Index labels come from this block's remaining coordinates (minus the
    # aggregated axis) — the same artifact that sets the array's length.
    return arr, _free_coords(da, exclude=agg_over)


def _read_diagonal(
    da: xr.DataArray,
    array_selections: dict[str, str],
    diagonal: tuple[str, str],
) -> BlockRead:
    """Extract diagonal values where two dimensions share coordinate labels.

    For each label that exists in both diagonal dimensions, reads the value
    at [dim_a=label, dim_b=label] (plus any other fixed selections). The index
    is a single ``label`` level over the shared labels found in THIS block.
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

    return np.array(values, dtype=np.float32), [("label", common)]


def _index_from_coords(free_coords: list[tuple[str, list[str]]]) -> list[IndexLevel]:
    """Turn captured (dim, labels) pairs into response index levels."""
    return [IndexLevel(dimension=dim, labels=labels) for dim, labels in free_coords]


def _align_blocks(
    reads: list[BlockRead],
) -> tuple[list[np.ndarray], list[tuple[str, list[str]]]]:
    """Align per-block arrays to the union of their labels along every free dim.

    Screens measure different label sets, so their raw arrays differ in shape.
    Each block is reindexed onto the per-dim union (first-seen order across
    blocks), filling labels the block lacks with NaN — the biological model that
    "a gene a screen didn't measure reads as NaN". Returns the aligned arrays
    (all now the same shape) and the union coords for the response index.

    Fast path: when every block already carries identical labels, the reindex is
    a no-op and the shapes were equal anyway.
    """
    _, first_coords = reads[0]
    dims = [dim for dim, _ in first_coords]

    if not dims:
        # Scalar per block (every dim selected/aggregated away) — nothing to align.
        return [arr for arr, _ in reads], []

    # Per-dim union of labels, first-seen across blocks (deterministic order).
    union: dict[str, list[str]] = {}
    for i, dim in enumerate(dims):
        seen: set[str] = set()
        merged: list[str] = []
        for _, coords in reads:
            for lbl in coords[i][1]:
                if lbl not in seen:
                    seen.add(lbl)
                    merged.append(lbl)
        union[dim] = merged

    aligned: list[np.ndarray] = []
    for arr, coords in reads:
        block_labels = {dim: coords[i][1] for i, dim in enumerate(dims)}
        if all(block_labels[dim] == union[dim] for dim in dims):
            aligned.append(arr)  # already full — skip the reindex
            continue
        da = xr.DataArray(arr, dims=dims, coords=block_labels)
        da = da.reindex({dim: union[dim] for dim in dims}, fill_value=np.nan)  # type: ignore[arg-type]
        aligned.append(da.values)
    return aligned, [(dim, union[dim]) for dim in dims]


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

        # Separate last_dimension selection (folder routing) from array selections.
        # Coordinate labels are stored as strings (see loader._stringify_coords),
        # so coerce selection values to str — a client may send timepoint=4 or
        # rank=0 as ints, but the store keys on "4"/"0".
        block_selection: str | None = None
        array_selections: dict[str, str] = {}
        for sel in query.select:
            if sel.dimension == last_dim:
                block_selection = str(sel.value)
            else:
                array_selections[sel.dimension] = str(sel.value)

        target_blocks = [block_selection] if block_selection else block_names

        if not target_blocks:
            return QueryOut(blocks=[], shape=[], index=[], data={})

        agg_over_last_dim = (
            query.aggregate is not None and query.aggregate.over == last_dim
        )
        within_block_agg = (
            query.aggregate is not None and not agg_over_last_dim
        )

        # Read all blocks in parallel. Each _read_block opens a block's store
        # once and reads every requested datatype from it (shared coordinates,
        # one open). Blocks are dispatched to the thread pool for parallel I/O.
        all_results: dict[str, dict[str, BlockRead]] = {}

        def _read_block(block_name: str) -> dict[str, BlockRead]:
            block_path = get_block_path(block_name)
            try:
                ds = xr.open_zarr(str(block_path))
                try:
                    return {
                        dt: _read_datatype_from_ds(
                            ds, dt, array_selections,
                            query.aggregate if within_block_agg else None,
                            query.diagonal,
                        )
                        for dt in query.datatypes
                    }
                finally:
                    ds.close()
            except QueryError:
                raise
            except Exception as e:
                # Attach the block name so failures point at the problematic folder.
                raise QueryError(f"Failed reading block {block_name!r}: {e}") from e

        if len(target_blocks) == 1:
            all_results[target_blocks[0]] = _read_block(target_blocks[0])
        else:
            # executor.map yields results in submission order, so zip re-pairs
            # each result with its block without threading the name through.
            results = self.executor.map(_read_block, target_blocks)
            all_results = dict(zip(target_blocks, results, strict=True))

        if agg_over_last_dim:
            assert query.aggregate is not None  # guaranteed by agg_over_last_dim
            return self._aggregate_across_blocks(
                all_results, target_blocks, query.datatypes, query.aggregate,
            )

        if len(target_blocks) == 1:
            return self._single_block_response(
                all_results, target_blocks, query.datatypes,
            )

        return self._multi_block_response(
            all_results, target_blocks, query.datatypes, last_dim,
        )

    def _aggregate_across_blocks(
        self,
        all_results: dict[str, dict[str, BlockRead]],
        target_blocks: list[str],
        datatypes: list[str],
        aggregate: AggregateSpec,
    ) -> QueryOut:
        """Aggregate raw values across blocks.

        Aligns per-block arrays to the union of their labels (NaN-filling what a
        block lacks), stacks along axis 0, then applies the aggregation once —
        never mean-of-means. NaN-aware reducers ignore the fills, so a gene
        measured in only some screens is aggregated over the screens that have
        it. The index is the union coords.
        """
        data: dict[str, list | float | int | None] = {}
        sample_arr = None
        index_coords: list[tuple[str, list[str]]] = []
        for dt in datatypes:
            aligned, union_coords = _align_blocks([all_results[b][dt] for b in target_blocks])
            agg_result = _aggregate_array(np.stack(aligned), 0, aggregate)
            data[dt] = _numpy_to_json(agg_result)
            if sample_arr is None:
                sample_arr = agg_result
                index_coords = union_coords

        index = _index_from_coords(index_coords)
        shape = list(sample_arr.shape) if sample_arr is not None and sample_arr.ndim > 0 else []

        return QueryOut(
            blocks=target_blocks,
            aggregation=aggregate.type,
            shape=shape,
            index=index,
            data=data,
        )

    def _single_block_response(
        self,
        all_results: dict[str, dict[str, BlockRead]],
        target_blocks: list[str],
        datatypes: list[str],
    ) -> QueryOut:
        block = target_blocks[0]
        data: dict[str, list | float | int | None] = {}
        sample_arr = None
        for dt in datatypes:
            arr, _ = all_results[block][dt]
            data[dt] = _numpy_to_json(arr)
            if sample_arr is None:
                sample_arr = arr

        # Index labels come from THIS block's coordinates (captured at read), so
        # the index length always matches the data.
        index = _index_from_coords(all_results[block][datatypes[0]][1])
        shape = list(sample_arr.shape) if sample_arr is not None and sample_arr.ndim > 0 else []

        return QueryOut(
            blocks=target_blocks,
            shape=shape,
            index=index,
            data=data,
        )

    def _multi_block_response(
        self,
        all_results: dict[str, dict[str, BlockRead]],
        target_blocks: list[str],
        datatypes: list[str],
        last_dim: str,
    ) -> QueryOut:
        """Build response for multi-block queries without cross-block aggregation.

        The last_dimension appears in the index as a regular dimension. Data
        arrays gain an extra leading dimension for blocks. Blocks whose label
        sets differ are aligned to the union with NaN-fill (see _align_blocks);
        the inner index is the union coords.
        """
        data: dict[str, list | float | int | None] = {}
        sample_arr = None
        inner_coords: list[tuple[str, list[str]]] = []
        for dt in datatypes:
            aligned, union_coords = _align_blocks([all_results[b][dt] for b in target_blocks])
            stacked = np.stack(aligned)
            data[dt] = _numpy_to_json(stacked)
            if sample_arr is None:
                sample_arr = stacked
                inner_coords = union_coords

        block_index = IndexLevel(dimension=last_dim, labels=target_blocks)
        index = [block_index, *_index_from_coords(inner_coords)]

        shape = list(sample_arr.shape) if sample_arr is not None else []

        return QueryOut(
            blocks=target_blocks,
            shape=shape,
            index=index,
            data=data,
        )
