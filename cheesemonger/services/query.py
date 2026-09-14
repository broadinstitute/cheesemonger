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
import pandas as pd
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
    block_list_size: int | None = None
    for sel in query.select:
        if sel.dimension == last_dim:
            if isinstance(sel.value, list):
                block_list_size = len(sel.value)
            else:
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
        # Aggregating over blocks collapses them to one; a scalar fixes one block.
        block_factor = 1
    elif block_list_size is not None:
        # A subset of blocks kept as a dimension.
        block_factor = max(block_list_size, 1)
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

    Uses arr.tolist() for fast conversion, then replaces NaN with None. NaN is
    not valid JSON (FastAPI serializes with allow_nan=False), so every NaN has
    to be turned into null here or the response fails to render.
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

    if arr.dtype == object:
        # String datatypes (e.g. CorrelateTarget) load as object arrays, and
        # _align_blocks NaN-fills the labels a block lacks — so an object array
        # can carry NaN even though it isn't a float array. `x != x` is only
        # true for NaN, and on object dtype numpy evaluates it element-wise.
        nan_mask = arr != arr
        if np.any(nan_mask):
            obj_arr = arr.copy()
            obj_arr[nan_mask] = None
            return obj_arr.tolist()

    return arr.tolist()


def _aggregate_array(arr: np.ndarray, axis: int, aggregate: AggregateSpec) -> np.ndarray:
    """Reduce ``arr`` along ``axis`` per the aggregation spec.

    Shared by within-block reduction (a datatype's own axis) and cross-block
    reduction (the stacked-blocks axis), so both paths behave identically. NaNs
    are ignored by mean/median/min/max/count, and never satisfy the threshold
    predicates — UNLESS ``aggregate.fill_value`` is set, in which case NaNs are
    replaced with it first (so "missing" counts as that value, e.g. 0).
    """
    # Optional pre-fill: treat missing cells as a constant rather than ignoring
    # them. Only float arrays carry NaN; int arrays have none, so this is a no-op
    # for them (np.isnan would also reject an int dtype).
    if aggregate.fill_value is not None and np.issubdtype(arr.dtype, np.floating):
        arr = np.where(np.isnan(arr), aggregate.fill_value, arr)

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


def _validate_selection_labels(
    array_selections: dict[str, str | list[str]], schema: SchemaDict
) -> None:
    """Reject selection labels that NO block in the dataset carries.

    ``dimensions[*].labels`` is the present union maintained at load, so it is
    exactly "every label some block has". Checking against it here is the only
    place a genuinely bad label — a typo, a UniProt accession, a gene the
    dataset never measured — is caught: the per-block read deliberately
    tolerates a label the block lacks (see ``_safe_sel``).

    Dimensions absent from the schema aren't label-validated (the router already
    rejects unknown dimension names, and a reduced-rank datatype may simply not
    carry the dim). Neither is one with no recorded labels: an empty list means
    "this schema doesn't say", not "this dimension has nothing", so validating
    against it would reject every selection on it.
    """
    known = {d["name"]: d["labels"] for d in schema["dimensions"]}
    missing: list[str] = []
    for dim, val in array_selections.items():
        if not known.get(dim):
            continue
        valid = set(known[dim])
        for v in val if isinstance(val, list) else [val]:
            if str(v) not in valid:
                missing.append(f"{dim}={v!r}")
    if missing:
        raise QueryError(
            f"Selection value(s) not found in dataset: {', '.join(missing)}"
        )


def _labels_present(index: pd.Index, wanted: list[str]) -> np.ndarray:
    """Mask of which ``wanted`` labels ``index`` carries.

    A one-label request (the common series query) is a single hash lookup. A
    long list goes through ``get_indexer``, the same vectorized C lookup
    ``.sel()`` would do, so the check costs no more than the selection it
    guards. ``get_indexer`` requires a unique index, hence the fallback.
    """
    if len(wanted) == 1:
        return np.array([wanted[0] in index])
    if index.is_unique:
        return np.asarray(index.get_indexer(pd.Index(wanted))) >= 0
    return np.fromiter((v in index for v in wanted), dtype=bool, count=len(wanted))


def _safe_sel(
    da: xr.DataArray, applicable: dict[str, str | list[str]]
) -> xr.DataArray:
    """``da.sel(applicable)``, NaN-filling labels THIS block doesn't carry.

    Screens measure different label sets, so a fixed selection can name a label
    the dataset has but this block lacks. That is the same "a gene a screen
    didn't measure reads as NaN" case ``_align_blocks`` handles for free
    dimensions — but xarray's ``.sel()`` raises KeyError on it, which would fail
    the whole multi-block query because one screen is missing one gene. So the
    labels the block has are selected, and the rest are reindexed in as NaN.

    Labels no block carries are rejected up front by
    ``_validate_selection_labels``, so anything missing here is genuinely
    unmeasured rather than a typo. Scalar-vs-list semantics are unchanged: a
    scalar still collapses its dimension, a list still keeps it in the order
    requested.
    """
    present: dict[str, str | list[str]] = {}
    fill: dict[str, list[str]] = {}
    collapse: list[str] = []
    # The block's own pandas indexes — membership is the same hash lookup
    # .sel() would do, so "missing here" is exactly "would have raised
    # KeyError". The hash table is built once per block and shared by every
    # datatype read from it.
    indexes = da.indexes
    for dim, val in applicable.items():
        wanted = val if isinstance(val, list) else [val]
        index = indexes.get(dim)
        if index is None:
            present[dim] = val  # no index to check against; let .sel decide
            continue
        have = _labels_present(index, wanted)
        if have.all():
            present[dim] = val
            continue
        # Take what this block has (as a list, which keeps the dim for the
        # reindex below), then widen back to the full request.
        present[dim] = [v for v, ok in zip(wanted, have, strict=True) if ok]
        fill[dim] = wanted
        if not isinstance(val, list):
            collapse.append(dim)

    da = da.sel(present)  # every label checked above, so this cannot KeyError
    if not fill:
        return da
    if da.dtype.kind in "TSU":
        # A string datatype can't hold the NaN fill — same promotion, and same
        # reason, as _align_blocks.
        da = da.astype(object)
    da = da.reindex(fill, fill_value=np.nan)  # type: ignore[arg-type]
    if collapse:
        # A scalar selection drops its dimension; the reindex re-added it as a
        # length-1 axis, so drop it back out.
        da = da.isel(dict.fromkeys(collapse, 0), drop=True)
    return da


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
    # dims this datatype doesn't have. Same NaN-fill for labels this block lacks.
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    if applicable:
        da = _safe_sel(da, applicable)

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
        if arr.dtype.kind in "TSU":
            # NumPy's fixed- and variable-width string dtypes can't hold the NaN
            # fill: promoting StringDType against a float raises outright, so a
            # string datatype (e.g. CorrelateTarget) has to become object dtype
            # first. Object arrays stack cleanly with unreindexed string ones,
            # and _numpy_to_json turns the NaN fills into null.
            arr = arr.astype(object)
        da = xr.DataArray(arr, dims=dims, coords=block_labels)
        da = da.reindex({dim: union[dim] for dim in dims}, fill_value=np.nan)  # type: ignore[arg-type]
        aligned.append(da.values)
    return aligned, [(dim, union[dim]) for dim in dims]


def _apply_filter_op_np(
    arr: np.ndarray, op: str, value: float | str | list[float | str]
) -> np.ndarray:
    """Return a boolean array (same shape as ``arr``) of cells passing the op.

    Evaluated in numpy on the already-read array so the filtered datatype is read
    exactly once (predicate + value gather share it). NaN never satisfies a
    comparison, so NaN cells are excluded automatically.
    """
    if op == "gt":
        return arr > value
    if op == "lt":
        return arr < value
    if op == "ge":
        return arr >= value
    if op == "le":
        return arr <= value
    if op == "eq":
        return arr == value
    if op == "in":
        return np.isin(arr, value)
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

    Fixed ``select`` dims are applied first (scalars collapse, lists subset); a
    selected label this block lacks comes back as NaN, which no predicate
    accepts, so it simply contributes no rows. The predicate on ``filt.datatype``
    yields a mask; every returned datatype is then read out AT the passing cells
    only. At most ``cap`` cells are kept.

    The filtered datatype is read once (its whole array is needed to evaluate the
    predicate) and reused to gather its passing values. Co-located datatypes are
    read pointwise via ``isel`` at the passing positions, so Zarr materializes
    only the chunks holding those cells instead of the entire cube — the win that
    makes a small ``limit`` cheap even on a large datatype.
    """
    if filt.datatype not in ds:
        raise QueryError(f"Datatype '{filt.datatype}' not found in block")

    da = ds[filt.datatype]
    applicable = {k: v for k, v in array_selections.items() if k in da.dims}
    if applicable:
        da = _safe_sel(da, applicable)

    free_dims = [str(d) for d in da.dims]

    # One full read of the filtered datatype; predicate evaluated in numpy so we
    # don't read it again to fetch the passing values.
    arr = np.asarray(da.values)
    mask = _apply_filter_op_np(arr, filt.op, filt.value)

    def select_other(dt: str) -> xr.DataArray:
        """The co-datatype's DataArray after the same fixed selections.

        Router validation guarantees co-datatypes share the filtered datatype's
        dimensions, and all variables in the block share coordinate arrays, so
        positions computed on ``da`` line up with ``other`` by dim name.
        """
        if dt not in ds:
            raise QueryError(f"Datatype '{dt}' not found in block")
        other = ds[dt]
        appl = {k: v for k, v in applicable.items() if k in other.dims}
        return _safe_sel(other, appl) if appl else other

    # Scalar case: everything was fixed by select, so the array is 0-d.
    if arr.ndim == 0:
        passing = bool(mask)
        n = 1 if passing else 0
        values: dict[str, list[str | int | float | None]] = {}
        for dt in return_dts:
            if not passing:
                values[dt] = []
            elif dt == filt.datatype:
                values[dt] = _cells_to_json(arr.reshape(1))
            else:
                values[dt] = _cells_to_json(
                    np.asarray(select_other(dt).values).reshape(1)
                )
        return [], {}, values, n

    idx = np.argwhere(mask)  # (n_pass, ndim), row-major order
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

    # Pointwise indexers keyed by dim name (order-independent). Shared "cells" dim
    # makes isel gather point i from (dim0=idx[i,0], dim1=idx[i,1], ...) jointly.
    point_indexers = {
        d: xr.DataArray(idx[:, axis], dims="cells")
        for axis, d in enumerate(free_dims)
    }
    picks = tuple(idx[:, axis] for axis in range(idx.shape[1]))
    values = {}
    for dt in return_dts:
        if dt == filt.datatype:
            values[dt] = _cells_to_json(arr[picks])  # already in memory
        else:
            gathered = select_other(dt).isel(point_indexers).values
            values[dt] = _cells_to_json(np.asarray(gathered))
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
        #
        # The block key routes to folders rather than an array axis, but follows
        # the same collapse-vs-keep rule as any other dimension:
        #   omitted -> span every block, block dim KEPT (spanning result)
        #   scalar  -> exactly one block, block dim COLLAPSED (fixed)
        #   list    -> a subset of blocks, block dim KEPT in requested order
        block_selection: str | list[str] | None = None
        array_selections: dict[str, str | list[str]] = {}
        for sel in query.select:
            if sel.dimension == last_dim:
                block_selection = sel.value
            else:
                array_selections[sel.dimension] = sel.value

        # Bad labels are caught here, against the dataset-wide union, so the
        # per-block read is free to treat a label a block lacks as NaN.
        _validate_selection_labels(array_selections, schema)

        if isinstance(block_selection, list):
            target_blocks = block_selection
            keep_block_dim = True
        elif block_selection is not None:
            target_blocks = [block_selection]
            keep_block_dim = False
        else:
            target_blocks = block_names
            keep_block_dim = True

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

        # A scalar block selection collapses the block dim (one folder, fixed);
        # a list or an omitted key keeps it, even if only one block is targeted.
        if not keep_block_dim:
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

        # Same routing rules as execute(): the block key selects a folder, a
        # subset of folders (list), or — omitted — all folders. Everything else
        # is an in-array selection. Filter output is tidy with the block key as a
        # coordinate column, so the block dim is always present regardless.
        block_selection: str | list[str] | None = None
        array_selections: dict[str, str | list[str]] = {}
        for sel in spec.select:
            if sel.dimension == last_dim:
                block_selection = sel.value
            else:
                array_selections[sel.dimension] = sel.value

        _validate_selection_labels(array_selections, schema)

        if isinstance(block_selection, list):
            target_blocks = block_selection
        elif block_selection is not None:
            target_blocks = [block_selection]
        else:
            target_blocks = block_names

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
