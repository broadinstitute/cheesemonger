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
from cheesemonger.schemas.filter import Filter, FilterIn, FilterOut
from cheesemonger.schemas.query import (
    AggregateSpec,
    IndexLevel,
    QueryIn,
    QueryOut,
)

logger = logging.getLogger(__name__)

# Hard cap on rows a single filter can return, regardless of the client's
# `limit`. An unselective predicate (e.g. Correlation > -inf) would otherwise
# stream the whole cube back; this bounds memory and payload size.
MAX_FILTER_ROWS = 100_000

# Conservative per-value byte estimate for a serialized result. A JSON float
# ("-0.12345678") plus list/punctuation overhead runs ~20-30 bytes; string
# datatypes are similar. Used to reject oversized /query results before reading.
BYTES_PER_RESULT_ELEMENT = 24


def human_bytes(n: int) -> str:
    """Human-readable byte size in decimal units (matches the GB-based cap)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} GB"


def estimate_result_elements(
    query: QueryIn, schema: SchemaDict, block_names: list[str]
) -> int:
    """Upper-bound the number of values a /query would return, from the schema
    and selections alone — no disk read.

    The result shape is deterministic before reading: it's the product of the
    free (unfixed, unaggregated) dimensions' label counts, times the number of
    datatypes, times the number of blocks when the query spans them. A scalar
    selection collapses a dim; a list selection subsets it; aggregation removes
    the reduced dim; a diagonal collapses to the shared-label axis (bounded by
    the smaller of the two dims). Cross-block aggregation collapses to one block.
    """
    last_dim = schema["last_dimension"]
    dt_dims = {d["name"]: d["dimensions"] for d in schema["datatypes"]}
    dim_labels = {d["name"]: d.get("labels", []) for d in schema["dimensions"]}
    queried_dims = dt_dims.get(query.datatypes[0], [])

    scalar_fixed: set[str] = set()
    list_sizes: dict[str, int] = {}
    block_selection: str | None = None
    for sel in query.select:
        if sel.dimension == last_dim:
            if not isinstance(sel.value, list):
                block_selection = sel.value
        elif isinstance(sel.value, list):
            list_sizes[sel.dimension] = len(sel.value)
        else:
            scalar_fixed.add(sel.dimension)

    agg_over = query.aggregate.over if query.aggregate else None

    if query.diagonal:
        # 1-D over the shared labels; bounded by the smaller of the two dims.
        a, b = query.diagonal
        sizes = [len(dim_labels.get(a, [])), len(dim_labels.get(b, []))]
        nonzero = [s for s in sizes if s]
        per_block = min(nonzero) if nonzero else 0
    else:
        per_block = 1
        for d in queried_dims:
            if d in scalar_fixed or d == agg_over:
                continue
            if d in list_sizes:
                per_block *= list_sizes[d]
            else:
                per_block *= max(len(dim_labels.get(d, [])), 1)

    if agg_over == last_dim or block_selection is not None:
        block_factor = 1
    else:
        block_factor = max(len(block_names), 1)

    return per_block * len(query.datatypes) * block_factor


def estimate_result_bytes(
    query: QueryIn, schema: SchemaDict, block_names: list[str]
) -> int:
    """Estimated serialized size of a /query result (see estimate_result_elements)."""
    return estimate_result_elements(query, schema, block_names) * BYTES_PER_RESULT_ELEMENT


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


def _safe_sel(
    da: xr.DataArray, applicable: dict[str, str | list[str]]
) -> xr.DataArray:
    """``da.sel(applicable)``, raising a QueryError that names bad labels.

    xarray's own KeyError only says "not all values found"; we pinpoint which
    value(s) aren't valid labels so the error is actionable. A selection value
    may be a scalar or a list; each element is checked.
    """
    try:
        return da.sel(applicable)
    except KeyError as e:
        missing = []
        for dim, val in applicable.items():
            valid = {str(x) for x in da.coords[dim].values.tolist()}
            for v in val if isinstance(val, list) else [val]:
                if str(v) not in valid:
                    missing.append(f"{dim}={v!r}")
        if missing:
            raise QueryError(
                f"Selection value(s) not found in dataset: {', '.join(missing)}"
            ) from e
        raise QueryError(f"Selection error: {e}") from e


def _read_datatype_from_ds(
    ds: xr.Dataset,
    datatype: str,
    array_selections: dict[str, str | list[str]],
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
    if applicable:
        da = _safe_sel(da, applicable)

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
    array_selections: dict[str, str | list[str]],
    diagonal: tuple[str, str],
) -> BlockRead:
    """Extract diagonal values where two dimensions share coordinate labels.

    For each label that exists in both diagonal dimensions, reads the value
    at [dim_a=label, dim_b=label] (plus any other fixed selections). The index
    is a single ``label`` level over the shared labels found in THIS block.
    """
    dim_a, dim_b = diagonal

    # Same reduced-rank tolerance as the main read path: skip selections for
    # dims this datatype doesn't have.
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    if applicable:
        da = da.sel(applicable)

    labels_a = [str(lbl) for lbl in da.coords[dim_a].values]
    labels_b = [str(lbl) for lbl in da.coords[dim_b].values]
    common = sorted(set(labels_a) & set(labels_b))
    if not common:
        return np.array([], dtype=np.float32), [("label", [])]

    # One vectorized pointwise selection instead of one .sel() per label: passing
    # DataArray indexers that share a dim makes xarray gather every
    # (dim_a=label, dim_b=label) point in a single call, so xarray/dask can batch
    # the reads rather than doing thousands of round trips. Any residual dim
    # (e.g. an unfixed timepoint) collapses to its first entry, matching the
    # previous per-label ``val.flat[0]`` behaviour.
    picker = xr.DataArray(common, dims="label")
    diag = da.sel({dim_a: picker, dim_b: picker})
    extra = [d for d in diag.dims if d != "label"]
    if extra:
        diag = diag.isel(dict.fromkeys(extra, 0))
    return diag.values.astype(np.float32), [("label", common)]


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


def _apply_filter_op(
    da: xr.DataArray, op: str, value: float | str | list[float | str]
) -> xr.DataArray:
    """Return a boolean DataArray (same dims as ``da``) of cells passing the op.

    NaN never satisfies a comparison, so NaN cells are excluded automatically.
    """
    if op == "gt":
        return da > value
    if op == "lt":
        return da < value
    if op == "ge":
        return da >= value
    if op == "le":
        return da <= value
    if op == "eq":
        return da == value
    if op == "in":
        return da.isin(value)  # type: ignore[arg-type]
    raise QueryError(f"Unknown filter op: {op}")


def _cells_to_json(arr_1d: np.ndarray) -> list[str | int | float | None]:
    """Per-cell JSON values, mapping NaN (float or object dtype) to None."""
    out: list[str | int | float | None] = []
    for v in arr_1d.tolist():
        if isinstance(v, float) and np.isnan(v):
            out.append(None)
        else:
            out.append(v)
    return out


# One block's filter result: the free dims (axis order), a coord-labels list per
# free dim, a values list per returned datatype, and the TRUE passing-cell count
# before any cap (coords/values themselves are capped; raw_count lets the caller
# detect truncation).
FilterBlockResult = tuple[
    list[str], dict[str, list[str]], dict[str, list[str | int | float | None]], int
]


def _filter_one_block(
    ds: xr.Dataset,
    filt: Filter,
    return_dts: list[str],
    array_selections: dict[str, str | list[str]],
    cap: int,
) -> FilterBlockResult:
    """Apply ``filt`` to one opened block and gather the passing cells.

    Fixed ``select`` dims are applied first (scalars collapse, lists subset).
    The predicate on ``filt.datatype`` yields a mask; every returned datatype is
    then read out at the passing cells. At most ``cap`` cells are materialized.
    """
    if filt.datatype not in ds:
        raise QueryError(f"Datatype '{filt.datatype}' not found in block")

    da = ds[filt.datatype]
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    if applicable:
        da = _safe_sel(da, applicable)

    mask = _apply_filter_op(da, filt.op, filt.value)
    free_dims = [str(d) for d in da.dims]

    def read_other(dt: str) -> np.ndarray:
        if dt == filt.datatype:
            base = da
        else:
            if dt not in ds:
                raise QueryError(f"Datatype '{dt}' not found in block")
            other = ds[dt]
            appl = {k: v for k, v in applicable.items() if k in other.dims}
            base = _safe_sel(other, appl) if appl else other
        # Match the mask's axis order so flat index tuples line up.
        return np.asarray(base.transpose(*da.dims).values)

    # Scalar case: everything was fixed by select, so the mask is 0-d.
    if da.ndim == 0:
        passing = bool(np.asarray(mask.values))
        n = 1 if passing else 0
        values = {
            dt: (_cells_to_json(read_other(dt).reshape(1)) if passing else [])
            for dt in return_dts
        }
        return [], {}, values, n

    idx = np.argwhere(np.asarray(mask.values))  # (n_pass, ndim), row-major order
    raw_count = idx.shape[0]
    if raw_count > cap:
        idx = idx[:cap]  # bound per-block memory; caller flags truncation

    coords: dict[str, list[str]] = {}
    for axis, d in enumerate(free_dims):
        if d in da.coords:
            labels = np.asarray(da.coords[d].values)
            coords[d] = [str(labels[i]) for i in idx[:, axis]]
        else:
            coords[d] = [str(i) for i in idx[:, axis]]

    picks = tuple(idx[:, axis] for axis in range(idx.shape[1]))
    values = {dt: _cells_to_json(read_other(dt)[picks]) for dt in return_dts}
    return free_dims, coords, values, raw_count


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
        # Selection values are already strings (Selection canonicalizes them, to
        # match the string coordinate labels — see loader._stringify_coords). A
        # list value keeps its dimension in the result (xarray .sel(list)); a
        # scalar collapses it.
        block_selection: str | None = None
        array_selections: dict[str, str | list[str]] = {}
        for sel in query.select:
            if sel.dimension == last_dim:
                if isinstance(sel.value, list):
                    raise QueryError(
                        f"A list of values for the block key {last_dim!r} is not "
                        f"supported; select a single block, or omit it to span all."
                    )
                block_selection = sel.value
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

    def execute_filter(
        self,
        spec: FilterIn,
        schema: SchemaDict,
        block_names: list[str],
        get_block_path: Callable[[str], Path],
    ) -> FilterOut:
        """Return the cells of a datatype that pass a predicate, with coords.

        Results are tidy/long: one record per passing cell. The block key
        (last dimension) is just another coordinate column, so results span
        blocks and each record carries its block. Read blocks in parallel.
        """
        last_dim = schema["last_dimension"]

        # Same routing rules as execute(): the block key selects a folder (or,
        # omitted, all folders); everything else is an in-array selection.
        block_selection: str | None = None
        array_selections: dict[str, str | list[str]] = {}
        for sel in spec.select:
            if sel.dimension == last_dim:
                if isinstance(sel.value, list):
                    raise QueryError(
                        f"A list of values for the block key {last_dim!r} is not "
                        f"supported; select a single block, or omit it to span all."
                    )
                block_selection = sel.value
            else:
                array_selections[sel.dimension] = sel.value

        target_blocks = [block_selection] if block_selection else block_names

        # Filtered datatype first, then any extra co-located datatypes to read.
        return_dts = [spec.filter.datatype]
        for dt in spec.datatypes:
            if dt not in return_dts:
                return_dts.append(dt)

        cap = min(spec.limit, MAX_FILTER_ROWS) if spec.limit else MAX_FILTER_ROWS

        if not target_blocks:
            return FilterOut(
                dimensions=[], coords={}, data={dt: [] for dt in return_dts},
                count=0, truncated=False,
            )

        def _run(block_name: str) -> FilterBlockResult:
            block_path = get_block_path(block_name)
            try:
                ds = xr.open_zarr(str(block_path))
                try:
                    return _filter_one_block(
                        ds, spec.filter, return_dts, array_selections, cap,
                    )
                finally:
                    ds.close()
            except QueryError:
                raise
            except Exception as e:
                raise QueryError(f"Failed reading block {block_name!r}: {e}") from e

        if len(target_blocks) == 1:
            per_block = [(target_blocks[0], _run(target_blocks[0]))]
        else:
            results = self.executor.map(_run, target_blocks)
            per_block = list(zip(target_blocks, results, strict=True))

        # Concatenate block results into parallel columns; the block key becomes
        # a coordinate column (Screen), so cross-block results stay attributable.
        dims_out: list[str] = []
        coords_out: dict[str, list[str]] = {}
        data_out: dict[str, list[str | int | float | None]] = {dt: [] for dt in return_dts}
        total = 0
        truncated = False

        for block_name, (free_dims, coords, values, raw_count) in per_block:
            if not dims_out and free_dims:
                dims_out = free_dims
                coords_out = {d: [] for d in free_dims}
                coords_out[last_dim] = []
            elif last_dim not in coords_out:
                coords_out[last_dim] = []

            available = len(values[return_dts[0]])  # already capped to `cap`
            take = min(available, cap - total)
            if take < raw_count:  # per-block or global cap cut this block short
                truncated = True
            if take <= 0:
                continue

            for d in dims_out:
                coords_out[d].extend(coords[d][:take])
            coords_out[last_dim].extend([block_name] * take)
            for dt in return_dts:
                data_out[dt].extend(values[dt][:take])
            total += take

        dimensions = [*dims_out, last_dim] if (dims_out or total) else []
        return FilterOut(
            dimensions=dimensions,
            coords=coords_out if dimensions else {},
            data=data_out,
            count=total,
            truncated=truncated,
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
