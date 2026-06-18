"""CLI entrypoint for cheesemonger administrative tasks.

Usage:
    python -m cheesemonger load --dataset pesca --block MCF7 --source gs://...

The CLI handles data loading (copying/validating xarray-exported Zarr stores
into the dataset's block directory). Source data is written by
xarray.Dataset.to_zarr() and contains both data variables and coordinate
arrays. This is separate from the REST API because block loads are infrequent
admin operations, not user-facing requests.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_load(args: argparse.Namespace) -> None:
    # TODO: Implement block loading
    #   1. Read dataset schema from disk
    #   2. ds = xr.open_zarr(args.source)  (xarray-exported Zarr)
    #   3. Validate ds dimensions, coordinates, and data variables against schema
    #   4. ds.to_zarr(data/{dataset}/blocks/{block}/, ...)
    #   5. Optionally re-chunk to match dataset's chunk_shape
    print(f"Loading block '{args.block}' into dataset '{args.dataset}' from '{args.source}'")
    print("ERROR: Block loading is not yet implemented.")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="cheesemonger", description="Cheesemonger CLI")
    subparsers = parser.add_subparsers(dest="command")

    load_parser = subparsers.add_parser("load", help="Load a block from a source Zarr store")
    load_parser.add_argument("--dataset", required=True, help="Dataset name")
    load_parser.add_argument("--block", required=True, help="Block name (e.g. screen name)")
    load_parser.add_argument("--source", required=True, help="Path or URL to source Zarr store")
    load_parser.set_defaults(func=_cmd_load)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
