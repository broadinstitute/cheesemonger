"""CLI entrypoint for cheesemonger administrative tasks.

Usage:
    python -m cheesemonger load --source gs://bucket/PS-SC-1_degs.zarr \
        --dataset perturb-scuba --block PS-SC-1 --create-dataset
    python -m cheesemonger delete-block --dataset perturb-scuba --block PS-SC-1
    python -m cheesemonger delete-dataset --dataset perturb-scuba --force

All data mutations (create/load/delete of datasets and blocks) happen here, not
over HTTP — the REST API is read-only. Source data is written by
xarray.Dataset.to_zarr() and contains both data variables and coordinate
arrays. These are infrequent admin operations, not user-facing requests.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import get_settings
from .services.loader import LoaderError, delete_block, delete_dataset, load_block


def _ensure_tables(settings) -> None:
    """Create DB tables if they don't exist yet (admin commands run standalone)."""
    from .db import get_engine
    from .models.base import Base
    Base.metadata.create_all(bind=get_engine(settings.sqlalchemy_database_url))


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


def _cmd_load(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir
    _ensure_tables(settings)

    try:
        summary = load_block(
            source=args.source,
            dataset=args.dataset,
            block=args.block,
            data_dir=data_dir,
            last_dimension=args.last_dimension,
            create_dataset=args.create_dataset,
            overwrite=args.overwrite,
            chunk_shape=_parse_chunks(args.chunk) or None,
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
    _ensure_tables(settings)

    try:
        summary = delete_block(args.dataset, args.block, data_dir)
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Deleted block '{summary['block']}' from dataset '{summary['dataset']}'")


def _cmd_delete_dataset(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir
    _ensure_tables(settings)

    try:
        summary = delete_dataset(args.dataset, data_dir, force=args.force)
    except LoaderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Deleted dataset '{summary['dataset']}' "
        f"({summary['blocks_deleted']} block(s) removed)"
    )


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
        "--last-dimension", default="screen",
        help="Name of the block key when creating the dataset (default: screen)",
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

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
