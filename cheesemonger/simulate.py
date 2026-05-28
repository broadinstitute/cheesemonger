"""
Generate simulated PESCA-like datasets in Zarr and/or NetCDF format.

Usage:
    python -m cheesemonger simulate --help

Each screen is written as an independent store (Zarr directory or NetCDF file)
under a dataset root directory.  A _registry.json file tracks active screens.
This layout makes screen-level add/delete/replace operations cheap.

Data is written one variable at a time to keep peak memory low (~1.5 GB per
variable instead of ~20+ GB for all 15 simultaneously).
"""

from __future__ import annotations

import argparse
import json
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
            divisor = 1024 * 1024 if os.uname().sysname == "Darwin" else 1024
            return rusage.ru_maxrss / divisor
    except Exception:
        pass
    return 0.0


def _fill_random_f32(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    """Generate a float32 random normal array without a full-size float64 temp."""
    arr = np.empty(shape, dtype=np.float32)
    for i in range(shape[0]):
        arr[i] = rng.standard_normal(shape[1:]).astype(np.float32)
    return arr


# ---------------------------------------------------------------------------
# Registry — tracks which screens exist in a dataset
# ---------------------------------------------------------------------------

def _registry_path(dataset_root: Path) -> Path:
    return dataset_root / "_registry.json"


def read_registry(dataset_root: Path) -> list[str]:
    path = _registry_path(dataset_root)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def write_registry(dataset_root: Path, screens: list[str]) -> None:
    path = _registry_path(dataset_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(screens, indent=2) + "\n")


def add_screen_to_registry(dataset_root: Path, screen_label: str) -> None:
    screens = read_registry(dataset_root)
    if screen_label not in screens:
        screens.append(screen_label)
    write_registry(dataset_root, screens)


def remove_screen_from_registry(dataset_root: Path, screen_label: str) -> None:
    screens = read_registry(dataset_root)
    screens = [s for s in screens if s != screen_label]
    write_registry(dataset_root, screens)


# ---------------------------------------------------------------------------
# Screen deletion
# ---------------------------------------------------------------------------

def delete_screen(
    dataset_root: Path,
    screen_label: str,
    fmt: str = "zarr",
) -> None:
    """
    Delete a screen's data and remove it from the registry.

    This is O(1) — it removes one directory (Zarr) or one file (NetCDF)
    without touching any other screen's data.
    """
    screen_dir = dataset_root / screen_label

    if fmt == "zarr":
        store_path = screen_dir / "data.zarr"
        if store_path.exists():
            shutil.rmtree(store_path)
    elif fmt == "netcdf":
        nc_path = screen_dir / "data.nc"
        if nc_path.exists():
            nc_path.unlink()

    if screen_dir.exists() and not any(screen_dir.iterdir()):
        screen_dir.rmdir()

    remove_screen_from_registry(dataset_root, screen_label)
    logger.info("Deleted screen %s from %s", screen_label, dataset_root)


# ---------------------------------------------------------------------------
# Dimension coordinate generation
# ---------------------------------------------------------------------------

def make_coords(schema: DatasetSchema) -> dict[str, np.ndarray]:
    """Generate synthetic coordinate labels for each dimension (excluding screen)."""
    coords: dict[str, np.ndarray] = {}
    for dim, size in schema.dim_sizes.items():
        if dim == "timepoint":
            coords[dim] = np.array([4, 7][:size])
        elif dim == "testedperturbation":
            coords[dim] = np.array([f"Gene_{i:05d}" for i in range(size)])
        elif dim == "testedgeneexpression":
            coords[dim] = np.array([f"RGene_{i:05d}" for i in range(size)])
        else:
            coords[dim] = np.array([f"{dim}_{i}" for i in range(size)])
    return coords


def make_screen_labels(schema: DatasetSchema) -> list[str]:
    return [f"{schema.screen_prefix}_{i:03d}" for i in range(schema.n_screens)]


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

    Strategy: chunk size 1 along timepoint (low cardinality, often constrained
    in queries), and larger chunks along testedperturbation/testedgeneexpression
    to give good read performance for series queries.

    Presets:
        big   — ~20 MB chunks: (1, 1000, 5000). Series query = 4 chunks.
        small — ~1 MB chunks:  (1, 250, 1000).  Series query = 18 chunks.
    """
    preset = CHUNK_PRESETS[chunk_preset]
    chunks = []
    for dim in dt.dimensions:
        size = schema.dim_sizes[dim]
        if dim == "timepoint":
            chunks.append(1)
        else:
            target = preset.get(dim, preset["default"])
            chunks.append(min(target, size))
    return tuple(chunks)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_screen_zarr(
    schema: DatasetSchema,
    coords: dict[str, np.ndarray],
    dataset_root: Path,
    screen_label: str,
    rng: np.random.Generator,
    chunk_preset: str = "big",
) -> Path:
    """
    Write a single screen's data to its own Zarr store, one variable at a time.

    Uses the zarr library directly so that only one variable's data is in
    memory at any time.  ``_ARRAY_DIMENSIONS`` attributes ensure the store
    is readable by ``xr.open_zarr()``.
    """
    import numcodecs
    import zarr

    store_path = dataset_root / screen_label / "data.zarr"
    if store_path.exists():
        shutil.rmtree(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)

    root = zarr.open_group(str(store_path), mode="w")

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

    for dt_i, dt in enumerate(schema.datatypes):
        full_shape = tuple(schema.dim_sizes[dim] for dim in dt.dimensions)
        arr = _fill_random_f32(rng, full_shape)
        root[dt.name][:] = arr
        del arr
        logger.debug(
            "  var %d/%d (%s)  RSS=%.0f MB",
            dt_i + 1, len(schema.datatypes), dt.name, _rss_mb(),
        )

    zarr.consolidate_metadata(str(store_path))
    add_screen_to_registry(dataset_root, screen_label)
    return store_path


def write_screen_netcdf(
    schema: DatasetSchema,
    coords: dict[str, np.ndarray],
    dataset_root: Path,
    screen_label: str,
    rng: np.random.Generator,
    chunk_preset: str = "big",
) -> Path:
    """
    Write a single screen's data to its own NetCDF file, one variable at a time.

    Uses the netCDF4 library directly so that only one variable's data is in
    memory at any time.
    """
    import netCDF4 as nc4

    nc_path = dataset_root / screen_label / "data.nc"
    if nc_path.exists():
        nc_path.unlink()
    nc_path.parent.mkdir(parents=True, exist_ok=True)

    ncfile = nc4.Dataset(str(nc_path), "w", format="NETCDF4")
    ncfile.set_fill_off()

    for dim_name, labels in coords.items():
        ncfile.createDimension(dim_name, len(labels))
        if labels.dtype.kind == "U":
            max_len = max(len(s) for s in labels)
            nc_dt = np.dtype(f"S{max_len}")
            coord_var = ncfile.createVariable(dim_name, nc_dt, (dim_name,))
            coord_var[:] = np.array(labels, dtype=nc_dt)
        else:
            coord_var = ncfile.createVariable(dim_name, labels.dtype, (dim_name,))
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

    for dt_i, dt in enumerate(schema.datatypes):
        full_shape = tuple(schema.dim_sizes[dim] for dim in dt.dimensions)
        arr = _fill_random_f32(rng, full_shape)
        ncfile.variables[dt.name][:] = arr
        del arr
        ncfile.sync()
        logger.debug(
            "  var %d/%d (%s)  RSS=%.0f MB",
            dt_i + 1, len(schema.datatypes), dt.name, _rss_mb(),
        )

    ncfile.close()
    add_screen_to_registry(dataset_root, screen_label)
    return nc_path


def write_all_screens(
    schema: DatasetSchema,
    output_dir: Path,
    fmt: str,
    seed: int = 42,
    chunk_preset: str = "big",
) -> Path:
    """Generate and write all screens for a dataset."""
    dataset_root = output_dir / f"chunks_{chunk_preset}" / schema.name / fmt
    if dataset_root.exists():
        shutil.rmtree(dataset_root)
    dataset_root.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    coords = make_coords(schema)
    screen_labels = make_screen_labels(schema)

    writer = write_screen_zarr if fmt == "zarr" else write_screen_netcdf

    for i, screen_label in enumerate(screen_labels):
        t0 = time.perf_counter()
        writer(schema, coords, dataset_root, screen_label, rng, chunk_preset)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d (%s) written as %s (%.1fs)  RSS=%.0f MB",
            i + 1, len(screen_labels), screen_label, fmt, elapsed, _rss_mb(),
        )

    logger.info("%s dataset written to %s (%d screens)", fmt, dataset_root, len(screen_labels))
    return dataset_root


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SCALE_PRESETS = {
    "small": {"n_screens": 2, "n_timepoints": 2, "n_testedperturbations": 100, "n_testedgeneexpressions": 200},
    "medium": {"n_screens": 5, "n_timepoints": 2, "n_testedperturbations": 5_000, "n_testedgeneexpressions": 10_000},
    "single_screen": {"n_screens": 1, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
    "full": {"n_screens": 30, "n_timepoints": 2, "n_testedperturbations": 10_000, "n_testedgeneexpressions": 18_000},
}


def _estimate_size(schema: DatasetSchema) -> float:
    """Estimate total uncompressed size in GB across all screens."""
    per_screen = 0
    for dt in schema.datatypes:
        n = 1
        for dim in dt.dimensions:
            n *= schema.dim_sizes[dim]
        per_screen += n
    return per_screen * schema.n_screens * 4 / (1024**3)


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
             "big=~20MB chunks (1,1000,5000), small=~1MB chunks (1,250,1000)",
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
        "Scale=%s  chunks=%s (~%.1f MB)  dims=%s  screens=%d  estimated uncompressed=%.2f GB",
        args.scale,
        sample_chunks,
        chunk_mb,
        dict(schema.dim_sizes),
        schema.n_screens,
        est_gb,
    )

    if args.format in ("zarr", "both"):
        t0 = time.perf_counter()
        path = write_all_screens(schema, args.output_dir, "zarr", seed=args.seed, chunk_preset=chunk_preset)
        logger.info("Zarr complete in %.1fs -> %s", time.perf_counter() - t0, path)

    if args.format in ("netcdf", "both"):
        t0 = time.perf_counter()
        path = write_all_screens(schema, args.output_dir, "netcdf", seed=args.seed, chunk_preset=chunk_preset)
        logger.info("NetCDF complete in %.1fs -> %s", time.perf_counter() - t0, path)


if __name__ == "__main__":
    main()
