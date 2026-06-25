"""Block loader — ingests an xarray-exported Zarr store as a cheesemonger block.

A "block" is one value of the dataset's last dimension (e.g. one screen). The
source is a Zarr store written by xarray.Dataset.to_zarr(); it may live on the
local filesystem or in cloud storage (``gs://`` URLs, via gcsfs). The store is
copied into ``{data_dir}/{dataset}/blocks/{block}/`` where the API reads it.

The store is loaded faithfully (as delivered). Note the query engine currently
requires each queried datatype to span every selected dimension — i.e. the
"broadcasted" form. A faithfully-loaded *unbroadcasted* store still loads, but
queries that fix a dimension a reduced-rank datatype doesn't have will be
rejected until the engine supports unbroadcasted stores (see
services/query.py TODO(unbroadcast)).

TODO(unbroadcast): optionally broadcast-on-load, or (better) teach the query
engine to ignore selections for dims a datatype lacks, so we can ingest the
storage-efficient unbroadcasted delivery directly.
TODO(per-block-coords): schema dimension labels are dataset-level, but blocks
(screens) legitimately differ in their Target/Response label sets. Revisit how
per-block coordinate labels feed the query response index for multi-screen
datasets.
"""

from __future__ import annotations

import logging
import shutil

import xarray as xr

from cheesemonger.schemas.common import DatatypeSpec, Dimension
from cheesemonger.schemas.dataset import DatasetIn
from cheesemonger.services.dataset import DatasetService

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
    # Dimension.labels must be a homogeneous list[int] | list[str].
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
    except Exception as e:  # pydantic ValidationError, InvalidName, etc.
        raise LoaderError(f"Inferred schema is invalid: {e}") from e


def _validate_against_schema(
    src: xr.Dataset, schema: dict, dataset: str, last_dimension: str
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


def load_block(
    source: str,
    dataset: str,
    block: str,
    data_dir: str,
    *,
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
        last_dimension: Name of the block key (only used when creating the
            dataset). Must not be one of the source store's dimensions.
        create_dataset: If the dataset doesn't exist, infer its schema from the
            source store and create it. Otherwise loading into a missing dataset
            is an error.
        overwrite: Replace the block if it already exists.

    Returns:
        A summary dict (dataset, block, path, dimensions, datatypes).
    """
    svc = DatasetService(data_dir)

    logger.info("Opening source store: %s", source)
    try:
        src = xr.open_zarr(source)
    except Exception as e:
        raise LoaderError(f"Could not open source Zarr store {source!r}: {e}") from e

    try:
        existing_schema = svc.get_schema(dataset)
        if existing_schema is not None:
            _validate_against_schema(src, existing_schema, dataset, last_dimension)
        elif create_dataset:
            dataset_in = _infer_schema(src, dataset, last_dimension)
            svc.create(dataset_in)
            logger.info(
                "Created dataset %r (last_dimension=%r, %d dims, %d datatypes)",
                dataset, last_dimension, len(dataset_in.dimensions), len(dataset_in.datatypes),
            )
        else:
            raise LoaderError(
                f"Dataset {dataset!r} does not exist. Pass create_dataset=True to "
                f"infer its schema from the source store."
            )

        dest = svc.get_block_zarr_path(dataset, block)
        if dest.exists():
            if not overwrite:
                raise LoaderError(
                    f"Block {block!r} already exists in dataset {dataset!r}. "
                    f"Pass overwrite=True to replace it."
                )
            logger.info("Overwriting existing block %r", block)
            shutil.rmtree(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Writing block %r -> %s", block, dest)
        # TODO(rechunk): honor the dataset's chunk_shape instead of copying the
        # source chunking verbatim.
        src.to_zarr(str(dest), mode="w")

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
