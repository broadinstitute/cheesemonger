"""
Generate simulated PESCA-like datasets in Zarr and/or NetCDF format.

Usage:
    python -m cheesemonger simulate --help

The script generates one screen at a time and appends it to the store,
writing one variable at a time to keep peak memory low (~1.5 GB per
variable instead of ~20+ GB for all 15 simultaneously).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np

from cheesemonger.schema import DatasetSchema, DatatypeSpec, pesca_schema

logger = logging.getLogger(__name__)


def _rss_mb() -> float:
    """Current RSS of this process in MB (Linux/macOS)."""
    try:
        import resource
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        if hasattr(rusage, "ru_maxrss"):
            # macOS reports in bytes, Linux in KB
            divisor = 1024 * 1024 if os.uname().sysname == "Darwin" else 1024
            return rusage.ru_maxrss / divisor
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Dimension coordinate generation
# ---------------------------------------------------------------------------

def make_coords(schema: DatasetSchema) -> dict[str, np.ndarray]:
    """Generate synthetic coordinate labels for each dimension."""
    coords: dict[str, np.ndarray] = {}
    for dim, size in schema.dim_sizes.items():
        if dim == "screen":
            coords[dim] = np.array([f"Screen_{i:03d}" for i in range(size)])
        elif dim == "timepoint":
            coords[dim] = np.array([4, 7][:size])
        elif dim == "testedperturbation":
            coords[dim] = np.array([f"Gene_{i:05d}" for i in range(size)])
        elif dim == "testedgeneexpression":
            coords[dim] = np.array([f"RGene_{i:05d}" for i in range(size)])
        else:
            coords[dim] = np.array([f"{dim}_{i}" for i in range(size)])
    return coords


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

CHUNK_PRESETS = {
    "big": {
        "testedperturbation": 1000,
        "testedgeneexpression": 5000,
        "default": 1000,
    },
    "small": {
        "testedperturbation": 250,
        "testedgeneexpression": 1000,
        "default": 250,
    },
}


def chunk_sizes(
    dt: DatatypeSpec,
    schema: DatasetSchema,
    chunk_preset: str = "big",
) -> tuple[int, ...]:
    """
    Choose chunk sizes for a datatype shard.

    Strategy: chunk size 1 along screen and timepoint (low cardinality, often
    constrained in queries), and larger chunks along testedperturbation/
    testedgeneexpression to give good read performance for series queries.

    Presets:
        big   — ~20 MB chunks: (1, 1, 1000, 5000). Series query = 4 chunks.
        small — ~1 MB chunks:  (1, 1, 250, 1000).  Series query = 18 chunks.
    """
    preset = CHUNK_PRESETS[chunk_preset]
    chunks = []
    for dim in dt.dimensions:
        size = schema.dim_sizes[dim]
        if dim in ("screen", "timepoint"):
            chunks.append(1)
        else:
            target = preset.get(dim, preset["default"])
            chunks.append(min(target, size))
    return tuple(chunks)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _fill_random_f32(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    """Generate a float32 random normal array without a full-size float64 temp."""
    arr = np.empty(shape, dtype=np.float32)
    # Slice along the first axis to keep the float64 intermediate small.
    # For shape (2, 10000, 18000) this halves the temp from 2.88 GB to 1.44 GB.
    for i in range(shape[0]):
        arr[i] = rng.standard_normal(shape[1:]).astype(np.float32)
    return arr


def write_zarr(
    schema: DatasetSchema,
    output_dir: Path,
    coords: dict[str, np.ndarray],
    seed: int = 42,
    chunk_preset: str = "big",
    scale: str = "small",
) -> Path:
    """
    Write a simulated dataset to a Zarr store, one variable at a time.

    Uses the zarr library directly so that only one variable's data for one
    screen is in memory at any time.  The ``_ARRAY_DIMENSIONS`` attribute on
    each array ensures the store is readable by ``xr.open_zarr()``.
    """
    import numcodecs
    import zarr

    store_path = output_dir / f"{schema.name}_{scale}.zarr"
    if store_path.exists():
        shutil.rmtree(store_path)

    rng = np.random.default_rng(seed)
    n_screens = schema.dim_sizes[schema.append_dim]
    append_dim = schema.append_dim

    root = zarr.open_group(str(store_path), mode="w")

    # ── coordinate arrays ──────────────────────────────────────────────
    for dim_name, labels in coords.items():
        if labels.dtype.kind in ("U", "S"):
            z_coord = root.create_dataset(
                dim_name,
                data=np.array(labels, dtype=object),
                object_codec=numcodecs.VLenUTF8(),
                chunks=(len(labels),),
                overwrite=True,
            )
        else:
            z_coord = root.create_dataset(
                dim_name,
                data=labels,
                chunks=(len(labels),),
                overwrite=True,
            )
        z_coord.attrs["_ARRAY_DIMENSIONS"] = [dim_name]

    # ── create empty data arrays ───────────────────────────────────────
    for dt in schema.datatypes:
        full_shape = tuple(schema.dim_sizes[dim] for dim in dt.dimensions)
        chunks = chunk_sizes(dt, schema, chunk_preset)
        z_var = root.create_dataset(
            dt.name,
            shape=full_shape,
            dtype=dt.dtype,
            chunks=chunks,
            fill_value=0.0,
            overwrite=True,
        )
        z_var.attrs["_ARRAY_DIMENSIONS"] = list(dt.dimensions)

    # ── fill data screen-by-screen, variable-by-variable ───────────────
    for screen_idx in range(n_screens):
        t0 = time.perf_counter()
        for dt_i, dt in enumerate(schema.datatypes):
            shape_no_screen = tuple(
                schema.dim_sizes[dim]
                for dim in dt.dimensions if dim != append_dim
            )
            arr = _fill_random_f32(rng, shape_no_screen)

            dim_idx = list(dt.dimensions).index(append_dim)
            slices = [slice(None)] * len(dt.dimensions)
            slices[dim_idx] = screen_idx
            root[dt.name][tuple(slices)] = arr
            del arr

            logger.debug(
                "  var %d/%d (%s)  RSS=%.0f MB",
                dt_i + 1, len(schema.datatypes), dt.name, _rss_mb(),
            )

        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d written to Zarr (%.1fs)  RSS=%.0f MB",
            screen_idx + 1, n_screens, elapsed, _rss_mb(),
        )

    zarr.consolidate_metadata(str(store_path))
    logger.info("Zarr store written to %s", store_path)
    return store_path


def write_netcdf(
    schema: DatasetSchema,
    output_dir: Path,
    coords: dict[str, np.ndarray],
    seed: int = 42,
    chunk_preset: str = "big",
    scale: str = "small",
) -> Path:
    """
    Write a simulated dataset to a NetCDF4 file, one screen at a time.

    Uses the netCDF4 library directly for incremental writes so that only
    one screen's data is in memory at any time.  The screen dimension is
    created as UNLIMITED so it can be extended later.
    """
    import netCDF4 as nc4

    out_path = output_dir / f"{schema.name}_{scale}.nc"
    if out_path.exists():
        out_path.unlink()

    rng = np.random.default_rng(seed)
    n_screens = schema.dim_sizes[schema.append_dim]
    append_dim = schema.append_dim

    ncfile = nc4.Dataset(str(out_path), "w", format="NETCDF4")
    ncfile.set_fill_off()

    append_is_string = coords[append_dim].dtype.kind == "U"

    for dim_name, labels in coords.items():
        if dim_name == append_dim:
            ncfile.createDimension(dim_name, None)
        else:
            ncfile.createDimension(dim_name, len(labels))

        if labels.dtype.kind == "U":
            max_len = max(len(s) for s in labels)
            nc_dt = np.dtype(f"S{max_len}")
            coord_var = ncfile.createVariable(dim_name, nc_dt, (dim_name,))
            if dim_name != append_dim:
                coord_var[:] = np.array(labels, dtype=nc_dt)
        else:
            coord_var = ncfile.createVariable(dim_name, labels.dtype, (dim_name,))
            if dim_name != append_dim:
                coord_var[:] = labels

    for dt in schema.datatypes:
        chunks = chunk_sizes(dt, schema, chunk_preset)

        var = ncfile.createVariable(
            dt.name,
            dt.dtype,
            dt.dimensions,
            chunksizes=chunks,
            zlib=True,
            complevel=1,
            fill_value=False,
        )
        var.set_var_chunk_cache(32 * 1024 * 1024, 521, 0.75)

    for screen_idx in range(n_screens):
        t0 = time.perf_counter()
        screen_label = coords[append_dim][screen_idx]

        if append_is_string:
            ncfile.variables[append_dim][screen_idx] = np.bytes_(screen_label)
        else:
            ncfile.variables[append_dim][screen_idx] = screen_label

        for dt_i, dt_spec in enumerate(schema.datatypes):
            shape_no_screen = tuple(
                schema.dim_sizes[dim]
                for dim in dt_spec.dimensions if dim != append_dim
            )
            arr = _fill_random_f32(rng, shape_no_screen)

            dim_idx = list(dt_spec.dimensions).index(append_dim)
            slices = [slice(None)] * len(dt_spec.dimensions)
            slices[dim_idx] = screen_idx
            ncfile.variables[dt_spec.name][tuple(slices)] = arr
            del arr
            ncfile.sync()

            logger.debug(
                "  var %d/%d (%s)  RSS=%.0f MB",
                dt_i + 1, len(schema.datatypes), dt_spec.name, _rss_mb(),
            )

        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d written to NetCDF (%.1fs)  RSS=%.0f MB",
            screen_idx + 1, n_screens, elapsed, _rss_mb(),
        )

    ncfile.close()
    logger.info("NetCDF file written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SCALE_PRESETS = {
    "small": {"n_screens": 2, "n_timepoints": 2, "n_testedperturbations": 100, "n_testedgeneexpressions": 200},
    "medium": {"n_screens": 5, "n_timepoints": 2, "n_testedperturbations": 5_000, "n_testedgeneexpressions": 10_000},
    "single_screen": {"n_screens": 1, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
    "triple_screens": {"n_screens": 3, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
    "full": {"n_screens": 30, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
}


def _estimate_size(schema: DatasetSchema) -> float:
    """Estimate total uncompressed size in GB."""
    total_elements = 0
    for dt in schema.datatypes:
        n = 1
        for dim in dt.dimensions:
            n *= schema.dim_sizes[dim]
        total_elements += n
    return total_elements * 4 / (1024**3)


def main():
    parser = argparse.ArgumentParser(
        description="Generate simulated PESCA datasets for benchmarking."
    )
    parser.add_argument(
        "--scale",
        choices=list(SCALE_PRESETS.keys()),
        default="small",
        help="Preset scale (default: small). "
        + ", ".join(f"{k}: {v}" for k, v in SCALE_PRESETS.items()),
    )
    parser.add_argument(
        "--format",
        choices=["zarr", "netcdf", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/simulated"),
        help="Output directory (default: data/simulated)",
    )
    parser.add_argument(
        "--chunk-preset",
        choices=list(CHUNK_PRESETS.keys()),
        default="big",
        help="Chunk size preset (default: big). "
             "big=~20MB chunks (1,1,1000,5000), small=~1MB chunks (1,1,250,1000)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    preset = SCALE_PRESETS[args.scale]
    schema = pesca_schema(**preset)
    chunk_preset = args.chunk_preset

    sample_chunks = chunk_sizes(schema.datatypes[0], schema, chunk_preset)
    chunk_mb = np.prod(sample_chunks) * 4 / (1024 ** 2)

    est_gb = _estimate_size(schema)
    logger.info(
        "Scale=%s  chunks=%s (~%.1f MB)  dims=%s  estimated uncompressed=%.2f GB",
        args.scale,
        sample_chunks,
        chunk_mb,
        dict(schema.dim_sizes),
        est_gb,
    )

    out_dir = args.output_dir / f"chunks_{chunk_preset}"
    out_dir.mkdir(parents=True, exist_ok=True)
    coords = make_coords(schema)

    if args.format in ("zarr", "both"):
        t0 = time.perf_counter()
        path = write_zarr(schema, out_dir, coords, seed=args.seed, chunk_preset=chunk_preset, scale=args.scale)
        logger.info("Zarr complete in %.1fs -> %s", time.perf_counter() - t0, path)

    if args.format in ("netcdf", "both"):
        t0 = time.perf_counter()
        path = write_netcdf(schema, out_dir, coords, seed=args.seed, chunk_preset=chunk_preset, scale=args.scale)
        logger.info("NetCDF complete in %.1fs -> %s", time.perf_counter() - t0, path)


if __name__ == "__main__":
    main()
