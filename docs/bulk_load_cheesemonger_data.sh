#!/usr/bin/env bash
#
# bulk_load_cheesemonger_data.sh — load a whole bucket of per-screen Zarr stores into cheesemonger
# as the 5-dataset layout (degs / response_agg / response_corr / target_agg /
# target_corr), one block per screen. Runs on the deployment HOST, via
# `docker exec` into the running container (so it uses the container's env,
# /mnt/data volume, and GCS credentials).
#
# It expects each screen to have five stores named:
#   <screen>_degs.zarr                 -> <prefix>_degs
#   <screen>_degs_response_agg.zarr    -> <prefix>_response_agg
#   <screen>_degs_response_corr.zarr   -> <prefix>_response_corr
#   <screen>_degs_target_agg.zarr      -> <prefix>_target_agg
#   <screen>_degs_target_corr.zarr     -> <prefix>_target_corr
# Screens are discovered automatically from the *_degs.zarr stores.
#
# Usage (on the host):
#   BUCKET=gs://cds_perturbseq_datasets/ps100_deg_zarrs DATASET_PREFIX=ps100 \
#       ./bulk_load_cheesemonger_data.sh
#
# Rerun semantics: by default every load passes --skip-existing, so a block that
# is already loaded (DB row + store on disk) is left untouched and reported as
# skipped. That makes the script safe to rerun after an interruption or after new
# screens land in the bucket: only missing blocks are loaded. (A half-written
# block from an interrupted load has no DB row; the loader removes it and loads
# it fresh on the next run.) The CLI has no in-place overwrite — to re-load
# blocks, use REPLACE=1, which runs `delete-block` and then `load` for each one.
#
# Chunking: the --chunk flags below only take effect when a dataset is CREATED
# (the first block loaded into it). Later blocks reuse the dataset's stored chunk
# shape and ignore --chunk. To change a dataset's chunking, delete-dataset
# --force it and reload; REPLACE=1 does not change chunking.
#
# Options (env vars):
#   BUCKET            (required) gs:// prefix holding the *_degs.zarr stores
#   DATASET_PREFIX    prefix for the 5 dataset names (default: derived from BUCKET;
#                     pass e.g. DATASET_PREFIX=ps100 for tidy names)
#   TARGET_CHUNK      chunk size for the keyed dim (Target/Response) on the
#                     CORRELATE stores only (target_corr/response_corr; default: 1024).
#                     Balances the two access patterns: a full-axis value-filter scan
#                     touches only ~(axis/1024) chunks (fast), while a single-entity
#                     lookup ("read one target's top-N correlates") still reads just
#                     one ~400 KB chunk. Lower it (e.g. 256) to favor point lookups,
#                     raise it (e.g. 4096) to favor scans. Avoid 1: that makes one tiny
#                     chunk per entity (tens of thousands of files) and cripples scans.
#   DEGS_TARGET_CHUNK   Target chunk size for the degs hypercube (default: 1000, per
#                     docs/data_storage_design.md)
#   DEGS_RESPONSE_CHUNK Response chunk size for the degs hypercube (default: 5000, per
#                     docs/data_storage_design.md). The default (Timepoint=1,
#                     Target=1000, Response=5000) float32 chunk is ~20 MB and keeps
#                     BOTH "fix Target, sweep Response" and "fix Response, sweep
#                     Target" acceptable. Override both for a different 2-D balance,
#                     e.g. DEGS_TARGET_CHUNK=100 DEGS_RESPONSE_CHUNK=1000.
#   LAST_DIMENSION    name of the block key, set when each dataset is created
#                     (default: Screen)
#   SKIP_EXISTING     1 (default) passes --skip-existing so already-loaded blocks
#                     are skipped; set SKIP_EXISTING=0 to make an already-loaded
#                     block an error (counted as a failure) instead
#   REPLACE=1         re-load blocks that already exist: runs `delete-block` then
#                     `load` for every (dataset, screen). Destructive — if the
#                     load then fails, the old block is already gone. A
#                     "does not exist" from delete-block (new block/dataset) is
#                     ignored. Overrides SKIP_EXISTING.
#   SKIP_PREFLIGHT=1  skip the dims/dtypes inspection of the first screen
#   DRY_RUN=1         print the commands without running them
#   CHEESE_CONTAINER  docker container name (default: cheesemonger)
set -o pipefail

BUCKET="${BUCKET:?set BUCKET=gs://.../<folder> }"
BUCKET="${BUCKET%/}"
CONTAINER="${CHEESE_CONTAINER:-cheesemonger}"
PREFIX="${DATASET_PREFIX:-$(basename "$BUCKET")}"
# Correlate-store chunk size on the keyed dim (Target/Response). 1024 balances
# full-axis scans (~axis/1024 chunks) against single-entity lookups (~400 KB/read).
# Avoid 1 — it makes one tiny chunk per entity and cripples value-filter scans.
TARGET_CHUNK="${TARGET_CHUNK:-1024}"
# degs hypercube chunking. Defaults to the design-doc layout
# (Timepoint=1, Target=1000, Response=5000) = ~20 MB/chunk; see
# docs/data_storage_design.md. Override either for a different 2-D balance.
DEGS_TARGET_CHUNK="${DEGS_TARGET_CHUNK:-1000}"
DEGS_RESPONSE_CHUNK="${DEGS_RESPONSE_CHUNK:-5000}"
LAST_DIMENSION="${LAST_DIMENSION:-Screen}"
SKIP_EXISTING="${SKIP_EXISTING:-1}"
REPLACE="${REPLACE:-}"
DRY_RUN="${DRY_RUN:-}"

tty=()
[ -t 1 ] && tty=(-t)
skip=()
if [ -z "$REPLACE" ] && [ "$SKIP_EXISTING" = "1" ]; then
    skip=(--skip-existing)
fi

# Per-dataset chunk flags. The degs hypercube uses the design-doc 2-D layout
# (Target × Response bands; see docs/data_storage_design.md). The two correlate
# stores band the keyed dim (Target/Response) into TARGET_CHUNK-wide chunks with
# the Rank axis whole — big enough that a full-axis value-filter scan reads only a
# few dozen chunks, small enough that a single-entity lookup reads just one. The
# tiny aggregation stores use the loader's default. Passing an explicit --chunk
# also routes the correlate stores (object-dtype coordinate arrays) away from
# dask's "auto" path, which can't size object dtype — so they load even on a
# container image that predates the object-dtype auto-chunk fix.
degs_chunks="--chunk Target=${DEGS_TARGET_CHUNK} --chunk Timepoint=1 --chunk Response=${DEGS_RESPONSE_CHUNK}"
target_corr_chunks="--chunk Target=${TARGET_CHUNK} --chunk Timepoint=1"
response_corr_chunks="--chunk Response=${TARGET_CHUNK} --chunk Timepoint=1"

_cli() {  # run a cheesemonger CLI command in the container
    docker exec "${tty[@]}" -e PYTHONUNBUFFERED=1 "$CONTAINER" python -m cheesemonger "$@"
}

_cli_captured() {  # same, without a TTY so stdout+stderr can be captured
    docker exec -e PYTHONUNBUFFERED=1 "$CONTAINER" python -m cheesemonger "$@" 2>&1
}

echo "Discovering screens in $BUCKET ..."
screens=()
while IFS= read -r line; do
    [ -n "$line" ] && screens+=("$line")
done < <(docker exec -e BUCKET="$BUCKET" "$CONTAINER" python -c '
import os, gcsfs
b = os.environ["BUCKET"].rstrip("/")
fs = gcsfs.GCSFileSystem()
for p in sorted(fs.glob(f"{b}/*_degs.zarr")):
    name = p.rstrip("/").rsplit("/", 1)[-1]
    print(name[: -len("_degs.zarr")])
')

if [ "${#screens[@]}" -eq 0 ]; then
    echo "No *_degs.zarr stores found under $BUCKET" >&2
    exit 1
fi
echo "Found ${#screens[@]} screen(s). Datasets: ${PREFIX}_degs, ${PREFIX}_response_agg, ${PREFIX}_response_corr, ${PREFIX}_target_agg, ${PREFIX}_target_corr"
if [ -n "$REPLACE" ]; then
    echo "Mode: REPLACE (delete-block, then load, for every block)"
elif [ "${#skip[@]}" -gt 0 ]; then
    echo "Mode: skip existing (already-loaded blocks are left untouched)"
else
    echo "Mode: strict (an already-loaded block is an error)"
fi
echo

# Pre-flight: open the first screen's five stores and print their dims + data
# variables (with dtypes) so you can confirm the correlate chunk axes below are
# right (query-align on the read/vector axis; object-dtype vars are the reason
# correlates need explicit --chunk). Read-only; also runs under DRY_RUN.
if [ -z "${SKIP_PREFLIGHT:-}" ]; then
    first="${screens[0]}"
    echo "Pre-flight: inspecting the five stores for '$first' ..."
    docker exec -e BUCKET="$BUCKET" -e SCREEN="$first" "$CONTAINER" python -c '
import os, xarray as xr
b = os.environ["BUCKET"].rstrip("/")
s = os.environ["SCREEN"]
stores = [
    ("degs",          f"{b}/{s}_degs.zarr"),
    ("response_agg",  f"{b}/{s}_degs_response_agg.zarr"),
    ("response_corr", f"{b}/{s}_degs_response_corr.zarr"),
    ("target_agg",    f"{b}/{s}_degs_target_agg.zarr"),
    ("target_corr",   f"{b}/{s}_degs_target_corr.zarr"),
]
for label, path in stores:
    try:
        ds = xr.open_zarr(path)
    except Exception as e:
        print(f"  [{label}] ERROR opening {path}: {e}")
        continue
    dims = ", ".join(f"{d}={int(ds.sizes[d])}" for d in ds.sizes) or "(none)"
    print(f"  [{label}] dims: {dims}")
    for v in ds.data_vars:
        obj = " (object dtype -> needs explicit --chunk)" if ds[v].dtype.kind == "O" else ""
        print(f"      {v}: {ds[v].dtype} {tuple(str(d) for d in ds[v].dims)}{obj}")
    ds.close()
' || echo "  (pre-flight inspection failed; continuing anyway)"
    echo
fi

declare -a failures=()

load_one() {  # dataset  source  "chunk flags"  screen
    local dataset="$1" source="$2" chunks="$3" screen="$4"
    if [ -n "$REPLACE" ]; then
        echo "+ delete-block $dataset  block=$screen"
        if [ -z "$DRY_RUN" ]; then
            local out
            if ! out="$(_cli_captured delete-block --dataset "$dataset" --block "$screen")"; then
                # Nothing to replace yet (new dataset or block) is fine; anything
                # else is a real failure — don't load over an unknown state.
                if [[ "$out" == *"does not exist"* ]]; then
                    echo "  (nothing to delete: $dataset/$screen not loaded yet)"
                else
                    printf '%s\n' "$out" >&2
                    failures+=("$dataset/$screen (delete-block)")
                    return 0
                fi
            else
                printf '%s\n' "$out"
            fi
        fi
    fi
    echo "+ load $dataset  <-  $(basename "$source")  block=$screen"
    [ -n "$DRY_RUN" ] && return 0
    # shellcheck disable=SC2086 # chunks must word-split into flags
    if ! _cli load --source "$source" --dataset "$dataset" --block "$screen" \
            --last-dimension "$LAST_DIMENSION" --create-dataset $chunks "${skip[@]}"; then
        failures+=("$dataset/$screen")
    fi
}

for screen in "${screens[@]}"; do
    echo "=== $screen ==="
    load_one "${PREFIX}_degs"          "$BUCKET/${screen}_degs.zarr"               "$degs_chunks"          "$screen"
    load_one "${PREFIX}_response_agg"  "$BUCKET/${screen}_degs_response_agg.zarr"  ""                      "$screen"
    load_one "${PREFIX}_response_corr" "$BUCKET/${screen}_degs_response_corr.zarr" "$response_corr_chunks" "$screen"
    load_one "${PREFIX}_target_agg"    "$BUCKET/${screen}_degs_target_agg.zarr"    ""                      "$screen"
    load_one "${PREFIX}_target_corr"   "$BUCKET/${screen}_degs_target_corr.zarr"   "$target_corr_chunks"   "$screen"
    echo
done

if [ "${#failures[@]}" -ne 0 ]; then
    echo "${#failures[@]} load(s) FAILED:" >&2
    printf '  %s\n' "${failures[@]}" >&2
    exit 1
fi

echo "All loads succeeded."
if [ -z "$DRY_RUN" ]; then
    _cli status
fi
