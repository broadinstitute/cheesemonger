"""
Query interface for cheesemonger datasets.

Provides get_vector() — a single entry point for all supported query patterns:
series, aggregation, and diagonal extraction.

Works with the single-store model where screen is a dimension inside a 4-D
array (screen, timepoint, testedperturbation, testedgeneexpression).
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr


# ---------------------------------------------------------------------------
# Aggregation types
# ---------------------------------------------------------------------------

class Aggregate(enum.Enum):
    NONE = "none"
    MEAN = "mean"
    COUNT_LT = "count_lt"


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def open_store(store_path: Path | str, fmt: str) -> xr.Dataset:
    """
    Open a Zarr store or NetCDF file and return a lazy xr.Dataset.

    For Zarr, data is backed by dask arrays automatically.  Accepts gs://
    URIs (requires gcsfs).  For NetCDF, pass chunks={} to get lazy
    dask-backed loading.
    """
    if fmt == "zarr":
        return xr.open_zarr(str(store_path))
    elif fmt == "netcdf":
        return xr.open_dataset(str(store_path), chunks={})
    else:
        raise ValueError(f"Unknown format: {fmt!r}")


# ---------------------------------------------------------------------------
# get_vector
# ---------------------------------------------------------------------------

def get_vector(
    store_path: Path | str,
    fmt: str,
    datatype: str,
    constraints: dict[str, Any] | None = None,
    aggregate: Aggregate = Aggregate.NONE,
    aggregate_over: str | None = None,
    aggregate_threshold: float | None = None,
    diagonal: tuple[str, str] | None = None,
) -> xr.DataArray:
    """
    Fetch a vector of values from a cheesemonger dataset.

    Parameters
    ----------
    store_path : Path
        Path to the Zarr store directory or NetCDF file.
    fmt : str
        Storage format — "zarr" or "netcdf".
    datatype : str
        Name of the datatype shard to read (e.g. "ZScore", "L2FC").
    constraints : dict
        Dimension values to fix, e.g.
        {"screen": "Screen_000", "timepoint": 4, "testedperturbation": "Gene_05000"}.
        Unconstrained dimensions remain free in the result.
    aggregate : Aggregate
        NONE     — return the raw selection.
        MEAN     — compute mean over ``aggregate_over`` dimension.
        COUNT_LT — count values < ``aggregate_threshold`` along
                    ``aggregate_over`` dimension.
    aggregate_over : str
        Dimension to aggregate across (required when aggregate != NONE).
    aggregate_threshold : float
        Threshold for COUNT_LT aggregation.
    diagonal : tuple[str, str]
        Two dimension names whose coordinates should be aligned to extract
        the diagonal (e.g. ("testedperturbation", "testedgeneexpression")
        for self-targeting queries).

    Returns
    -------
    xr.DataArray
        The resulting vector, fully materialized (not lazy).
    """
    ds = open_store(store_path, fmt)
    da = ds[datatype]

    if constraints:
        da = da.sel(**constraints)

    if diagonal is not None:
        da = _extract_diagonal(da, diagonal)
    elif aggregate == Aggregate.MEAN:
        if aggregate_over is None:
            raise ValueError("aggregate_over is required for MEAN aggregation")
        da = da.mean(dim=aggregate_over)
    elif aggregate == Aggregate.COUNT_LT:
        if aggregate_over is None:
            raise ValueError("aggregate_over is required for COUNT_LT aggregation")
        if aggregate_threshold is None:
            raise ValueError("aggregate_threshold is required for COUNT_LT aggregation")
        da = (da < aggregate_threshold).sum(dim=aggregate_over)

    result = da.compute()
    ds.close()
    return result


def _extract_diagonal(
    da: xr.DataArray,
    dims: tuple[str, str],
) -> xr.DataArray:
    """
    Extract diagonal values where two dimensions share coordinate labels.

    For example, with dims=("testedperturbation", "testedgeneexpression"),
    returns the value at (perturbation=X, expression=X) for every X that
    appears in both coordinate arrays.
    """
    dim_a, dim_b = dims
    labels_a = da.coords[dim_a].values
    labels_b = da.coords[dim_b].values

    common = np.intersect1d(labels_a, labels_b)
    if len(common) == 0:
        return xr.DataArray(
            np.array([], dtype=da.dtype),
            dims=["label"],
            coords={"label": np.array([], dtype=labels_a.dtype)},
        )

    da_selected = da.sel({dim_a: common, dim_b: common})
    idx = np.arange(len(common))
    values = da_selected.values[..., idx, idx]

    remaining_dims = [d for d in da.dims if d not in dims]
    if values.ndim == 1:
        return xr.DataArray(values, dims=["label"], coords={"label": common})

    result_coords: dict = {"label": common}
    for d in remaining_dims:
        result_coords[d] = da.coords[d].values
    return xr.DataArray(values, dims=remaining_dims + ["label"], coords=result_coords)
