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

Output is written to `data/simulated/` by default, producing `pesca_simulated.zarr/` and/or `pesca_simulated.nc`.

## Data generation flow

The simulation script generates random data matching the real dataset's structure. Data is produced one screen at a time, mimicking the real ingestion pattern.

```
DatasetSchema ──► generate per-screen data ──┬──► Zarr Store
                                       │                   │
                                       ▼                   └──► NetCDF File
                                  ZScore array
                                  L2FC array
                                  FDR array
                                  ... 12 more
```

**Step 1: Define schema.**
`DatasetSchema` declares each dimension's cardinality and each datatype's name, dimensions, and dtype. The schema is the single source of truth for the entire pipeline.

**Step 2: Generate coordinates.**
Synthetic labels are created for each dimension: `"Screen_000"` through `"Screen_029"`, timepoints `[4, 7]`, `"Gene_00000"` through `"Gene_09999"`, and `"RGene_00000"` through `"RGene_17999"`.

**Step 3: Generate per-screen data.**
For each screen, generate a random float32 array of shape `(1, 2, 10000, 18000)` for every 4-D datatype. One screen across all 15 datatypes is about 10 GB uncompressed at full scale.

**Step 4: Append to store.**
Each screen's data is appended along the screen axis. Zarr writes new chunk files. NetCDF extends its unlimited dimension. After all screens, the store is complete.

