"""
Generate simulated PESCA-like datasets in Zarr and/or NetCDF format.

Usage:
    python -m cheesemonger simulate --help

The script generates one screen at a time and appends it to the store,
"""

from __future__ import annotations

import argparse
import logging
import shutil
import time
from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.schema import DatasetSchema, DatatypeSpec, pesca_schema

logger = logging.getLogger(__name__)


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
# Build one screen as an xarray Dataset
# ---------------------------------------------------------------------------

def build_screen_dataset(
    schema: DatasetSchema,
    coords: dict[str, np.ndarray],
    screen_idx: int,
    rng: np.random.Generator,
) -> xr.Dataset:
    """
    Build an xr.Dataset containing one screen's worth of data for all
    datatype shards.
    """
    screen_label = coords[schema.append_dim][screen_idx]

    screen_coords = {
        dim: labels if dim != schema.append_dim else [screen_label]
        for dim, labels in coords.items()
    }

    data_vars: dict[str, tuple] = {}
    for dt in schema.datatypes:
        shape = tuple(
            1 if dim == schema.append_dim else schema.dim_sizes[dim]
            for dim in dt.dimensions
        )
        arr = rng.standard_normal(shape).astype(np.float32)
        data_vars[dt.name] = (list(dt.dimensions), arr)

    return xr.Dataset(data_vars, coords=screen_coords)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_zarr(
    schema: DatasetSchema,
    output_dir: Path,
    coords: dict[str, np.ndarray],
    seed: int = 42,
    chunk_preset: str = "big",
    scale: str = "small",
) -> Path:
    """
    Write a simulated dataset to a Zarr store, one screen at a time.

    Uses xarray's native to_zarr() with append_dim for incremental writes.
    """
    store_path = output_dir / f"{schema.name}_{scale}.zarr"
    if store_path.exists():
        shutil.rmtree(store_path)

    rng = np.random.default_rng(seed)
    n_screens = schema.dim_sizes[schema.append_dim]

    encoding = {
        dt.name: {"chunks": chunk_sizes(dt, schema, chunk_preset)}
        for dt in schema.datatypes
    }

    for screen_idx in range(n_screens):
        t0 = time.perf_counter()
        ds = build_screen_dataset(schema, coords, screen_idx, rng)

        if screen_idx == 0:
            ds.to_zarr(str(store_path), mode="w", encoding=encoding)
        else:
            ds.to_zarr(str(store_path), append_dim=schema.append_dim)

        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d written to Zarr (%.1fs)",
            screen_idx + 1, n_screens, elapsed,
        )

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
    Write a simulated dataset to a NetCDF4 file.

    Builds each screen as an xr.Dataset, concatenates them, and writes once
    with unlimited_dims so the screen axis can be extended later.

    NOTE: this loads all screens into memory before writing. This will be an issue 
    for larger datasets and should be optimized in a way like incremental netCDF4-based 
    approach or backing the arrays with dask for lazy evaluation.
    """
    out_path = output_dir / f"{schema.name}_{scale}.nc"
    if out_path.exists():
        out_path.unlink()

    rng = np.random.default_rng(seed)
    n_screens = schema.dim_sizes[schema.append_dim]

    encoding = {
        dt.name: {
            "chunksizes": chunk_sizes(dt, schema, chunk_preset),
            "zlib": True,
            "complevel": 1,
        }
        for dt in schema.datatypes
    }

    screen_datasets = []
    for screen_idx in range(n_screens):
        t0 = time.perf_counter()
        ds = build_screen_dataset(schema, coords, screen_idx, rng)
        screen_datasets.append(ds)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d generated for NetCDF (%.1fs)",
            screen_idx + 1, n_screens, elapsed,
        )

    t0 = time.perf_counter()
    full_ds = xr.concat(screen_datasets, dim=schema.append_dim)
    full_ds.to_netcdf(
        str(out_path),
        unlimited_dims=[schema.append_dim],
        encoding=encoding,
    )
    logger.info("NetCDF written in %.1fs", time.perf_counter() - t0)

    logger.info("NetCDF file written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SCALE_PRESETS = {
    "tiny": {"n_screens": 2, "n_timepoints": 2, "n_testedperturbations": 100, "n_testedgeneexpressions": 200},
    "small": {"n_screens": 3, "n_timepoints": 2, "n_testedperturbations": 1_000, "n_testedgeneexpressions": 2_000},
    "medium": {"n_screens": 5, "n_timepoints": 2, "n_testedperturbations": 5_000, "n_testedgeneexpressions": 10_000},
    "single_screen": {"n_screens": 1, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
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
