"""Block loader — ingests an xarray-exported Zarr store as a cheesemonger block.

A "block" is one value of the dataset's last dimension (e.g. one screen). The
source is a Zarr store written by xarray.Dataset.to_zarr(); it may live on the
local filesystem or in cloud storage (``gs://`` URLs, via gcsfs). The store is
copied into ``{data_dir}/{dataset}/blocks/{block}/`` where the API reads it.

The store is loaded faithfully (as delivered), including the storage-efficient
*unbroadcasted* form where reduced-rank datatypes span only the dims they vary
along (e.g. CtrlMean over ["Timepoint", "Response"], not the full grid). The
query engine handles this: a selection on a dim a datatype lacks is ignored for
that datatype rather than erroring (see services/query.py). No broadcast-on-load
is needed.

TODO(per-block-coords): schema dimension labels are dataset-level, but blocks
(screens) legitimately differ in their Target/Response label sets. Revisit how
per-block coordinate labels feed the query response index for multi-screen
datasets.
"""

from __future__ import annotations

import logging
import shutil

import xarray as xr
from sqlalchemy.orm import Session

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.schemas.common import DatatypeSpec, Dimension, SchemaDict
from cheesemonger.schemas.dataset import DatasetIn
from cheesemonger.services import dataset as ds_paths

logger = logging.getLogger(__name__)


class LoaderError(Exception):
    """Raised for data-loading problems (unreadable source, schema mismatch)."""


def _coord_labels(src: xr.Dataset, dim: str) -> list:
    """Return a dimension's coordinate labels as a JSON-friendly list.

    Falls back to integer positions if the dimension has no coordinate array.
    """
    if dim in src.coords:
        values = src.coords[dim].values.tolist()
    else:
        values = list(range(int(src.sizes[dim])))
    return [v if isinstance(v, int) and not isinstance(v, bool) else str(v) for v in values]


def _infer_schema(src: xr.Dataset, name: str, last_dimension: str) -> DatasetIn:
    """Build a DatasetIn schema from a source store's dims and data variables."""
    if last_dimension in src.sizes:
        raise LoaderError(
            f"last_dimension {last_dimension!r} must not be one of the source "
            f"store's dimensions {tuple(src.sizes)}; it is the block key, stored "
            f"as the folder name, not an array axis."
        )
    dimensions = [Dimension(name=str(d), labels=_coord_labels(src, str(d))) for d in src.sizes]
    datatypes = [
        DatatypeSpec(
            name=str(v),
            dimensions=[str(d) for d in src[v].dims],
            dtype=str(src[v].dtype),
        )
        for v in src.data_vars
    ]
    try:
        return DatasetIn(
            name=name,
            last_dimension=last_dimension,
            dimensions=dimensions,
            datatypes=datatypes,
            chunk_shape=[],
        )
    except Exception as e:
        raise LoaderError(f"Inferred schema is invalid: {e}") from e


def _validate_against_schema(
    src: xr.Dataset, schema: SchemaDict, dataset: str, last_dimension: str
) -> None:
    """Ensure the source's dims and datatypes are declared in the dataset schema.

    Coordinate *labels* are intentionally not checked: separate blocks (screens)
    legitimately carry different Target/Response label sets.
    """
    schema_dims = {d["name"] for d in schema["dimensions"]}
    schema_dts = {d["name"] for d in schema["datatypes"]}
    for d in src.sizes:
        if str(d) not in schema_dims:
            raise LoaderError(
                f"Source dimension {d!r} is not declared in dataset {dataset!r} "
                f"(declared: {sorted(schema_dims)})."
            )
    for v in src.data_vars:
        if str(v) not in schema_dts:
            raise LoaderError(
                f"Source datatype {v!r} is not declared in dataset {dataset!r} "
                f"(declared: {sorted(schema_dts)})."
            )


def _write_dataset(ds: xr.Dataset, dest: str) -> None:
    """Rechunk to sane chunk sizes and write to a Zarr store, with progress.

    TODO(rechunk): honor the dataset's declared chunk_shape instead of "auto".
    """
    for var in ds.variables.values():
        for key in ("chunks", "preferred_chunks"):
            var.encoding.pop(key, None)

    try:
        from dask.diagnostics import ProgressBar  # type: ignore[attr-defined]
    except ImportError:
        logger.info("Writing (no dask; progress bar unavailable)...")
        ds.to_zarr(dest, mode="w")
        return

    # dt=1.0 so the bar updates every second — enough to see it's alive on a
    # slow remote read without spamming the terminal. The bar covers the whole
    # read+write compute (reading source chunks, writing them to dest).
    with ProgressBar(dt=1.0):
        ds.chunk("auto").to_zarr(dest, mode="w")


def load_block(
    source: str,
    dataset: str,
    block: str,
    data_dir: str,
    *,
    db: Session,
    last_dimension: str = "screen",
    create_dataset: bool = False,
    overwrite: bool = False,
) -> dict:
    """Load a Zarr store as a block of ``dataset``.

    Args:
        source: Local path or ``gs://`` URL of an xarray-exported Zarr store.
        dataset: Target dataset name.
        block: Block name (one value of the last dimension, e.g. a screen ID).
        data_dir: Root data directory the API serves from.
        db: SQLAlchemy session for metadata operations. The caller owns it and
            is responsible for the commit (e.g. via ``db.session_scope``).
        last_dimension: Name of the block key (only used when creating).
        create_dataset: If the dataset doesn't exist, infer and create it.
        overwrite: Replace the block if it already exists.

    Returns:
        A summary dict (dataset, block, path, dimensions, datatypes).
    """
    logger.info("Opening source store: %s", source)
    try:
        src = xr.open_zarr(source)
    except Exception as e:
        raise LoaderError(f"Could not open source Zarr store {source!r}: {e}") from e

    try:
        existing_schema = ds_crud.get_schema_dict(db, dataset)
        if existing_schema is not None:
            _validate_against_schema(src, existing_schema, dataset, last_dimension)
        elif create_dataset:
            dataset_in = _infer_schema(src, dataset, last_dimension)
            ds_crud.create_dataset(db, dataset_in)
            logger.info(
                "Created dataset %r (last_dimension=%r, %d dims, %d datatypes)",
                dataset, last_dimension, len(dataset_in.dimensions), len(dataset_in.datatypes),
            )
            ds_paths.blocks_dir(data_dir, dataset).mkdir(parents=True, exist_ok=True)
        else:
            raise LoaderError(
                f"Dataset {dataset!r} does not exist. Pass create_dataset=True to "
                f"infer its schema from the source store."
            )

        dest = ds_paths.block_dir(data_dir, dataset, block)
        if dest.exists():
            if not overwrite:
                raise LoaderError(
                    f"Block {block!r} already exists in dataset {dataset!r}. "
                    f"Pass overwrite=True to replace it."
                )
            logger.info("Overwriting existing block %r", block)
            shutil.rmtree(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            size_mb = src.nbytes / 1e6
        except Exception:
            size_mb = float("nan")
        logger.info(
            "Source: %d data variables, ~%.1f MB uncompressed, dims=%s",
            len(src.data_vars), size_mb, dict(src.sizes),
        )
        logger.info(
            "Writing block %r -> %s  (remote gs:// sources can take a while)",
            block, dest,
        )
        _write_dataset(src, str(dest))

        # Register the block in the DB (the caller's session_scope commits).
        if not ds_crud.block_exists(db, dataset, block):
            ds_crud.create_block(db, dataset, block)

        summary = {
            "dataset": dataset,
            "block": block,
            "path": str(dest),
            "dimensions": {str(d): int(src.sizes[d]) for d in src.sizes},
            "datatypes": [str(v) for v in src.data_vars],
        }
    finally:
        src.close()

    logger.info("Loaded block %r into dataset %r", block, dataset)
    return summary


def delete_block(
    dataset: str,
    block: str,
    data_dir: str,
    *,
    db: Session,
) -> dict:
    """Delete a block: remove its DB row and its Zarr directory on disk.

    Raises LoaderError if the dataset or block does not exist. The caller owns
    the session and commits it (e.g. via ``db.session_scope``).
    """
    if not ds_crud.dataset_exists(db, dataset):
        raise LoaderError(f"Dataset {dataset!r} does not exist")
    if not ds_crud.block_exists(db, dataset, block):
        raise LoaderError(f"Block {block!r} does not exist in dataset {dataset!r}")

    ds_crud.delete_block(db, dataset, block)
    block_path = ds_paths.block_dir(data_dir, dataset, block)
    if block_path.exists():
        shutil.rmtree(block_path)

    logger.info("Deleted block %r from dataset %r", block, dataset)
    return {"dataset": dataset, "block": block, "deleted": True}


def delete_dataset(
    dataset: str,
    data_dir: str,
    *,
    db: Session,
    force: bool = False,
) -> dict:
    """Delete a dataset and its on-disk directory.

    Refuses if the dataset still has blocks unless ``force=True``, in which case
    its blocks are deleted first (their FK is RESTRICT, so block rows must be
    removed before the dataset row). Raises LoaderError if it doesn't exist. The
    caller owns the session and commits it (e.g. via ``db.session_scope``).
    """
    if not ds_crud.dataset_exists(db, dataset):
        raise LoaderError(f"Dataset {dataset!r} does not exist")

    block_names = ds_crud.list_block_names(db, dataset)
    if block_names and not force:
        raise LoaderError(
            f"Dataset {dataset!r} still has {len(block_names)} block(s): "
            f"{', '.join(block_names)}. Delete them first or pass force=True."
        )

    for b in block_names:
        ds_crud.delete_block(db, dataset, b)
    ds_crud.delete_dataset(db, dataset)

    # rmtree the dataset dir removes the blocks/ subtree in one shot.
    ds_dir = ds_paths.dataset_dir(data_dir, dataset)
    if ds_dir.exists():
        shutil.rmtree(ds_dir)

    logger.info("Deleted dataset %r (%d block(s))", dataset, len(block_names))
    return {"dataset": dataset, "deleted": True, "blocks_deleted": len(block_names)}
