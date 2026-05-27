#!/usr/bin/env bash
#
# Generate data, deploy to storage, and run all 6 benchmark configs.
#
# Usage:
#   ./scripts/run_benchmark.sh <scale> [bucket]
#
# Examples:
#   ./scripts/run_benchmark.sh small   cheesemonger-benchmark-data
#   ./scripts/run_benchmark.sh medium  cheesemonger-benchmark-data
#
# Requirements:
#   - BUCKET env var or second argument for GCS bucket name
#   - Hyperdisk mounted at /mnt/data
#   - Python env activated with cheesemonger installed

set -euo pipefail

SCALE="${1:?Usage: $0 <scale> [bucket]}"
BUCKET="${2:-${BUCKET:-}}"

if [[ -z "$BUCKET" ]]; then
    echo "ERROR: Provide a GCS bucket name as the second argument or set BUCKET env var."
    exit 1
fi

RESULTS_DIR="results"
mkdir -p "$RESULTS_DIR"

echo "========================================"
echo "  Cheesemonger Benchmark Runner"
echo "  Scale:  $SCALE"
echo "  Bucket: gs://$BUCKET"
echo "========================================"

# ── Step 1: Generate data (sections 7) ─────────────────────────────────

echo ""
echo "=== Step 1/3: Generating data ==="
echo ""

python3 -m cheesemonger simulate --scale "$SCALE" --format both --chunk-preset big -v
python3 -m cheesemonger simulate --scale "$SCALE" --format both --chunk-preset small -v

# ── Step 2: Deploy data to storage (section 8) ─────────────────────────

echo ""
echo "=== Step 2/3: Deploying data ==="
echo ""

echo "Uploading Zarr stores to GCS..."
gcloud storage rsync -r \
    "data/simulated/chunks_big/pesca_simulated_${SCALE}.zarr/" \
    "gs://${BUCKET}/chunks_big/pesca_simulated_${SCALE}.zarr/"

gcloud storage rsync -r \
    "data/simulated/chunks_small/pesca_simulated_${SCALE}.zarr/" \
    "gs://${BUCKET}/chunks_small/pesca_simulated_${SCALE}.zarr/"

echo "Copying files to Hyperdisk..."
mkdir -p /mnt/data/chunks_big /mnt/data/chunks_small

cp "data/simulated/chunks_big/pesca_simulated_${SCALE}.nc" /mnt/data/chunks_big/
cp "data/simulated/chunks_small/pesca_simulated_${SCALE}.nc" /mnt/data/chunks_small/
cp -r "data/simulated/chunks_big/pesca_simulated_${SCALE}.zarr" /mnt/data/chunks_big/
cp -r "data/simulated/chunks_small/pesca_simulated_${SCALE}.zarr" /mnt/data/chunks_small/

# ── Step 3: Run benchmarks (section 9) ─────────────────────────────────

echo ""
echo "=== Step 3/3: Running benchmarks ==="
echo ""

run_bench() {
    local label=$1; shift
    local out_cold="${RESULTS_DIR}/${label}_cold_${SCALE}.txt"
    local out_warm="${RESULTS_DIR}/${label}_warm_${SCALE}.txt"

    echo "--- ${label} (cold) → ${out_cold}"
    sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    sleep 2
    python3 -m cheesemonger benchmark "$@" -n 20 --warmup 0 \
        2>&1 | tee "$out_cold"

    echo "--- ${label} (warm) → ${out_warm}"
    python3 -m cheesemonger benchmark "$@" -n 20 --warmup 0 \
        2>&1 | tee "$out_warm"
}

# Config A: Zarr on GCS
run_bench A1 --data-dir "gs://${BUCKET}" --format zarr --scale "$SCALE" --chunk-preset big
run_bench A2 --data-dir "gs://${BUCKET}" --format zarr --scale "$SCALE" --chunk-preset small

# Config B: NetCDF on Hyperdisk
run_bench B1 --data-dir /mnt/data --format netcdf --scale "$SCALE" --chunk-preset big
run_bench B2 --data-dir /mnt/data --format netcdf --scale "$SCALE" --chunk-preset small

# Config C: Zarr on Hyperdisk (control)
run_bench C1 --data-dir /mnt/data --format zarr --scale "$SCALE" --chunk-preset big
run_bench C2 --data-dir /mnt/data --format zarr --scale "$SCALE" --chunk-preset small

echo ""
echo "========================================"
echo "  Done. Results in ${RESULTS_DIR}/:"
ls -1 "${RESULTS_DIR}"/*_"${SCALE}".txt 2>/dev/null || echo "  (no result files found)"
echo "========================================"
