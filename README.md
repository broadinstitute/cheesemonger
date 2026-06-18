# Cheesemonger

Low-latency REST API for multi-dimensional perturb-seq data. Serves xarray-exported Zarr stores from Hyperdisk with sub-second query latency.

## Quick start

### Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/) (for dependency management)

### Install

```bash
# Clone and install
git clone <repo-url>
cd cheesemonger
uv sync
```

For development (includes pytest, httpx, pyright):

```bash
uv sync --group dev
```

### Run the server

```bash
# Development (auto-reload)
uv run uvicorn cheesemonger.main:uv run uvicorn cheesemonger.main:app --reloadapp --reload

# Production
uv run gunicorn -k uvicorn.workers.UvicornWorker cheesemonger.main:app
```

The API docs are available at `http://localhost:8000/docs`.

### Configuration

Settings are read from environment variables (or a `.env` file):


| Variable                | Default     | Description                                                   |
| ----------------------- | ----------- | ------------------------------------------------------------- |
| `DATA_DIR`              | `/mnt/data` | Path to the data directory on disk                            |
| `TAIGA_GENE_MAPPING_ID` | `""`        | Taiga dataset ID for gene mapping (loaded at startup)         |
| `TAIGA_TOKEN_PATH`      | `""`        | Path to the Taiga token file (for Docker: mount and set this) |
| `THREAD_POOL_SIZE`      | `4`         | Number of threads for parallel Zarr reads                     |
| `API_PREFIX`            | `""`        | Optional prefix for all API routes                            |


Example (local):

```bash
DATA_DIR=/mnt/data TAIGA_GENE_MAPPING_ID="internal-26q1-82aa.94/Gene" uv run uvicorn cheesemonger.main:app
```

Example (Docker):

```bash
docker run \
  -v /mnt/hyperdisk:/mnt/data:ro \
  -v /path/to/taiga-token:/etc/cheesemonger/taiga-token:ro \
  -e DATA_DIR=/mnt/data \
  -e TAIGA_GENE_MAPPING_ID=internal-26q1-82aa.94/Gene \
  -e TAIGA_TOKEN_PATH=/etc/cheesemonger/taiga-token \
  -p 8000:8000 \
  cheesemonger
```

### Run tests

```bash
uv run pytest tests/ -v
```

Tests use temporary directories — no real data or Taiga access needed.

## API endpoints


| Method   | Path                                 | Purpose                                 |
| -------- | ------------------------------------ | --------------------------------------- |
| `GET`    | `/health`                            | Service health check                    |
| `POST`   | `/datasets`                          | Create a new dataset (define schema)    |
| `GET`    | `/datasets`                          | List all datasets                       |
| `GET`    | `/datasets/{dataset}`                | Get dataset metadata                    |
| `DELETE` | `/datasets/{dataset}`                | Delete an empty dataset                 |
| `DELETE` | `/datasets/{dataset}/blocks/{block}` | Delete a block                          |
| `GET`    | `/gene_mappings`                     | Retrieve gene mapping (entrez ↔ symbol) |
| `POST`   | `/datasets/{dataset}/query`          | Query data                              |


See `[docs/api_design_draft.md](docs/api_design_draft.md)` for full API documentation with examples.

## Data loading

Block loading is a CLI operation (not part of the REST API). Source data must be an xarray-exported Zarr store (written by `xarray.Dataset.to_zarr()`):

```bash
python -m cheesemonger load \
  --dataset pesca \
  --block MCF7 \
  --source gs://lab-results/experiment-42/pesca_output/
```

## Project structure

```
cheesemonger/
├── cheesemonger/           # Application package
│   ├── main.py             # ASGI entrypoint
│   ├── startup.py          # App factory (create_app)
│   ├── config.py           # pydantic-settings
│   ├── api/                # FastAPI routers (HTTP layer)
│   │   ├── deps.py         # Shared dependencies (DI)
│   │   ├── health.py
│   │   ├── datasets.py
│   │   ├── blocks.py
│   │   ├── gene_mappings.py
│   │   └── query.py
│   ├── schemas/            # Pydantic request/response models
│   │   ├── common.py
│   │   ├── dataset.py
│   │   ├── query.py
│   │   └── gene_mappings.py
│   └── services/           # Business logic (disk + Zarr operations)
│       ├── dataset.py
│       ├── query.py
│       └── gene_mappings.py
├── tests/
├── docs/
│   ├── api_design_draft.md
│   ├── data_storage_design.md
│   └── architecture_diagram.md
└── pyproject.toml
```

## Architecture

- **Storage:** Each block (screen) is an xarray Dataset exported as Zarr on Hyperdisk. Data is written via `xarray.Dataset.to_zarr()`, which embeds coordinate labels alongside data variables.
- **Query engine:** Reads blocks via `xarray.open_zarr()` with `.sel()` for label-based indexing. Uses ThreadPoolExecutor for parallel multi-block/multi-datatype reads.
- **Gene mapping:** Loaded from Taiga at startup, served via `/gene_mappings` for client-side translation

See `[docs/architecture_diagram.md](docs/architecture_diagram.md)` for system diagrams.