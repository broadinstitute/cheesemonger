"""
Generate simulated PESCA-like datasets in Zarr and/or NetCDF format.

Usage:
    python -m cheesemonger simulate --help

Each screen is written as an independent store (Zarr directory or NetCDF file)
under a dataset root directory.  A _registry.json file tracks active screens.
This layout makes screen-level add/delete/replace operations cheap.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.schema import DatasetSchema, DatatypeSpec, pesca_schema

logger = logging.getLogger(__name__)


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
            coords[dim] = np.array([f"Gene_{i:05d}" for i in range(size)])
        else:
            coords[dim] = np.array([f"{dim}_{i}" for i in range(size)])
    return coords


def make_screen_labels(schema: DatasetSchema) -> list[str]:
    return [f"{schema.screen_prefix}_{i:03d}" for i in range(schema.n_screens)]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_sizes(dt: DatatypeSpec, schema: DatasetSchema) -> tuple[int, ...]:
    """
    Choose chunk sizes for a datatype shard.

    Strategy: chunk size 1 along timepoint (low cardinality, often constrained
    in queries), and larger chunks along testedperturbation/testedgeneexpression
    to give good read performance for series queries.
    """
    chunks = []
    for dim in dt.dimensions:
        size = schema.dim_sizes[dim]
        if dim == "timepoint":
            chunks.append(1)
        elif dim == "testedperturbation":
            chunks.append(min(1000, size))
        elif dim == "testedgeneexpression":
            chunks.append(min(5000, size))
        else:
            chunks.append(min(1000, size))
    return tuple(chunks)


# ---------------------------------------------------------------------------
# Build one screen as an xarray Dataset
# ---------------------------------------------------------------------------

def build_screen_dataset(
    schema: DatasetSchema,
    coords: dict[str, np.ndarray],
    rng: np.random.Generator,
) -> xr.Dataset:
    """
    Build an xr.Dataset containing one screen's worth of data for all
    datatype shards.  The dataset has no screen dimension — screen is the
    organizational key, not a data axis.
    """
    data_vars: dict[str, tuple] = {}
    for dt in schema.datatypes:
        shape = tuple(schema.dim_sizes[dim] for dim in dt.dimensions)
        arr = rng.standard_normal(shape).astype(np.float32)
        data_vars[dt.name] = (list(dt.dimensions), arr)

    return xr.Dataset(data_vars, coords=coords)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_screen_zarr(
    ds: xr.Dataset,
    schema: DatasetSchema,
    dataset_root: Path,
    screen_label: str,
) -> Path:
    """Write a single screen's Dataset to its own Zarr store."""
    store_path = dataset_root / screen_label / "data.zarr"
    if store_path.exists():
        shutil.rmtree(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)

    encoding = {
        dt.name: {"chunks": chunk_sizes(dt, schema)}
        for dt in schema.datatypes
    }
    ds.to_zarr(str(store_path), mode="w", encoding=encoding)
    add_screen_to_registry(dataset_root, screen_label)
    return store_path


def write_screen_netcdf(
    ds: xr.Dataset,
    schema: DatasetSchema,
    dataset_root: Path,
    screen_label: str,
) -> Path:
    """Write a single screen's Dataset to its own NetCDF file."""
    nc_path = dataset_root / screen_label / "data.nc"
    if nc_path.exists():
        nc_path.unlink()
    nc_path.parent.mkdir(parents=True, exist_ok=True)

    encoding = {
        dt.name: {
            "chunksizes": chunk_sizes(dt, schema),
            "zlib": True,
            "complevel": 1,
        }
        for dt in schema.datatypes
    }
    ds.to_netcdf(str(nc_path), encoding=encoding)
    add_screen_to_registry(dataset_root, screen_label)
    return nc_path


def write_all_screens(
    schema: DatasetSchema,
    output_dir: Path,
    fmt: str,
    seed: int = 42,
) -> Path:
    """Generate and write all screens for a dataset."""
    dataset_root = output_dir / schema.name / fmt
    if dataset_root.exists():
        shutil.rmtree(dataset_root)
    dataset_root.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    coords = make_coords(schema)
    screen_labels = make_screen_labels(schema)

    writer = write_screen_zarr if fmt == "zarr" else write_screen_netcdf

    for i, screen_label in enumerate(screen_labels):
        t0 = time.perf_counter()
        ds = build_screen_dataset(schema, coords, rng)
        writer(ds, schema, dataset_root, screen_label)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Screen %d/%d (%s) written as %s (%.1fs)",
            i + 1, len(screen_labels), screen_label, fmt, elapsed,
        )

    logger.info("%s dataset written to %s (%d screens)", fmt, dataset_root, len(screen_labels))
    return dataset_root


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
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    preset = SCALE_PRESETS[args.scale]
    schema = pesca_schema(**preset)

    est_gb = _estimate_size(schema)
    logger.info(
        "Scale=%s  dims=%s  screens=%d  estimated uncompressed=%.2f GB",
        args.scale,
        dict(schema.dim_sizes),
        schema.n_screens,
        est_gb,
    )

    if args.format in ("zarr", "both"):
        t0 = time.perf_counter()
        path = write_all_screens(schema, args.output_dir, "zarr", seed=args.seed)
        logger.info("Zarr complete in %.1fs -> %s", time.perf_counter() - t0, path)

    if args.format in ("netcdf", "both"):
        t0 = time.perf_counter()
        path = write_all_screens(schema, args.output_dir, "netcdf", seed=args.seed)
        logger.info("NetCDF complete in %.1fs -> %s", time.perf_counter() - t0, path)


if __name__ == "__main__":
    main()
