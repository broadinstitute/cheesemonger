"""CLI entrypoint for cheesemonger administrative tasks.

Usage:
    python -m cheesemonger load --source gs://bucket/PS-SC-1_degs.zarr \
        --dataset perturb-scuba --block PS-SC-1 --create-dataset
    python -m cheesemonger delete-block --dataset perturb-scuba --block PS-SC-1
    python -m cheesemonger delete-dataset --dataset perturb-scuba --force
    python -m cheesemonger status

All data mutations (create/load/delete of datasets and blocks) happen here, not
over HTTP, the REST API is read-only. Source data is written by
xarray.Dataset.to_zarr() and contains both data variables and coordinate
arrays. These are infrequent admin operations, not user-facing requests.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import SessionLocal, init_db
from .services.loader import (
    LoaderError,
    delete_block,
    delete_dataset,
    load_block,
    reconcile_dataset,
)


@contextmanager
def _session(settings: Settings) -> Iterator[Session]:
    """Open a DB session for one CLI command and close it on the way out.

    Schema creation lives in main() (init_db). The CLI owns the session
    lifecycle so the loader service can stay pure (it just commits its work).
    """
    db = SessionLocal(settings.sqlalchemy_database_url)
    try:
        yield db
    finally:
        db.close()


def _parse_chunks(items: list[str]) -> dict[str, int]:
    """Parse repeated ``--chunk DIM=SIZE`` flags into a {dim: size} dict."""
    chunks: dict[str, int] = {}
    for item in items:
        name, sep, size = item.partition("=")
        if not sep or not name:
            print(f"ERROR: --chunk expects DIM=SIZE, got {item!r}", file=sys.stderr)
            sys.exit(1)
        try:
            chunks[name] = int(size)
        except ValueError:
            print(f"ERROR: chunk size must be an integer, got {item!r}", file=sys.stderr)
            sys.exit(1)
    return chunks


def _build_gene_universe_arg(args: argparse.Namespace) -> dict[str, list[str]] | None:
    """Build the {gene_dim: labels} gene-universe mapping from the CLI flags.

    Requires --gene-dim (which dims to validate) together with a source
    (--gene-universe-taiga-id or --gene-universe-manifest). The same label list
    is applied to every listed gene dimension.
    """
    gene_dims = args.gene_dim or []
    has_source = bool(args.gene_universe_taiga_id or args.gene_universe_manifest)
    if not gene_dims and not has_source:
        return None
    if gene_dims and not has_source:
        print(
            "ERROR: --gene-dim requires a gene-universe source "
            "(--gene-universe-taiga-id or --gene-universe-manifest).",
            file=sys.stderr,
        )
        sys.exit(1)
    if has_source and not gene_dims:
        print(
            "ERROR: a gene-universe source was given but no --gene-dim to apply it to.",
            file=sys.stderr,
        )
        sys.exit(1)

    from .services.gene_universe import build_gene_universe

    labels = build_gene_universe(
        taiga_id=args.gene_universe_taiga_id or "",
        manifest_path=args.gene_universe_manifest or "",
        extras=args.gene_universe_extra or [],
        token_path=get_settings().taiga_token_path,
    )
    print(f"Gene universe: {len(labels)} label(s) applied to dims {gene_dims}")
    return {dim: labels for dim in gene_dims}


def _cmd_load(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir
    gene_universe = _build_gene_universe_arg(args)

    try:
        with _session(settings) as db:
            summary = load_block(
                source=args.source,
                dataset=args.dataset,
                block=args.block,
                data_dir=data_dir,
                db=db,
                last_dimension=args.last_dimension,
                create_dataset=args.create_dataset,
                overwrite=args.overwrite,
                chunk_shape=_parse_chunks(args.chunk) or None,
                gene_universe=gene_universe,
            )
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Loaded block '{summary['block']}' into dataset '{summary['dataset']}'\n"
        f"  path:       {summary['path']}\n"
        f"  dimensions: {summary['dimensions']}\n"
        f"  datatypes:  {len(summary['datatypes'])} ({', '.join(summary['datatypes'])})"
    )


def _cmd_delete_block(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir

    try:
        with _session(settings) as db:
            summary = delete_block(args.dataset, args.block, data_dir, db=db)
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Deleted block '{summary['block']}' from dataset '{summary['dataset']}'")


def _cmd_delete_dataset(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir

    try:
        with _session(settings) as db:
            summary = delete_dataset(args.dataset, data_dir, db=db, force=args.force)
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Deleted dataset '{summary['dataset']}' "
        f"({summary['blocks_deleted']} block(s) removed)"
    )


def _cmd_reconcile(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir

    try:
        with _session(settings) as db:
            summary = reconcile_dataset(args.dataset, data_dir, db=db)
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    dims = ", ".join(f"{name}({n})" for name, n in summary["dimensions"].items())
    print(f"Reconciled present union for '{summary['dataset']}': {dims}")


def _cmd_status(args: argparse.Namespace) -> None:
    settings = get_settings()

    from .crud import dataset as ds_crud

    with _session(settings) as db:
        if args.dataset:
            ds = ds_crud.get_dataset_by_name(db, args.dataset)
            if ds is None:
                print(f"Dataset {args.dataset!r} not found.", file=sys.stderr)
                sys.exit(1)
            datasets = [ds]
        else:
            datasets = ds_crud.list_datasets(db)

        if not datasets:
            print("No datasets loaded.")
            return

        total_blocks = sum(len(ds.blocks) for ds in datasets)
        print(f"{len(datasets)} dataset(s), {total_blocks} block(s) total:\n")
        for ds in datasets:
            blocks = sorted(b.name for b in ds.blocks)
            dims = ", ".join(f"{d['name']}({len(d['labels'])})" for d in ds.dimensions)
            print(f"• {ds.name}")
            print(f"    last_dimension: {ds.last_dimension}")
            print(f"    dimensions:     {dims}")
            print(f"    datatypes:      {len(ds.datatypes)}")
            print(f"    blocks ({len(blocks)}): {', '.join(blocks) or '(none)'}")
            print()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="cheesemonger", description="Cheesemonger CLI")
    subparsers = parser.add_subparsers(dest="command")

    load_parser = subparsers.add_parser("load", help="Load a block from a source Zarr store")
    load_parser.add_argument(
        "--source", required=True,
        help="Path or URL to the source Zarr store (local path or gs://...)",
    )
    load_parser.add_argument("--dataset", required=True, help="Target dataset name")
    load_parser.add_argument("--block", required=True, help="Block name (e.g. screen ID)")
    load_parser.add_argument(
        "--data-dir", default=None,
        help="Data root to load into (default: DATA_DIR from settings/.env)",
    )
    load_parser.add_argument(
        "--last-dimension", default="Screen",
        help="Name of the block key when creating the dataset (default: Screen)",
    )
    load_parser.add_argument(
        "--create-dataset", action="store_true",
        help="Infer and create the dataset schema from the source if it doesn't exist",
    )
    load_parser.add_argument(
        "--overwrite", action="store_true",
        help="Replace the block if it already exists",
    )
    load_parser.add_argument(
        "--chunk", action="append", default=[], metavar="DIM=SIZE",
        help="Chunk a dimension to SIZE (repeatable); unlisted dims stay whole. "
             "Set when creating the dataset. E.g. for series-heavy access: "
             "--chunk Target=1 --chunk Timepoint=1 (leaves Response whole).",
    )
    load_parser.add_argument(
        "--gene-dim", action="append", default=[], metavar="DIM",
        help="Dimension to validate against the gene universe (repeatable), e.g. "
             "--gene-dim Target --gene-dim Response. Requires a gene-universe source.",
    )
    load_parser.add_argument(
        "--gene-universe-taiga-id", default=None, metavar="TAIGA_ID",
        help="Taiga dataset id of an HGNC gene table; its entrez_id column "
             "(normalized to strings) becomes the gene universe. "
             "E.g. hgnc-gene-table-e250.4",
    )
    load_parser.add_argument(
        "--gene-universe-manifest", default=None, metavar="PATH",
        help="Local file of gene-universe labels (one per line, or a JSON list). "
             "Alternative to --gene-universe-taiga-id (for tests/air-gapped loads).",
    )
    load_parser.add_argument(
        "--gene-universe-extra", action="append", default=[], metavar="LABEL",
        help="Extra non-entrez token to add to the gene universe (repeatable), "
             "e.g. --gene-universe-extra Cas9.",
    )
    load_parser.set_defaults(func=_cmd_load)

    del_block_parser = subparsers.add_parser(
        "delete-block", help="Delete a block (DB row + Zarr directory)"
    )
    del_block_parser.add_argument("--dataset", required=True, help="Dataset name")
    del_block_parser.add_argument("--block", required=True, help="Block name to delete")
    del_block_parser.add_argument(
        "--data-dir", default=None,
        help="Data root (default: DATA_DIR from settings/.env)",
    )
    del_block_parser.set_defaults(func=_cmd_delete_block)

    del_ds_parser = subparsers.add_parser(
        "delete-dataset", help="Delete a dataset (DB row + Zarr directory)"
    )
    del_ds_parser.add_argument("--dataset", required=True, help="Dataset name to delete")
    del_ds_parser.add_argument(
        "--data-dir", default=None,
        help="Data root (default: DATA_DIR from settings/.env)",
    )
    del_ds_parser.add_argument(
        "--force", action="store_true",
        help="Delete the dataset's blocks first (otherwise refuse if any remain)",
    )
    del_ds_parser.set_defaults(func=_cmd_delete_dataset)

    status_parser = subparsers.add_parser(
        "status", help="Show loaded datasets, their blocks, dimensions, and datatypes"
    )
    status_parser.add_argument(
        "--dataset", default=None, help="Show only this dataset (default: all)"
    )
    status_parser.set_defaults(func=_cmd_status)

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Rebuild a dataset's present-union labels from the blocks on disk",
    )
    reconcile_parser.add_argument("--dataset", required=True, help="Dataset name")
    reconcile_parser.add_argument(
        "--data-dir", default=None,
        help="Data root (default: DATA_DIR from settings/.env)",
    )
    reconcile_parser.set_defaults(func=_cmd_reconcile)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    # Ensure the schema exists once, up front. The composition root owns DB
    # bootstrap so the loader service never has to.
    init_db(get_settings().sqlalchemy_database_url)
    args.func(args)


if __name__ == "__main__":
    main()
