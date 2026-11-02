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
│  services/  (Business logic)     │  Zarr/xarray reads, loader, Taiga client
│  No HTTP concepts here           │
└────────────┬─────────────────────┘
             │ reads/writes
             ▼
┌──────────────────────────────────┐
│  crud/ + models/  (SQLite)       │  Dataset/block metadata (SQLAlchemy)
└──────────────────────────────────┘

schemas/ (Pydantic models) — request/response shapes, shared across layers
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

`uv sync` also installs the `dev` group (pytest, httpx, pyright, ruff) by
default; use `uv sync --no-dev` for a runtime-only environment.

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

# Production (what the Docker image runs)
uv run gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 cheesemonger.main:app
```

The API docs are available at `http://localhost:8000/docs`.

### Configuration

Settings are read from environment variables (or a `.env` file):


| Variable                  | Default                       | Description                                                          |
| ------------------------- | ----------------------------- | -------------------------------------------------------------------- |
| `DATA_DIR`                | `/mnt/data`                   | Path to the data directory (Zarr blocks) on disk                     |
| `SQLALCHEMY_DATABASE_URL` | `sqlite:///./cheesemonger.db` | Metadata DB (datasets, blocks, labels). Relative to the working dir; point it at persistent storage in production |
| `TAIGA_GENE_MAPPING_ID`   | `""`                          | Taiga dataset ID for gene mapping (loaded at startup)                |
| `TAIGA_TOKEN_PATH`        | `""`                          | Path to the Taiga token file (for Docker: mount and set this)        |
| `THREAD_POOL_SIZE`        | `4`                           | Number of threads for parallel Zarr reads                            |
| `MAX_RESULT_BYTES`        | `1000000000`                  | Reject a `/query` whose estimated response exceeds this (HTTP 413)   |
| `API_PREFIX`              | `""`                          | Optional prefix for all API routes                                   |


Example (local):

```bash
DATA_DIR=/mnt/data TAIGA_GENE_MAPPING_ID="hgnc-gene-table-e250.4/hgnc_complete_set" uv run uvicorn cheesemonger.main:app
```

Example (Docker):

```bash
docker run \
  -v /mnt/hyperdisk:/mnt/data:ro \
  -v /path/to/taiga-token:/etc/cheesemonger/taiga-token:ro \
  -e DATA_DIR=/mnt/data \
  -e TAIGA_GENE_MAPPING_ID=hgnc-gene-table-e250.4/hgnc_complete_set \
  -e TAIGA_TOKEN_PATH=/etc/cheesemonger/taiga-token \
  -p 8000:8000 \
  cheesemonger
```

### Run tests

```bash
uv run pytest tests/ -v
make check        # lint (ruff) + typecheck (pyright) + tests — same gates as CI
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
| `GET`    | `/datasets/{dataset}/dimensions/{dim}` | Full coordinate labels for one dimension |
| `GET`    | `/gene_mappings`                     | Retrieve gene mapping (entrez ↔ symbol) |
| `POST`   | `/datasets/{dataset}/query`          | Query data (series, aggregate, diagonal) |
| `POST`   | `/datasets/{dataset}/filter`         | Cells passing a value predicate (`>`, `<`, `=`, `>=`, `<=`, `in`), with coords |


See [docs/api_design.md](docs/api_design.md) for full API documentation with examples.
There is no authentication in the app itself; on dev.cds.team the service sits
behind oauth2_proxy.

## Python client

[`cheesypy`](clients/cheesypy/) is a standalone client (pandas-friendly) for
querying from Python (read-only; loading is a server-side CLI task):

```python
from cheesypy import Cheesemonger
cm = Cheesemonger("https://dev.cds.team/cheesemonger")
cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
          Screen="PS-SC-1", Timepoint="D4", Target="23293")   # -> DataFrame
cm.filter("perturb-scuba", "FDR", "<", 0.05, datatypes=["L2FC"],
          select={"Screen": "PS-SC-1", "Timepoint": "D4"})    # -> tidy DataFrame
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
| `--last-dimension` | Block-key name when creating (default: `Screen`) |
| `--chunk DIM=SIZE` | Chunk a dimension (repeatable; set when creating). Unlisted dims stay whole |
| `--skip-existing` | Skip a block that's already loaded (safe re-run of a bulk load). There is no overwrite: `delete-block` first to replace |
| `--gene-dim DIM` | Validate this dimension's labels against the gene universe (repeatable) |
| `--gene-universe-taiga-id` / `--gene-universe-manifest` | Gene-universe source for `--gene-dim` (HGNC Taiga table, or a local file) |
| `--gene-universe-extra LABEL` | Extra non-entrez label allowed in the gene universe (e.g. `Cas9`) |
| `--data-dir` | Store root (defaults to `DATA_DIR` from settings/`.env`) |

Run `python -m cheesemonger load --help` for the full flag reference.

A `gs://` source works the same way; it just needs credentials
(`gcloud auth application-default login`, or a VM service account). The
destination is still your local/PD `--data-dir`. The Zarr data lands in
`./local_store/perturb-scuba/blocks/PS-SC-1/`; dataset metadata (dimensions,
labels, datatypes, blocks) is recorded in the SQLite DB
(`SQLALCHEMY_DATABASE_URL`).

On the deployment host, [`scripts/load_cheese`](scripts/load_cheese) wraps these
commands as `docker exec` into the running container
(`load_cheese load --source gs://... --dataset ... --block ...`).

> **Note:** both **broadcasted** and **unbroadcasted** stores work. Reduced-rank
> datatypes (those that span only some dimensions, e.g. `CtrlMean` over
> `[Timepoint, Response]`) load faithfully and are queryable — a query that fixes
> a dimension such a datatype lacks simply ignores it for that datatype.

### Retrieve (query)

Point the server at the same store, then query over HTTP:

```bash
DATA_DIR=./local_store uv run uvicorn cheesemonger.main:app --reload
```

```bash
# What's loaded?
curl -s localhost:8000/datasets | python3 -m json.tool
curl -s localhost:8000/datasets/perturb-scuba | python3 -m json.tool

# Series: ZScore for one perturbation at D4, across all Response genes
curl -s localhost:8000/datasets/perturb-scuba/query \
  -H 'content-type: application/json' \
  -d '{
    "datatypes": ["ZScore"],
    "select": [
      {"dimension": "Screen", "value": "PS-SC-1"},
      {"dimension": "Timepoint", "value": "D4"},
      {"dimension": "Target", "value": "23293"}
    ]
  }'

# Aggregation: mean ZScore across Targets at D4 (one value per Response gene)
curl -s localhost:8000/datasets/perturb-scuba/query \
  -H 'content-type: application/json' \
  -d '{
    "datatypes": ["ZScore"],
    "select": [
      {"dimension": "Screen", "value": "PS-SC-1"},
      {"dimension": "Timepoint", "value": "D4"}
    ],
    "aggregate": {"type": "mean", "over": "Target"}
  }'

# Filter: cells with FDR < 0.05 (plus L2FC at those cells), as tidy columns
curl -s localhost:8000/datasets/perturb-scuba/filter \
  -H 'content-type: application/json' \
  -d '{
    "filter": {"datatype": "FDR", "op": "lt", "value": 0.05},
    "datatypes": ["L2FC"],
    "select": [{"dimension": "Screen", "value": "PS-SC-1"}],
    "limit": 100
  }'
```

In `select`, the block-key dimension (`Screen` by default; the name is set per
dataset and is case-sensitive) routes to a block; the rest index within it. A
single `value` fixes a dimension (dropping it from the result); a list of labels
keeps it, in the given order, and on the block key selects that subset of
blocks. Omitting a dimension spans it. `aggregate` also accepts `median`, `min`,
`max`, `count`, `count_lt`/`count_gt`/`abs_gt` (with `threshold`), and
`fill_value` to treat missing cells as a constant. Responses can be large (one
value per Response gene), so pipe through `python3 -m json.tool` or slice the
arrays client-side; a `/query` estimated above `MAX_RESULT_BYTES` is rejected
with 413. See [docs/api_design.md](docs/api_design.md) for all query patterns
(multi-block, cross-screen aggregation, diagonal).

### Manage (status, reconcile, delete)

Inspection and deletion are CLI operations too — the API cannot mutate data.

```bash
# What's loaded (datasets, blocks, dimension sizes, datatype counts)
uv run python -m cheesemonger status [--dataset perturb-scuba]

# Rebuild a dataset's union-of-labels metadata from the blocks on disk
uv run python -m cheesemonger reconcile --dataset perturb-scuba --data-dir ./local_store

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
│   ├── db.py               # SQLAlchemy engine/session (SQLite metadata DB)
│   ├── api/                # FastAPI routers (HTTP layer, read-only)
│   │   ├── deps.py         # Shared dependencies (DI)
│   │   ├── health.py
│   │   ├── datasets.py     # GET list/detail/dimension labels
│   │   ├── gene_mappings.py
│   │   ├── query.py        # POST /query
│   │   └── filter.py       # POST /filter
│   ├── crud/               # DB reads/writes for datasets and blocks
│   ├── models/             # SQLAlchemy ORM models
│   ├── schemas/            # Pydantic request/response models
│   │   ├── common.py
│   │   ├── dataset.py
│   │   ├── query.py
│   │   ├── filter.py
│   │   └── gene_mappings.py
│   └── services/           # Business logic (Zarr reads, loading, Taiga)
│       ├── dataset.py
│       ├── query.py         # Query + filter engine
│       ├── loader.py        # Block loader (CLI ingest, local or gs://)
│       ├── gene_universe.py # Gene-universe validation for --gene-dim
│       └── gene_mappings.py
├── tests/
├── clients/
│   └── cheesypy/           # Standalone Python client (separate package)
├── scripts/
│   └── load_cheese         # docker-exec wrapper for the CLI on the deploy host
├── docs/
│   ├── api_design.md
│   ├── data_model.md       # Data model: SQLite metadata + Zarr layout
│   ├── data_storage_design.md
│   └── architecture_diagram.md
├── Dockerfile
├── Makefile                # make lint / typecheck / test / check
└── pyproject.toml
```

## Architecture

- **Storage:** Each block (screen) is an xarray Dataset exported as Zarr on Hyperdisk. Data is written via `xarray.Dataset.to_zarr()`, which embeds coordinate labels alongside data variables.
- **Metadata:** Datasets, blocks, dimensions, labels, and datatypes are recorded in a SQLite DB (SQLAlchemy), written by the loader and read by the API.
- **Loading:** A CLI loader (`python -m cheesemonger load`) ingests source Zarr stores (local or `gs://`) into the data directory, inferring or validating the dataset schema. See [Loading & retrieving data](#loading--retrieving-data).
- **Query engine:** Reads blocks via `xarray.open_zarr()` with `.sel()` for label-based indexing. Uses ThreadPoolExecutor for parallel multi-block reads; a single query can read several datatypes at the same coordinates. Multi-block results are aligned to the union of labels, NaN-filled where a block lacks one.
- **Gene mapping:** Loaded from Taiga at startup, served via `/gene_mappings` for client-side translation

See [docs/architecture_diagram.md](docs/architecture_diagram.md) for system diagrams.