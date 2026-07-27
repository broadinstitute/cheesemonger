# Cheesemonger Architecture

## System Architecture

```mermaid
graph TB
    subgraph Clients
        WEB[Web App / Portal]
        PY[Python Client]
        CLI_USER[Admin CLI]
    end

    subgraph GCP VM — n4-standard-4
        subgraph FastAPI Server
            API[REST API<br/>8 endpoints]
            TP[ThreadPool<br/>4 workers]
            GM[Gene Mapping Cache<br/>entrez ↔ symbol]
        end

        subgraph Hyperdisk Balanced — 1 TB
            subgraph "/mnt/data/pesca/"
                SCHEMA[schema.json]
                subgraph "blocks/"
                    SW[SW620/]
                    HT[HT29/]
                    A5[A549/]
                    MORE[... 27 more]
                end
            end
        end

        subgraph "Each block (e.g. SW620/) — xarray Dataset as Zarr"
            Z1[ZScore/ — data variable]
            Z2[L2FC/ — data variable]
            Z3[FDR/ — data variable]
            ZN[... 12 more datatypes]
            ZC[timepoint/ testedperturbation/<br/>testedgeneexpression/<br/>— coordinate arrays]
        end
    end

    TAIGA[(Taiga<br/>Gene Mapping<br/>Source)]

    WEB -->|JSON over HTTPS| API
    PY -->|JSON over HTTPS| API
    CLI_USER -->|"cheesemonger load<br/>(Zarr → disk)"| SCHEMA

    API -->|read chunks| SW
    API -->|read chunks| HT
    API -->|read chunks| A5
    TP -.->|parallel reads| SW
    TP -.->|parallel reads| HT

    API -->|startup load| TAIGA
    TAIGA -->|DataFrame via taigapy| GM

    SW --- Z1
    SW --- Z2
    SW --- Z3
    SW --- ZN
    SW --- ZC

    style API fill:#4a9eff,color:#fff
    style TP fill:#6cb4ee,color:#fff
    style GM fill:#8ecae6,color:#000
    style TAIGA fill:#f4a261,color:#000
    style SCHEMA fill:#e9c46a,color:#000
    style SW fill:#2a9d8f,color:#fff
    style HT fill:#2a9d8f,color:#fff
    style A5 fill:#2a9d8f,color:#fff
    style Z1 fill:#264653,color:#fff
    style Z2 fill:#264653,color:#fff
    style Z3 fill:#264653,color:#fff
    style ZN fill:#264653,color:#fff
    style ZC fill:#457b9d,color:#fff
```

### Components

| Component | Role |
|-----------|------|
| **FastAPI Server** | Serves REST API, validates requests via Pydantic, reads blocks via `xr.open_zarr()` |
| **ThreadPool (4 workers)** | Parallelizes multi-block reads within a single request (each read pulls all requested datatypes) |
| **Gene Mapping Cache** | In-memory entrez ↔ symbol mapping, loaded from Taiga at startup |
| **Hyperdisk** | High-performance block storage mounted at `/mnt/data/`. Stores all Zarr data. |
| **Block (xarray Dataset as Zarr)** | One folder per screen. Contains an xarray Dataset with data variables (datatypes) and coordinate arrays (dimension labels). Written by `xarray.Dataset.to_zarr()`. |
| **Taiga** | External data platform. Source of the gene mapping file. Accessed via `taigapy`. |
| **Admin CLI** | Loads new blocks from source xarray-exported Zarr stores. Validates and copies via `xr.open_zarr()` / `.to_zarr()`. Not part of the REST API. |

---

## Query Flow

How a typical query moves through the system.

### Single-block series query

"Give me the ZScore for SW620 at day 4, perturbation MDM2 (entrez 4193)."

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant V as Validator
    participant R as Router
    participant Z as Zarr Store<br/>(SW620/)

    C->>A: POST /datasets/pesca/query
    Note right of C: {"datatypes": ["ZScore"],<br/>"select": [<br/>  {screen: SW620},<br/>  {timepoint: 4},<br/>  {perturbation: 4193}<br/>]}

    A->>V: Validate against schema
    V-->>A: ✓ All fields valid

    A->>R: Identify last_dimension<br/>→ screen = "SW620"
    R-->>A: Open 1 block

    A->>Z: ds = xr.open_zarr(".../blocks/SW620/")
    A->>Z: ds["ZScore"].sel(timepoint=4, testedperturbation="4193")
    Note right of Z: xarray resolves labels to<br/>chunk indices, reads 4 chunks<br/>(4 × 5000 genes = 18,000)
    Z-->>A: .values → numpy array (18000,)

    A-->>C: 200 OK
    Note left of A: {"blocks": ["SW620"],<br/>"shape": [18000],<br/>"index": [{dim: "testedgeneexpression", ...}],<br/>"data": {"ZScore": [0.48, -1.2, ...]}}
```

**Latency: ~40 ms**

### Multi-block aggregation query

"Average ZScore for TP53 (7157) response to MDM2 (4193) knockout at day 7, across all screens."

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant TP as ThreadPool
    participant Z1 as SW620/
    participant Z2 as HT29/
    participant Z3 as A549/
    participant ZN as ... (27 more)

    C->>A: POST /datasets/pesca/query
    Note right of C: screen omitted from select<br/>→ query all 30 blocks<br/>aggregate: mean over screen

    A->>TP: Dispatch 30 block reads

    par 4 threads at a time
        TP->>Z1: .sel(tp=7, pert=4193, gene=7157)
        Z1-->>TP: scalar: -1.8
        TP->>Z2: .sel(tp=7, pert=4193, gene=7157)
        Z2-->>TP: scalar: -1.3
        TP->>Z3: .sel(tp=7, pert=4193, gene=7157)
        Z3-->>TP: scalar: -1.5
        TP->>ZN: (... 27 more reads)
        ZN-->>TP: scalars
    end

    TP-->>A: 30 raw scalars collected

    Note over A: mean([-1.8, -1.3, -1.5, ...]) = -1.54<br/>Single-pass aggregation<br/>(not mean of means)

    A-->>C: 200 OK
    Note left of A: {"blocks": ["SW620", "HT29", ...],<br/>"aggregation": "mean",<br/>"data": {"ZScore": -1.54}}
```

**Latency: ~300 ms** (30 blocks / 4 threads = ~8 batches × ~40 ms each)

### Data loading flow (CLI)

```mermaid
sequenceDiagram
    participant ADMIN as Admin
    participant CLI as cheesemonger CLI
    participant DISK as Hyperdisk
    participant SRC as Source xarray Zarr<br/>(GCS / local)

    ADMIN->>CLI: cheesemonger load<br/>--dataset pesca<br/>--block MCF7<br/>--source gs://lab-results/.../

    CLI->>DISK: Read schema.json
    DISK-->>CLI: Schema (dimensions, datatypes, chunk shape)

    CLI->>SRC: ds = xr.open_zarr(source)
    SRC-->>CLI: xarray Dataset<br/>(data vars + coordinates)

    CLI->>CLI: Validate ds against schema<br/>(dimensions, coords, data variables)

    CLI->>DISK: ds.to_zarr(/mnt/data/pesca/blocks/MCF7/)
    Note right of DISK: Writes data variables,<br/>coordinate arrays, and<br/>xarray metadata

    CLI-->>ADMIN: ✓ Block MCF7 loaded
```
