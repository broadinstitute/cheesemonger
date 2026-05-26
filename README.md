# cheesemonger

A storage system for large perturb-seq data.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create the virtual environment and install all dependencies
uv sync --all-extras

# Activate the environment
source .venv/bin/activate
```

## Running the simulation

Generate simulated datasets for benchmarking:

```bash
# Default: "small" scale, both Zarr and NetCDF formats
python3 -m cheesemonger simulate

# Choose a scale preset
python3 -m cheesemonger simulate --scale tiny      # ~3 MB, fast
python3 -m cheesemonger simulate --scale small     # ~670 MB (default)
python3 -m cheesemonger simulate --scale medium    # ~28 GB
python3 -m cheesemonger simulate --scale full      # ~300 GB

# Choose output format
python3 -m cheesemonger simulate --format zarr
python3 -m cheesemonger simulate --format netcdf
python3 -m cheesemonger simulate --format both

# Verbose logging and custom output directory
python3 -m cheesemonger simulate --scale tiny --format both -v --output-dir data/my_test
```

Output is written to `data/simulated/pesca/zarr/` and `data/simulated/pesca/netcdf/` by default. Each screen gets its own independent store.

## Storage layout

Each screen is stored as a separate Zarr store or NetCDF file, making screen-level add/delete/replace operations O(1):

```
data/simulated/pesca/zarr/
├── _registry.json              ← list of active screens
├── Screen_000/
│   └── data.zarr/              ← 3-D arrays: (timepoint, perturbation, gene_expression)
├── Screen_001/
│   └── data.zarr/
└── ...
```

Deleting a screen removes one directory and updates the registry — no other screen's data is touched.

## Running benchmarks

After generating data, benchmark query latency:

```bash
# Benchmark both formats on tiny data (quick sanity check)
python3 -m cheesemonger simulate --scale tiny --format both
python3 -m cheesemonger benchmark --scale tiny --format both

# Benchmark with more iterations and warmup
python3 -m cheesemonger benchmark --scale small --format zarr -n 20 --warmup 3 -v
```

The benchmark runs 9 query patterns (6 series, 2 aggregation, 1 diagonal) and reports min/p50/p95/p99/max latency for each.

## Data generation flow

The simulation script generates random data matching the real dataset's structure. Data is produced one screen at a time, mimicking the real ingestion pattern.

```
CSV Input ──► DatasetSchema ──► generate per-screen data ──┬──► Zarr Store (per screen)
                                       │                   │
                                       ▼                   └──► NetCDF File (per screen)
                                  ZScore array
                                  L2FC array
                                  FDR array
                                  ... 12 more
```

**Step 1: Define schema.**
`DatasetSchema` declares each dimension's cardinality and each datatype's name, dimensions, and dtype. The schema is the single source of truth for the entire pipeline.

**Step 2: Generate coordinates.**
Synthetic labels are created for each dimension: timepoints `[4, 7]`, `"Gene_00000"` through `"Gene_09999"`, and `"RGene_00000"` through `"RGene_17999"`.

**Step 3: Generate per-screen data.**
For each screen, generate a random float32 array of shape `(2, 10000, 18000)` for every 3-D datatype. One screen across all 15 datatypes is about 10 GB uncompressed at full scale.

**Step 4: Write each screen.**
Each screen's data is written as an independent store (Zarr directory or NetCDF file) and registered in `_registry.json`.

## Jupyter notebooks

After activating the environment, register the kernel and launch Jupyter:

```bash
python3 -m ipykernel install --user --name cheesemonger --display-name "cheesemonger"
jupyter notebook
```

Select the **cheesemonger** kernel when opening notebooks.
