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
