# Cheesemonger Architecture

Dimension names in these diagrams follow the deployed data: `Timepoint`, `Target`
(knocked-out gene), `Response` (measured gene), with the block key (last
dimension) `Screen`. Dataset and block names (`ps100_degs`, `SW620`, ...) are
illustrative.

## System Architecture

```mermaid
graph TB
    subgraph Clients
        WEB[Web App / Portal]
        PY["Python client (cheesypy)"]
        CLI_USER[Admin CLI user]
    end

    SRC[("Source xarray Zarr stores<br/>(GCS gs:// or local)")]

    subgraph VM["GCP VM — n4-standard-4"]
        subgraph FastAPI Server
            API["REST API — 7 endpoints<br/>GET /health<br/>GET /datasets<br/>GET /datasets/{dataset}<br/>GET /datasets/{dataset}/dimensions/{dim}<br/>GET /gene_mappings<br/>POST /datasets/{dataset}/query<br/>POST /datasets/{dataset}/filter"]
            TP["QueryService ThreadPool<br/>(THREAD_POOL_SIZE, default 4)"]
            GM["Gene Mapping Cache<br/>entrez ↔ symbol"]
        end

        CLI["python -m cheesemonger<br/>load / delete-block / delete-dataset /<br/>status / reconcile"]

        DB[("SQLite metadata DB<br/>(SQLALCHEMY_DATABASE_URL)<br/>datasets: last_dimension, dimensions<br/>+ present-union labels, datatypes,<br/>chunk_shape, gene_universe<br/>blocks: one row per block")]

        subgraph DISK["Hyperdisk Balanced — 1 TB (DATA_DIR=/mnt/data)"]
            subgraph DSDIR["/mnt/data/ps100_degs/blocks/"]
                SW["SW620/"]
                HT["HT29/"]
                A5["A549/"]
                MORE["... 27 more"]
            end
        end

        subgraph BLOCK["Each block (e.g. SW620/) — xarray Dataset as Zarr v3"]
            ZJ["zarr.json — group metadata<br/>(+ consolidated array metadata)"]
            Z1["ZScore/ — data variable"]
            Z2["L2FC/ — data variable"]
            Z3["FDR/ — data variable"]
            ZN["... more datatypes"]
            ZC["Timepoint/ Target/ Response/<br/>— coordinate arrays"]
        end
    end

    TAIGA[("Taiga<br/>Gene Mapping<br/>Source")]

    WEB -->|JSON over HTTPS| API
    PY -->|JSON over HTTPS| API
    CLI_USER -->|admin commands| CLI

    CLI -->|"xr.open_zarr(source)"| SRC
    CLI -->|"ds.to_zarr(...) — write block"| DSDIR
    CLI -->|"record dataset / block,<br/>update present-union labels"| DB

    API -->|"read metadata<br/>(schema, block list)"| DB
    API -->|read chunks| SW
    TP -.->|parallel block reads| SW
    TP -.->|parallel block reads| HT
    TP -.->|parallel block reads| A5

    API -->|"startup load<br/>(if TAIGA_GENE_MAPPING_ID set)"| TAIGA
    TAIGA -->|DataFrame via taigapy| GM

    SW --- ZJ
    SW --- Z1
    SW --- Z2
    SW --- Z3
    SW --- ZN
    SW --- ZC

    style API fill:#4a9eff,color:#fff
    style TP fill:#6cb4ee,color:#fff
    style GM fill:#8ecae6,color:#000
    style CLI fill:#e76f51,color:#fff
    style TAIGA fill:#f4a261,color:#000
    style SRC fill:#f4a261,color:#000
    style DB fill:#e9c46a,color:#000
    style SW fill:#2a9d8f,color:#fff
    style HT fill:#2a9d8f,color:#fff
    style A5 fill:#2a9d8f,color:#fff
    style ZJ fill:#457b9d,color:#fff
    style Z1 fill:#264653,color:#fff
    style Z2 fill:#264653,color:#fff
    style Z3 fill:#264653,color:#fff
    style ZN fill:#264653,color:#fff
    style ZC fill:#457b9d,color:#fff
```

### Components

| Component | Role |
|-----------|------|
| **FastAPI Server** | Serves the read-only REST API (7 endpoints, listed below). Validates requests with Pydantic, reads dataset metadata from SQLite, and reads block data via `xr.open_zarr()`. The routers in `cheesemonger/api/` never touch disk directly. They go through `services/`. |
| **QueryService ThreadPool** | Parallelizes multi-block reads within a single `/query` or `/filter` request. Each block read opens the store once and pulls all requested datatypes. Size set by `THREAD_POOL_SIZE` (default 4). |
| **Gene Mapping Cache** | In-memory entrez ↔ symbol mapping, loaded from Taiga at startup when `TAIGA_GENE_MAPPING_ID` is set. Otherwise it is empty and `GET /gene_mappings` returns 404. |
| **SQLite metadata DB** | Source of truth for dataset metadata, accessed via SQLAlchemy (`db.py`, `models/`, `crud/`), at `SQLALCHEMY_DATABASE_URL` (default `sqlite:///./cheesemonger.db`). It stores each dataset's last dimension, dimension names with present-union labels, datatypes, chunk shape and gene universe, plus one row per loaded block. Tables are created at startup by both the app and the CLI (`init_db`). |
| **Hyperdisk** | High-performance block storage mounted at `DATA_DIR` (default `/mnt/data/`). Holds all Zarr data under `{dataset}/blocks/{block}/`. |
| **Block (xarray Dataset as Zarr v3)** | One folder per value of the last dimension (e.g. per screen). Contains an xarray Dataset with data variables (datatypes) and coordinate arrays (dimension labels). Written by the loader via `xarray.Dataset.to_zarr()` (zarr 3.1.1, Zarr format v3). |
| **Taiga** | External data platform. Source of the gene mapping file (and optionally the gene universe used at load time). Accessed via `taigapy`. |
| **Admin CLI** | `python -m cheesemonger load \| delete-block \| delete-dataset \| status \| reconcile`. The only write path. `load` opens a source xarray Zarr store (local or `gs://`), validates it against the dataset schema in SQLite (or infers and creates the dataset with `--create-dataset`), writes the block to disk, and records it in SQLite. Not part of the REST API. |

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness check plus gene-mapping load status |
| `GET` | `/datasets` | List datasets |
| `GET` | `/datasets/{dataset}` | Dataset schema: last dimension, dimensions, datatypes, blocks, chunk shape |
| `GET` | `/datasets/{dataset}/dimensions/{dim}` | All labels of one dimension (the present union, or the loaded block names for the last dimension), paged with `offset`/`limit` |
| `GET` | `/gene_mappings` | entrez ↔ symbol mapping for client-side translation |
| `POST` | `/datasets/{dataset}/query` | Read datatypes at selected coordinates, optionally aggregated or along a diagonal |
| `POST` | `/datasets/{dataset}/filter` | Return the individual cells of a datatype that pass a predicate (e.g. `Correlation > 0.75`) |

All paths are mounted under `API_PREFIX` if one is set.

---

## Query Flow

How a typical query moves through the system.

### Single-block series query

"Give me the ZScore for SW620 at day 4, target MDM2 (entrez 4193)."

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI router
    participant DB as SQLite
    participant Q as QueryService
    participant Z as Zarr Store<br/>(SW620/)

    C->>A: POST /datasets/ps100_degs/query
    Note right of C: {"datatypes": ["ZScore"],<br/>"select": [<br/>  {"dimension": "Screen", "value": "SW620"},<br/>  {"dimension": "Timepoint", "value": "D4"},<br/>  {"dimension": "Target", "value": "4193"}<br/>]}

    A->>DB: get schema + block names
    DB-->>A: last_dimension=Screen, dims, datatypes, blocks
    A->>A: Validate datatypes, dimensions, blocks,<br/>estimated result size

    A->>Q: execute(query, schema, blocks)
    Q->>Q: Split select: Screen=SW620 → folder routing,<br/>Timepoint/Target → array selection
    Q->>Z: ds = xr.open_zarr(".../ps100_degs/blocks/SW620/")
    Q->>Z: ds["ZScore"].sel(Timepoint="D4", Target="4193")
    Note right of Z: xarray resolves labels to<br/>chunk indices and reads only<br/>the chunks that cover the series
    Z-->>Q: .values → numpy array (n_response,)

    Q-->>A: QueryOut
    A-->>C: 200 OK
    Note left of A: {"blocks": ["SW620"],<br/>"shape": [n_response],<br/>"index": [{"dimension": "Response", "labels": [...]}],<br/>"data": {"ZScore": [0.48, -1.2, ...]}}
```

**Latency: ~40 ms** (benchmark: big-chunk Zarr on Hyperdisk, see `data_storage_design.md` §4)

### Multi-block aggregation query

"Average ZScore for TP53 (7157) response to MDM2 (4193) knockout at day 7, across all screens."

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI router
    participant DB as SQLite
    participant TP as ThreadPool
    participant Z1 as SW620/
    participant Z2 as HT29/
    participant Z3 as A549/
    participant ZN as ... (27 more)

    C->>A: POST /datasets/ps100_degs/query
    Note right of C: Screen omitted from select<br/>→ query all 30 blocks<br/>aggregate: {"type": "mean", "over": "Screen"}

    A->>DB: get schema + block names
    DB-->>A: 30 blocks

    A->>TP: Dispatch 30 block reads

    par THREAD_POOL_SIZE threads at a time
        TP->>Z1: .sel(Timepoint=D7, Target=4193, Response=7157)
        Z1-->>TP: scalar: -1.8
        TP->>Z2: .sel(Timepoint=D7, Target=4193, Response=7157)
        Z2-->>TP: scalar: -1.3
        TP->>Z3: .sel(Timepoint=D7, Target=4193, Response=7157)
        Z3-->>TP: scalar: -1.5
        TP->>ZN: (... 27 more reads)
        ZN-->>TP: scalars
    end

    TP-->>A: 30 raw scalars collected

    Note over A: mean([-1.8, -1.3, -1.5, ...]) = -1.54<br/>Single-pass aggregation<br/>(not mean of means)

    A-->>C: 200 OK
    Note left of A: {"blocks": ["SW620", "HT29", ...],<br/>"aggregation": "mean",<br/>"data": {"ZScore": -1.54}}
```

**Latency: ~300 ms** (estimate: 30 blocks / 4 threads = ~8 batches × ~40 ms each)

### Value-filter query

"Which target–correlate pairs have Correlation > 0.75 at day 4, across all screens?"

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI router
    participant DB as SQLite
    participant TP as ThreadPool
    participant Z as Each block of<br/>ps100_target_corr

    C->>A: POST /datasets/ps100_target_corr/filter
    Note right of C: {"filter": {"datatype": "Correlation",<br/>  "op": "gt", "value": 0.75},<br/>"datatypes": ["CorrelateTarget"],<br/>"select": [{"dimension": "Timepoint", "value": "D4"}]}

    A->>DB: get schema + block names
    DB-->>A: dims, datatypes, blocks

    A->>TP: Dispatch one filter per block
    TP->>Z: xr.open_zarr, .sel(select),<br/>apply predicate inside xarray
    Z-->>TP: passing cells + their coordinate labels
    TP-->>A: per-block results

    A-->>C: 200 OK
    Note left of A: {"dimensions": [...], "coords": {...},<br/>"data": {"Correlation": [...], "CorrelateTarget": [...]},<br/>"count": N, "truncated": false}
```

Results are capped by the request's `limit` and by a server-side hard cap
(`MAX_FILTER_ROWS`); `truncated` reports whether the cap was hit.

### Data loading flow (CLI)

```mermaid
sequenceDiagram
    participant ADMIN as Admin
    participant CLI as cheesemonger CLI
    participant DB as SQLite
    participant SRC as Source xarray Zarr<br/>(GCS / local)
    participant DISK as Hyperdisk

    ADMIN->>CLI: python -m cheesemonger load<br/>--dataset ps100_degs --block MCF7<br/>--source gs://.../MCF7_degs.zarr<br/>[--create-dataset --chunk DIM=SIZE ...]<br/>[--skip-existing]

    CLI->>DB: Block already registered?
    DB-->>CLI: no (if yes and on disk: skip with --skip-existing, else error)

    CLI->>SRC: src = xr.open_zarr(source)
    SRC-->>CLI: xarray Dataset<br/>(data vars + coordinates)

    CLI->>DB: Read dataset schema
    alt dataset exists
        DB-->>CLI: dims, datatypes, stored chunk_shape
        CLI->>CLI: Validate src dims + datatypes<br/>against the schema
    else --create-dataset
        CLI->>CLI: Infer schema from src<br/>(+ chunk_shape from --chunk)
        CLI->>DB: Create dataset row
    end

    CLI->>CLI: Validate labels against gene universe (if any)

    CLI->>DISK: Rechunk + ds.to_zarr(/mnt/data/ps100_degs/blocks/MCF7/)
    Note right of DISK: Zarr v3: zarr.json + c/ chunks<br/>for each data variable and<br/>coordinate array
    CLI->>DISK: Reopen and verify structure<br/>(discard the write on mismatch)

    CLI->>DB: Insert block row,<br/>fold labels into present union, commit

    CLI-->>ADMIN: Loaded block MCF7
```

To replace a block, run `delete-block` first. It removes the DB row and the
directory, then recomputes the present union from the remaining blocks. There is
no in-place overwrite. `reconcile --dataset NAME` rebuilds the present union from
the blocks on disk.
