"""CLI entrypoint for cheesemonger administrative tasks.

Usage:
    python -m cheesemonger load --source gs://bucket/PS-SC-1_degs.zarr \
        --dataset perturb-scuba --block PS-SC-1 --create-dataset

The CLI handles data loading (copying/validating xarray-exported Zarr stores
into the dataset's block directory). Source data is written by
xarray.Dataset.to_zarr() and contains both data variables and coordinate
arrays. This is separate from the REST API because block loads are infrequent
admin operations, not user-facing requests.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import get_settings
from .services.loader import LoaderError, load_block


def _cmd_load(args: argparse.Namespace) -> None:
    settings = get_settings()
    data_dir = args.data_dir or settings.data_dir

    # Ensure DB tables exist before loading
    from .db import get_engine
    from .models.base import Base
    Base.metadata.create_all(bind=get_engine(settings.sqlalchemy_database_url))

    try:
        summary = load_block(
            source=args.source,
            dataset=args.dataset,
            block=args.block,
            data_dir=data_dir,
            last_dimension=args.last_dimension,
            create_dataset=args.create_dataset,
            overwrite=args.overwrite,
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
    load_parser.set_defaults(func=_cmd_load)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
