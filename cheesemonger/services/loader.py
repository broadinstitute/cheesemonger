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

Per-block coordinate labels: a dataset declares a *gene universe* (the validation
superset for gene dimensions); every block's labels must be a subset of it. The
dataset's ``dimensions[*].labels`` is the *present union* — the labels actually
loaded across blocks — maintained here at load (fold-in on add) and recomputed
on delete. The query engine builds its index from each block's own Zarr
coordinates, so the index always matches the data.
"""

from __future__ import annotations

import logging
import shutil

import xarray as xr
from sqlalchemy.orm import Session

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.models.dataset import SchemaDict
from cheesemonger.schemas.common import ChunkDim, DatatypeSpec, Dimension, normalize_name
from cheesemonger.schemas.dataset import DatasetIn
from cheesemonger.services import dataset as ds_paths
from cheesemonger.services.gene_universe import normalize_label

logger = logging.getLogger(__name__)


class LoaderError(Exception):
    """Raised for data-loading problems (unreadable source, schema mismatch)."""


def _coord_labels(src: xr.Dataset, dim: str) -> list[str]:
    """Return a dimension's coordinate labels as a list of strings.

    Labels are stored as strings uniformly (entrez IDs, screen names, and
    numeric keys like timepoints/ranks alike) — see ``_stringify_coords``, which
    applies the same conversion to the on-disk Zarr coordinates so schema labels
    and store coordinates stay in lock-step. Falls back to integer positions
    (stringified) if the dimension has no coordinate array.
    """
    if dim in src.coords:
        values = src.coords[dim].values.tolist()
    else:
        values = list(range(int(src.sizes[dim])))
    return [str(v) for v in values]


def _infer_schema(
    src: xr.Dataset,
    name: str,
    last_dimension: str,
    chunk_shape: dict[str, int] | None = None,
    gene_universe: dict[str, list[str]] | None = None,
) -> DatasetIn:
    """Build a DatasetIn schema from a source store's dims and data variables.

    ``chunk_shape`` (dim -> chunk size) is recorded so every block of the dataset
    is chunked the same way. Entries for dims not in the source are dropped.
    ``gene_universe`` (dim -> allowed labels) is the validation superset stored on the
    dataset; a block's labels along a listed dim must be a subset of it.
    """
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
    src_dims = {str(d) for d in src.sizes}
    chunks = [
        ChunkDim(name=n, size=s) for n, s in (chunk_shape or {}).items() if n in src_dims
    ]
    try:
        return DatasetIn(
            name=name,
            last_dimension=last_dimension,
            dimensions=dimensions,
            datatypes=datatypes,
            chunk_shape=chunks,
            gene_universe=gene_universe or {},
        )
    except Exception as e:
        raise LoaderError(f"Inferred schema is invalid: {e}") from e


def _validate_against_schema(
    src: xr.Dataset, schema: SchemaDict, dataset: str, last_dimension: str
) -> None:
    """Ensure the source's dims and datatypes are declared in the dataset schema.

    This checks dimension/datatype *names* only. Coordinate *labels* are validated
    separately against the gene universe (``_validate_against_gene_universe``);
    they are not required to match other blocks, since screens legitimately carry
    different Target/Response label sets.
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


def _validate_against_gene_universe(
    src: xr.Dataset, gene_universe: dict[str, list[str]], dataset: str
) -> None:
    """Ensure each block coordinate is a subset of the dataset's gene_universe.

    Only dims present in ``gene_universe`` are checked (gene dims); other dims (e.g.
    Timepoint) are unconstrained. A block that lacks a listed dim entirely
    (reduced-rank) is fine — there is nothing to validate. Labels are normalized
    the same way as the gene universe so the comparison is apples-to-apples.
    """
    for dim, allowed in gene_universe.items():
        if dim not in src.sizes:
            continue  # block doesn't carry this dim — nothing to check
        allowed_set = {normalize_label(a) or str(a) for a in allowed}
        missing = [lbl for lbl in _coord_labels(src, dim) if lbl not in allowed_set]
        if missing:
            preview = ", ".join(missing[:10])
            more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
            raise LoaderError(
                f"Block has {len(missing)} label(s) in dimension {dim!r} that are "
                f"not in dataset {dataset!r}'s gene_universe: {preview}{more}"
            )


def _block_labels_by_dim(src: xr.Dataset) -> dict[str, list[str]]:
    """The block's coordinate labels for every dimension it carries."""
    return {str(d): _coord_labels(src, str(d)) for d in src.sizes}


def _recompute_present_union(db: Session, dataset: str, data_dir: str) -> None:
    """Recompute the present union for every dim from the blocks on disk.

    Used after a removal, where the union may *shrink* and so cannot be
    maintained incrementally. Reads each remaining block's coordinate vectors (a
    small per-dim array, not the data variables) and unions them, preserving
    first-seen order across blocks (sorted block order, for determinism). Flushes
    but does not commit — the caller owns the transaction.
    """
    schema = ds_crud.get_schema_dict(db, dataset)
    if schema is None:
        return
    dim_names = [d["name"] for d in schema["dimensions"]]
    seen: dict[str, set[str]] = {d: set() for d in dim_names}
    union: dict[str, list[str]] = {d: [] for d in dim_names}
    for block in ds_crud.list_block_names(db, dataset):
        path = ds_paths.block_dir(data_dir, dataset, block)
        if not path.exists():
            continue
        bsrc = xr.open_zarr(str(path))
        try:
            for dim in dim_names:
                if dim not in bsrc.sizes:
                    continue
                for lbl in _coord_labels(bsrc, dim):
                    if lbl not in seen[dim]:
                        seen[dim].add(lbl)
                        union[dim].append(lbl)
        finally:
            bsrc.close()
    ds_crud.set_present_labels(db, dataset, union)


def reconcile_dataset(dataset: str, data_dir: str, *, db: Session) -> dict:
    """Rebuild the present union from the blocks on disk (the source of truth).

    The present union is a cache derived from the blocks, so it can always be
    regenerated. Use after an out-of-band change, an interrupted load, or if the
    union ever drifts. The caller owns the session; this commits its work.
    """
    if not ds_crud.dataset_exists(db, dataset):
        raise LoaderError(f"Dataset {dataset!r} does not exist")
    _recompute_present_union(db, dataset, data_dir)
    db.commit()
    schema = ds_crud.get_schema_dict(db, dataset)
    dims = {d["name"]: len(d["labels"]) for d in (schema["dimensions"] if schema else [])}
    logger.info("Reconciled present union for %r: %s", dataset, dims)
    return {"dataset": dataset, "reconciled": True, "dimensions": dims}


# Default per-chunk target when no chunk_shape is declared. Far below dask's
# ~128 MiB default so a point query never pulls a huge band (read amplification).
_DEFAULT_CHUNK_TARGET = "8MiB"


def _rechunk(ds: xr.Dataset, chunk_shape: dict[str, int] | None) -> xr.Dataset:
    """Apply the dataset's chunking before writing.

    With ``chunk_shape`` (dim -> size), each listed dim gets that chunk size and
    every other dim is kept as a single chunk (full extent). This is what makes a
    query-aligned layout possible: e.g. ``{"Target": 1}`` (with Response left
    whole) makes each (Target, all-Response) series exactly one chunk, so a series
    read touches one small object instead of a 100+ MB band. With no chunk_shape,
    fall back to a modest ~8 MiB auto target.
    """
    if chunk_shape:
        unknown = set(chunk_shape) - {str(d) for d in ds.sizes}
        if unknown:
            logger.warning("Ignoring chunk_shape dims not in the source: %s", sorted(unknown))
        # Listed dims -> given size; all others -> one chunk (full extent).
        return ds.chunk({str(d): chunk_shape.get(str(d), -1) for d in ds.sizes})

    import dask.config

    # dask "auto" can't estimate the byte size of object-dtype (e.g. string)
    # arrays — as in the correlates store's CorrelateTarget — so chunk those
    # whole and auto-size only the numeric variables.
    with dask.config.set({"array.chunk-size": _DEFAULT_CHUNK_TARGET}):
        for name in list(ds.data_vars):
            ds[name] = ds[name].chunk(-1 if ds[name].dtype.kind == "O" else "auto")
    return ds


def _stringify_coords(ds: xr.Dataset) -> xr.Dataset:
    """Store every coordinate array as strings.

    Coordinate labels are the query's lookup keys, and cheesemonger keys on
    strings uniformly (entrez IDs, screen names, and numeric keys like
    timepoints/ranks alike). Converting here — with the same ``str()`` that
    ``_coord_labels`` applies to the schema — keeps the on-disk coordinates and
    the schema labels identical, so ``.sel()`` matches the labels clients see.
    """
    return ds.assign_coords(
        {c: [str(v) for v in ds.coords[c].values.tolist()] for c in ds.coords}
    )


def _write_dataset(
    ds: xr.Dataset, dest: str, chunk_shape: dict[str, int] | None = None
) -> None:
    """Rechunk (see _rechunk) and write to a Zarr store, with progress."""
    for var in ds.variables.values():
        for key in ("chunks", "preferred_chunks"):
            var.encoding.pop(key, None)

    rechunked = _rechunk(_stringify_coords(ds), chunk_shape)

    try:
        from dask.diagnostics import ProgressBar  # type: ignore[attr-defined]
    except ImportError:
        logger.info("Writing (no dask; progress bar unavailable)...")
        rechunked.to_zarr(dest, mode="w")
        return

    # dt=1.0 so the bar updates every second — enough to see it's alive on a
    # slow remote read without spamming the terminal. The bar covers the whole
    # read+write compute (reading source chunks, writing them to dest).
    with ProgressBar(dt=1.0):
        rechunked.to_zarr(dest, mode="w")


def _normalized_block(block: str) -> str:
    """Replace '.' with '-' in a block name (screen IDs like 'PS-SC-…​.GG01'),
    warning if it changed, so the caller can paste the raw screen ID."""
    normalized = normalize_name(block)
    if normalized != block:
        logger.warning("Normalized block name %r -> %r (dots are not allowed).", block, normalized)
    return normalized


def _verify_written_store(src: xr.Dataset, dest, block: str) -> None:
    """Reopen a freshly written block and check it matches the source.

    A structural check (not a full data read): confirms the store reopens and
    that its data-variable names, dimension sizes, per-variable dims, and
    coordinate labels equal the source's. This catches a wrong/empty/truncated
    write cheaply — before the DB row is committed. Raises LoaderError on any
    mismatch so the caller can discard the write and leave nothing registered.

    It does not read every chunk, so it cannot detect a silently missing chunk
    (Zarr backfills those with a fill value). Structural drift and gross write
    failures — the common interrupted-load outcomes — are caught.
    """
    try:
        written = xr.open_zarr(str(dest))
    except Exception as e:
        raise LoaderError(
            f"Post-write check failed: block {block!r} could not be reopened at "
            f"{dest}: {e}"
        ) from e
    try:
        src_vars = {str(v) for v in src.data_vars}
        got_vars = {str(v) for v in written.data_vars}
        if src_vars != got_vars:
            raise LoaderError(
                f"Post-write check failed for block {block!r}: data variables "
                f"{sorted(got_vars)} != source {sorted(src_vars)}."
            )
        for dim in src.sizes:
            d = str(dim)
            got = int(written.sizes.get(d, -1))
            want = int(src.sizes[dim])
            if got != want:
                raise LoaderError(
                    f"Post-write check failed for block {block!r}: dimension {d!r} "
                    f"size {got} != source {want}."
                )
            if _coord_labels(written, d) != _coord_labels(src, d):
                raise LoaderError(
                    f"Post-write check failed for block {block!r}: coordinate "
                    f"labels for {d!r} do not match the source."
                )
        for v in src.data_vars:
            got_dims = tuple(str(x) for x in written[str(v)].dims)
            want_dims = tuple(str(x) for x in src[v].dims)
            if got_dims != want_dims:
                raise LoaderError(
                    f"Post-write check failed for block {block!r}: datatype {v!r} "
                    f"dims {got_dims} != source {want_dims}."
                )
    finally:
        written.close()


def load_block(
    source: str,
    dataset: str,
    block: str,
    data_dir: str,
    *,
    db: Session,
    last_dimension: str = "Screen",
    create_dataset: bool = False,
    skip_existing: bool = False,
    chunk_shape: dict[str, int] | None = None,
    gene_universe: dict[str, list[str]] | None = None,
) -> dict:
    """Load a Zarr store as a block of ``dataset``.

    Built to be safely rerun after an interrupted bulk load:

    - A *fully loaded* block (registered in the DB **and** present on disk) is
      left untouched: with ``skip_existing`` it is reported as skipped without
      re-reading the source; otherwise it is an error. There is no in-place
      overwrite — delete the block (delete-block) to replace it.
    - A block directory on disk with **no** DB row is an uncommitted partial
      write from an interrupted load (the DB commit happens only after a verified
      write). It is removed and the block is loaded fresh, so a crash mid-write
      self-heals on the next run.

    After writing, the store is reopened and structurally verified against the
    source (see ``_verify_written_store``) before the DB row is committed; a
    mismatch discards the write and raises, leaving nothing registered.

    Args:
        source: Local path or ``gs://`` URL of an xarray-exported Zarr store.
        dataset: Target dataset name.
        block: Block name (one value of the last dimension, e.g. a screen ID).
        data_dir: Root data directory the API serves from.
        db: SQLAlchemy session for metadata operations. The caller owns the
            session lifecycle (open/close); this function commits its work.
        last_dimension: Name of the block key (only used when creating).
        create_dataset: If the dataset doesn't exist, infer and create it.
        skip_existing: If the block is already fully loaded, skip it (report
            ``skipped``) instead of erroring — the safe way to rerun a bulk load.
        chunk_shape: Dim -> chunk size, applied when *creating* the dataset and
            stored on it. Unlisted dims stay whole. For an existing dataset the
            stored chunk_shape is reused (this arg is ignored) so all blocks
            chunk consistently.

    Returns:
        A summary dict (dataset, block, path, skipped, and — unless skipped —
        dimensions, datatypes).
    """
    block = _normalized_block(block)
    dest = ds_paths.block_dir(data_dir, dataset, block)
    registered = ds_crud.block_exists(db, dataset, block)

    # Fully-loaded block (DB row + on-disk store): skip or refuse, but never
    # silently re-read a large remote source on a rerun.
    if registered and dest.exists():
        if skip_existing:
            logger.info("Block %r already loaded in dataset %r — skipping.", block, dataset)
            return {"dataset": dataset, "block": block, "path": str(dest), "skipped": True}
        raise LoaderError(
            f"Block {block!r} already exists in dataset {dataset!r}. Delete it "
            f"first (delete-block) to replace it, or pass skip_existing=True to "
            f"leave it as is."
        )

    # A directory with no DB row is an uncommitted partial write from an
    # interrupted load. It is never a block the API serves, so drop it and load
    # fresh (self-heal).
    if dest.exists() and not registered:
        logger.warning("Removing unregistered block directory (interrupted load?): %s", dest)
        shutil.rmtree(dest)

    logger.info("Opening source store: %s", source)
    try:
        src = xr.open_zarr(source)
    except Exception as e:
        raise LoaderError(f"Could not open source Zarr store {source!r}: {e}") from e

    try:
        existing_schema = ds_crud.get_schema_dict(db, dataset)
        if existing_schema is not None:
            _validate_against_schema(src, existing_schema, dataset, last_dimension)
            resolved_gene_universe = existing_schema.get("gene_universe") or {}
            # Reuse the dataset's stored chunking so all blocks match.
            resolved_chunks = {c["name"]: c["size"] for c in existing_schema["chunk_shape"]}
        elif create_dataset:
            dataset_in = _infer_schema(
                src, dataset, last_dimension, chunk_shape=chunk_shape, gene_universe=gene_universe
            )
            ds_crud.create_dataset(db, dataset_in)
            resolved_gene_universe = dataset_in.gene_universe
            resolved_chunks = {c.name: c.size for c in dataset_in.chunk_shape}
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

        # Validate the block's labels against the gene universe BEFORE writing to
        # disk — fail fast rather than leaving an out-of-gene_universe block behind.
        _validate_against_gene_universe(src, resolved_gene_universe, dataset)

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
        _write_dataset(src, str(dest), resolved_chunks)

        # Structural integrity check before we commit: the write must have
        # produced a store that matches the source. On mismatch, remove the bad
        # write and raise — nothing is committed, so a rerun retries cleanly.
        try:
            _verify_written_store(src, dest, block)
        except LoaderError:
            if dest.exists():
                shutil.rmtree(dest)
            raise

        # Register the block (if new) and fold its labels into the present union.
        # union_present_labels only grows and dedupes, so it is safe both for a
        # fresh add and for re-materializing a registered block whose dir was
        # lost. Replacement goes through delete-block (which shrinks the union).
        if not registered:
            ds_crud.create_block(db, dataset, block)
        ds_crud.union_present_labels(db, dataset, _block_labels_by_dim(src))
        db.commit()

        summary = {
            "dataset": dataset,
            "block": block,
            "path": str(dest),
            "skipped": False,
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
    the session lifecycle (open/close); this function commits its work.
    """
    block = _normalized_block(block)
    if not ds_crud.dataset_exists(db, dataset):
        raise LoaderError(f"Dataset {dataset!r} does not exist")
    if not ds_crud.block_exists(db, dataset, block):
        raise LoaderError(f"Block {block!r} does not exist in dataset {dataset!r}")

    ds_crud.delete_block(db, dataset, block)
    block_path = ds_paths.block_dir(data_dir, dataset, block)
    if block_path.exists():
        shutil.rmtree(block_path)
    # A removal may shrink the present union (genes only this block had), so
    # recompute it from the blocks that remain.
    _recompute_present_union(db, dataset, data_dir)
    db.commit()

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
    removed before the dataset row). Raises LoaderError if it doesn't exist.
    The caller owns the session lifecycle (open/close); this function commits.
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
    db.commit()

    logger.info("Deleted dataset %r (%d block(s))", dataset, len(block_names))
    return {"dataset": dataset, "deleted": True, "blocks_deleted": len(block_names)}
