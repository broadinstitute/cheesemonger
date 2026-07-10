# Cheesemonger

Low-latency REST API for multi-dimensional perturb-seq data. Serves xarray-exported Zarr stores from Hyperdisk.

Cheesemonger is a **layered FastAPI application** with three layers:

```
HTTP Request
    │
    ▼
┌──────────────────────────────────┐
│  api/  (Routers)                 │  HTTP concerns: validation, status codes, DI
│  Uses Depends() to get services  │
└────────────┬─────────────────────┘
             │ calls
             ▼
┌──────────────────────────────────┐
│  services/  (Business logic)     │  Disk I/O, xarray reads, Taiga client
│  No HTTP concepts here           │
└────────────┬─────────────────────┘
             │ reads/writes
             ▼
┌──────────────────────────────────┐
│  schemas/  (Pydantic models)     │  Data shapes for requests and responses
│  Shared across layers            │
└──────────────────────────────────┘
```

The key rule: **routers never touch disk directly**. They validate the HTTP request, call a service, and return the result. Services know nothing about HTTP (no `HTTPException`, no status codes).

## Quick start

### Prerequisites

- Python 3.11+
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

For local development, first create a `.env` so `DATA_DIR` points at a local
path (the default `/mnt/data` is the production Hyperdisk mount and won't exist
locally):

```bash
cp .env.example .env   # sets DATA_DIR=./data
```

```bash
# Development (auto-reload)
uv run uvicorn cheesemonger.main:app --reload

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

The HTTP API is **read-only**. Datasets and blocks are created, loaded, and
deleted exclusively through the CLI loader (see [Loading & retrieving data](#loading--retrieving-data)).

| Method   | Path                                 | Purpose                                 |
| -------- | ------------------------------------ | --------------------------------------- |
| `GET`    | `/health`                            | Service health check                    |
| `GET`    | `/datasets`                          | List all datasets                       |
| `GET`    | `/datasets/{dataset}`                | Get dataset metadata                    |
| `GET`    | `/gene_mappings`                     | Retrieve gene mapping (entrez ↔ symbol) |
| `POST`   | `/datasets/{dataset}/query`          | Query data                              |


See [docs/api_design.md](docs/api_design.md) for full API documentation with examples.
There is no authentication yet (tracked in [docs/planning.md](docs/planning.md));
on dev.cds.team the service sits behind oauth2_proxy.

## Python client

[`cheesypy`](clients/cheesypy/) is a standalone client (pandas-friendly) for
querying from Python (read-only; loading is a server-side CLI task):

```python
from cheesypy import Cheesemonger
cm = Cheesemonger("https://dev.cds.team/cheesemonger")
cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
          screen="PS-SC-1", Timepoint="D4", Target="23293")   # -> DataFrame
```

See [clients/cheesypy/README.md](clients/cheesypy/README.md) for the full guide.

## Loading & retrieving data

Data flows in two steps: **deposit** a block with the CLI loader, then **retrieve**
it over the HTTP API. A "block" is one value of the dataset's last dimension
(e.g. one screen).

- **Source** — where the data is delivered: a local path or a `gs://` URL of an
  xarray-exported Zarr store (`xarray.Dataset.to_zarr()`). Read-only input.
- **Store** (`DATA_DIR`) — the directory cheesemonger serves from. A local folder
  in development; a mounted Persistent Disk on the VM in production. The loader
  copies source → store; nothing is served from object storage.

### Deposit (load a block)

Loading is a CLI operation (not a REST endpoint), since loads are infrequent
admin tasks that can be slow.

```bash
# First block also creates the dataset — its schema (dimensions, labels,
# datatypes) is inferred from the source store:
uv run python -m cheesemonger load \
  --source data/PS-SC-1_degs_broadcast.zarr \
  --dataset perturb-scuba \
  --block PS-SC-1 \
  --create-dataset \
  --data-dir ./local_store

# Additional blocks into the same dataset (validated against the existing schema):
uv run python -m cheesemonger load \
  --source data/PS-SC-2_degs_broadcast.zarr \
  --dataset perturb-scuba --block PS-SC-2 --data-dir ./local_store
```

| Flag | Purpose |
| ---- | ------- |
| `--source` | Local path or `gs://` URL of the source Zarr store |
| `--dataset` / `--block` | Target dataset and block (e.g. screen ID) names |
| `--create-dataset` | Infer + create the dataset schema if it doesn't exist |
| `--last-dimension` | Block-key name when creating (default: `screen`) |
| `--overwrite` | Replace the block if it already exists |
| `--data-dir` | Store root (defaults to `DATA_DIR` from settings/`.env`) |

A `gs://` source works the same way; it just needs credentials
(`gcloud auth application-default login`, or a VM service account). The
destination is still your local/PD `--data-dir`. On disk you get
`./local_store/perturb-scuba/schema.json` and
`./local_store/perturb-scuba/blocks/PS-SC-1/`.

> **Note:** the loader currently expects the **broadcasted** store form (every
> datatype spans all dimensions). An unbroadcasted store still loads, but queries
> that fix a dimension a reduced-rank datatype lacks aren't supported yet
> (tracked as `TODO(unbroadcast)`).

### Retrieve (query)

Point the server at the same store, then query over HTTP:

```bash
DATA_DIR=./local_store uv run uvicorn cheesemonger.main:app --reload
```

```bash
# What's loaded?
curl -s localhost:8000/datasets | python3 -m json.tool
curl -s localhost:8000/datasets/perturb-scuba | python3 -m json.tool

# Series: ZScore + L2FC + FDR for one perturbation at D4, across all Response genes
curl -s localhost:8000/datasets/perturb-scuba/query \
  -H 'content-type: application/json' \
  -d '{
    "datatype": ["ZScore", "L2FC", "FDR"],
    "select": [
      {"dimension": "screen", "value": "PS-SC-1"},
      {"dimension": "Timepoint", "value": "D4"},
      {"dimension": "Target", "value": "23293"}
    ]
  }'

# Aggregation: mean ZScore across Targets at D4 (one value per Response gene)
curl -s localhost:8000/datasets/perturb-scuba/query \
  -H 'content-type: application/json' \
  -d '{
    "datatype": "ZScore",
    "select": [
      {"dimension": "screen", "value": "PS-SC-1"},
      {"dimension": "Timepoint", "value": "D4"}
    ],
    "aggregate": {"type": "mean", "over": "Target"}
  }'
```

In `select`, the block-key dimension (`screen`) routes to a block; the rest index
within it. Omitting a dimension spans it. Responses can be large (one value per
Response gene), so pipe through `python3 -m json.tool` or slice the arrays
client-side. See [docs/api_design.md](docs/api_design.md) for all query patterns
(multi-block, cross-screen aggregation, diagonal).

### Manage (delete)

Deletion is a CLI operation too — the API cannot mutate data.

```bash
# Delete a single block (DB row + Zarr directory)
uv run python -m cheesemonger delete-block \
  --dataset perturb-scuba --block PS-SC-1 --data-dir ./local_store

# Delete a dataset. Refuses if it still has blocks unless --force removes them first.
uv run python -m cheesemonger delete-dataset \
  --dataset perturb-scuba --force --data-dir ./local_store
```

## Project structure

```
cheesemonger/
├── cheesemonger/           # Application package
│   ├── main.py             # ASGI entrypoint
│   ├── __main__.py         # CLI entrypoint (python -m cheesemonger load ...)
│   ├── startup.py          # App factory (create_app)
│   ├── config.py           # pydantic-settings
│   ├── api/                # FastAPI routers (HTTP layer, read-only)
│   │   ├── deps.py         # Shared dependencies (DI)
│   │   ├── health.py
│   │   ├── datasets.py     # GET list/detail
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
│       ├── loader.py        # Block loader (CLI ingest, local or gs://)
│       └── gene_mappings.py
├── tests/
├── clients/
│   └── cheesypy/           # Standalone Python client (separate package)
├── docs/
│   ├── api_design.md
│   ├── data_model.md       # Data model: SQLite metadata + Zarr layout
│   ├── data_storage_design.md
│   ├── architecture_diagram.md
│   ├── deployment.md       # Deploying to dev.cds.team (Docker + Persistent Disk)
│   └── planning.md         # Living TODO / decisions tracker
├── Dockerfile
└── pyproject.toml
```

## Architecture

- **Storage:** Each block (screen) is an xarray Dataset exported as Zarr on Hyperdisk. Data is written via `xarray.Dataset.to_zarr()`, which embeds coordinate labels alongside data variables.
- **Loading:** A CLI loader (`python -m cheesemonger load`) ingests source Zarr stores (local or `gs://`) into the data directory, inferring or validating the dataset schema. See [Loading & retrieving data](#loading--retrieving-data).
- **Query engine:** Reads blocks via `xarray.open_zarr()` with `.sel()` for label-based indexing. Uses ThreadPoolExecutor for parallel multi-block/multi-datatype reads.
- **Gene mapping:** Loaded from Taiga at startup, served via `/gene_mappings` for client-side translation

See `[docs/architecture_diagram.md](docs/architecture_diagram.md)` for system diagrams.