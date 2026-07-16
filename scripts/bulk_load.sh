#!/usr/bin/env bash
#
# bulk_load.sh — load a whole bucket of per-screen Zarr stores into cheesemonger
# as the 4-dataset layout (degs / response_agg / target_agg / correlates), one
# block per screen. Runs on the deployment HOST, via `docker exec` into the
# running container (so it uses the container's env, /mnt/data volume, and GCS
# credentials).
#
# It expects each screen to have four stores named:
#   <screen>_degs.zarr
#   <screen>_degs_response_agg.zarr
#   <screen>_degs_target_agg.zarr
#   <screen>_degs_target_corr.zarr
# Screens are discovered automatically from the *_degs.zarr stores.
#
# Usage (on the host):
#   BUCKET=gs://cds_perturbseq_datasets/ps100_deg_zarrs DATASET_PREFIX=ps100 \
#       ./bulk_load.sh
#
# Options (env vars):
#   BUCKET            (required) gs:// prefix holding the *_degs.zarr stores
#   DATASET_PREFIX    prefix for the 4 dataset names (default: derived from BUCKET)
#   TARGET_CHUNK      chunk size for the Target dim on degs/correlates (default: 1;
#                     use 32–64 for full-size screens to avoid millions of files)
#   LAST_DIMENSION    name of the block key, set when each dataset is created
#                     (default: Screen)
#   OVERWRITE=1       replace blocks that already exist (for reruns)
#   DRY_RUN=1         print the commands without running them
#   CHEESE_CONTAINER  docker container name (default: cheesemonger)
set -o pipefail

BUCKET="${BUCKET:?set BUCKET=gs://.../<folder> }"
BUCKET="${BUCKET%/}"
CONTAINER="${CHEESE_CONTAINER:-cheesemonger}"
PREFIX="${DATASET_PREFIX:-$(basename "$BUCKET")}"
TARGET_CHUNK="${TARGET_CHUNK:-1}"
LAST_DIMENSION="${LAST_DIMENSION:-Screen}"
OVERWRITE="${OVERWRITE:-}"
DRY_RUN="${DRY_RUN:-}"

tty=()
[ -t 1 ] && tty=(-t)
ow=()
[ -n "$OVERWRITE" ] && ow=(--overwrite)

# Per-dataset source suffix + chunk flags. degs/correlates get query-aligned
# chunking (fix Target+Timepoint, read the vector axis whole); the tiny
# aggregations use the loader's default.
degs_chunks="--chunk Target=${TARGET_CHUNK} --chunk Timepoint=1"
corr_chunks="--chunk Target=${TARGET_CHUNK} --chunk Timepoint=1"

_cli() {  # run a cheesemonger CLI command in the container
    docker exec "${tty[@]}" -e PYTHONUNBUFFERED=1 "$CONTAINER" python -m cheesemonger "$@"
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
echo "Found ${#screens[@]} screen(s). Datasets: ${PREFIX}_degs, ${PREFIX}_response_agg, ${PREFIX}_target_agg, ${PREFIX}_correlates"
echo

declare -a failures=()

load_one() {  # dataset  source  "chunk flags"  screen
    local dataset="$1" source="$2" chunks="$3" screen="$4"
    echo "+ load $dataset  <-  $(basename "$source")  block=$screen"
    [ -n "$DRY_RUN" ] && return 0
    # shellcheck disable=SC2086  (chunks must word-split into flags)
    if ! _cli load --source "$source" --dataset "$dataset" --block "$screen" \
            --last-dimension "$LAST_DIMENSION" --create-dataset $chunks "${ow[@]}"; then
        failures+=("$dataset/$screen")
    fi
}

for screen in "${screens[@]}"; do
    echo "=== $screen ==="
    load_one "${PREFIX}_degs"         "$BUCKET/${screen}_degs.zarr"              "$degs_chunks" "$screen"
    load_one "${PREFIX}_response_agg" "$BUCKET/${screen}_degs_response_agg.zarr" ""             "$screen"
    load_one "${PREFIX}_target_agg"   "$BUCKET/${screen}_degs_target_agg.zarr"   ""             "$screen"
    load_one "${PREFIX}_correlates"   "$BUCKET/${screen}_degs_target_corr.zarr"  "$corr_chunks" "$screen"
    echo
done

if [ "${#failures[@]}" -ne 0 ]; then
    echo "${#failures[@]} load(s) FAILED:" >&2
    printf '  %s\n' "${failures[@]}" >&2
    exit 1
fi

echo "All loads succeeded."
[ -z "$DRY_RUN" ] && _cli status
