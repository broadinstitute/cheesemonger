# Cheesemonger REST API Design

**Draft Version:** v1
**Stack:** FastAPI + Pydantic + xarray, Zarr on Hyperdisk (big chunks)
**Storage model:** Separate-block (each block is an independent xarray Dataset stored as Zarr; block is not an array dimension)

---

## API Overview

Nine endpoints. All request and response bodies are JSON.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Service health check |
| `POST` | `/datasets` | Create a new dataset (define schema) |
| `GET` | `/datasets` | List all datasets |
| `GET` | `/datasets/{dataset}` | Get dataset metadata |
| `DELETE` | `/datasets/{dataset}` | Delete an empty dataset (all blocks must be deleted first) |
| `POST` | `/datasets/{dataset}/blocks` | Load a block from a server-readable Zarr source |
| `DELETE` | `/datasets/{dataset}/blocks/{block}` | Delete a block |
| `GET` | `/gene_mappings` | Get the gene mappings (entrez ↔ symbol) |
| `POST` | `/datasets/{dataset}/query` | Query data |

Block loading can be done two ways, both backed by the same loader: the CLI
(`python -m cheesemonger load`, for admins on the server) or the
`POST /datasets/{dataset}/blocks` endpoint (for remote clients, e.g. `cheesypy`).

> **Note:** there is currently no authentication (see `docs/planning.md`). On
> dev.cds.team the service sits behind oauth2_proxy (Broad OAuth), which gates
> all of these — including the ingest endpoint — at the network edge.

---

## Metadata Storage

Dataset and block metadata is stored in a **SQLite database** (via SQLAlchemy), not
in JSON files on disk. The database has two tables:

### ERD

```
┌─────────────────────────────────────────────────┐
│                   dataset                       │
├─────────────────────────────────────────────────┤
│ PK  id              STRING (UUID)               │
│     name            STRING  [UNIQUE, INDEX]     │
│     last_dimension  STRING                      │
│     dimensions      JSON                        │
│     datatypes       JSON                        │
│     chunk_shape     JSON                        │
│     created_at      DATETIME                    │
└──────────────────────┬──────────────────────────┘
                       │ 1 ─── * (one-to-many)
                       │ ON DELETE RESTRICT
┌──────────────────────┴──────────────────────────┐
│                    block                        │
├─────────────────────────────────────────────────┤
│ PK  id              STRING (UUID)               │
│ FK  dataset_id      STRING → dataset.id         │
│     name            STRING  [INDEX]             │
│     loaded_at       DATETIME                    │
├─────────────────────────────────────────────────┤
│ UNIQUE(dataset_id, name)                        │
└─────────────────────────────────────────────────┘
```

**`dataset` table** — one row per dataset (e.g. `pesca`). Stores the schema:
which dimensions exist, their coordinate labels, and which datatypes are
available. `dimensions`, `datatypes`, and `chunk_shape` are JSON columns because
they are complex nested structures (a dimension can have 50,000+ labels) that
are always read and written as a unit.

**`block` table** — one row per loaded block (e.g. `SW620`), foreign-keyed to
its parent dataset. The actual multi-dimensional data lives on disk as Zarr
stores; the database only records which blocks exist and when they were loaded.

**Key constraints:**

- `dataset.name` is `UNIQUE` — no two datasets can share a name.
- `UNIQUE(dataset_id, name)` on `block` — no duplicate block names within a
  dataset, but the same block name can exist in different datasets.
- `FOREIGN KEY dataset_id ... ON DELETE RESTRICT` — the database refuses to
  delete a dataset that still has blocks. This is the safety net behind the
  app-layer 409 check (all blocks must be deleted before deleting a dataset).

**Why SQLite?** Compared to the previous `schema.json` files:

- Transactional writes — dataset creation and block registration are atomic.
- Foreign key enforcement — the DB prevents orphaned blocks and enforces the
  "delete blocks first" rule at the database level.
- Single queryable source of truth — no scattered JSON files to scan.
- Standard tooling — `sqlite3` CLI for debugging, backups are a file copy.

**Scale:** The dataset table will have very few rows (1–10 datasets). The block
table will have tens to low hundreds of rows (one per screen per dataset). This
is a metadata registry, not a transactional database — the heavy data (43.2
billion measurements) lives in Zarr stores on disk.

**Default location:** `sqlite:///./cheesemonger.db` (configurable via
`SQLALCHEMY_DATABASE_URL` environment variable).

---

## Types

Shared type definitions used across endpoints.

### Schema types

```python
class Dimension(BaseModel):
    name: str                              # e.g. "timepoint"
    labels: list[int] | list[str]          # e.g. [4, 7] or ["103", "226", ...]

class DatatypeSpec(BaseModel):
    name: str                              # e.g. "ZScore"
    dimensions: list[str]                  # which dimensions this array spans
    dtype: str = "float32"

class ChunkDim(BaseModel):
    name: str                              # dimension name, e.g. "testedperturbation"
    size: int                              # chunk size along this dimension, e.g. 5000
```

### Query types

```python
class Selection(BaseModel):
    dimension: str                         # dimension name, e.g. "screen" or "timepoint"
    value: int | str                       # coordinate label to fix

class AggregateSpec(BaseModel):
    type: Literal["mean", "count_lt"]
    over: str                              # dimension to aggregate across
    threshold: float | None = None         # required for count_lt
```

A `Selection` fixes a dimension to a single coordinate label, removing it from the result. Omitting a dimension from `select` means "all values along that dimension."

### Response types

```python
class IndexLevel(BaseModel):
    dimension: str                         # e.g. "testedgeneexpression"
    labels: list[int | str]                # e.g. ["29974", "127550", ...] (raw labels)
```

---

## Endpoints

### `GET /health`

**Response** `200 OK`

```json
{
  "status": "ok"
}
```

---

### `POST /datasets`

Create a new dataset by defining its schema. This must be called before any blocks can be loaded. The schema declares the dimensions, their coordinate labels, the datatypes that will be stored, and the chunk shape for storage.

**Request body**

```json
{
  "name": "pesca",
  "last_dimension": "screen",
  "dimensions": [
    {"name": "timepoint", "labels": [4, 7]},
    {"name": "testedperturbation", "labels": ["103", "226", "672", "...", "100204"]},
    {"name": "testedgeneexpression", "labels": ["103", "226", "672", "...", "100204"]}
  ],
  "datatypes": [
    {"name": "ZScore", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "L2FC", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "FDR", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "neg_log10_FDR", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "MeanDifference", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "DetrendedMeanDifference", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "STD", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "PermutationP", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
    {"name": "nNonzeroTestCells", "dimensions": ["timepoint", "testedperturbation"]},
    {"name": "TestMean", "dimensions": ["timepoint", "testedperturbation"]},
    {"name": "nTestCells", "dimensions": ["timepoint", "testedperturbation"]},
    {"name": "nPermutations", "dimensions": ["timepoint", "testedperturbation"]},
    {"name": "nNonzeroCtrlCells", "dimensions": ["timepoint"]},
    {"name": "CtrlMean", "dimensions": ["timepoint"]},
    {"name": "nCtrlCells", "dimensions": ["timepoint"]}
  ],
  "chunk_shape": [
    {"name": "testedperturbation", "size": 1000},
    {"name": "testedgeneexpression", "size": 5000}
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Dataset name. E.g. `pesca`. |
| `last_dimension` | string | yes | The name of the organizational key (e.g. `"screen"`). Stored as folders on disk, not as an array axis. Not listed in `dimensions`. In queries, it behaves like any other dimension. |
| `dimensions` | list[Dimension] | yes | Each key is a dimension name, value contains labels (the coordinate array). |
| `datatypes` | list[DatatypeSpec] | yes | Each entry has a name and dimensions (which dims this array spans). This matters because not all datatypes have the same shape. `dtype` defaults to `float32`. |
| `chunk_shape` | list[ChunkDim] | no | Chunk size per dimension. Omitted dimensions default to the full extent. Default: big chunks `(1000, 5000)`. |

Notes:

- `last_dimension` names the organizational key. It does not appear in `dimensions` or in any datatype's dimension list because it's a folder on disk, not an array axis. Its values are created via the CLI `load` command.
- In queries, `last_dimension` is treated like any other dimension: it can appear in `select`, be used as `aggregate.over`, etc. The server internally routes it to folder selection instead of array indexing.
- `chunk_shape` only needs entries for dimensions that should be chunked. Dimensions like `timepoint` (size 2) are small enough to fit in a single chunk.
- The schema is immutable after creation. To change dimensions or datatypes, delete and re-create the dataset.

**Response** `201 Created`

```json
{
  "name": "pesca",
  "last_dimension": "screen",
  "dimensions": 3,
  "datatypes": 15,
  "chunk_shape": [
    {"name": "testedperturbation", "size": 1000},
    {"name": "testedgeneexpression", "size": 5000}
  ]
}
```

**Error cases**

| Status | Condition |
|--------|-----------|
| `409 Conflict` | A dataset with this name already exists |
| `400 Bad Request` | Missing required fields, empty labels, or datatype references unknown dimension |

---

### `GET /datasets`

List all datasets available on this server.

**Response** `200 OK`

```json
{
  "datasets": [
    {
      "name": "pesca",
      "blocks": 30,
      "datatypes": 15
    }
  ]
}
```

---

### `GET /datasets/{dataset}`

Full metadata for a dataset. Returns the same structure as `POST /datasets` plus runtime fields: loaded blocks and dimension sizes.

**Response** `200 OK`

```json
{
  "name": "pesca",
  "last_dimension": "screen",
  "dimensions": [
    {"name": "timepoint", "size": 2, "labels": [4, 7]},
    {
      "name": "testedperturbation",
      "size": 10000,
      "labels_truncated": true,
      "labels_sample": ["103", "226", "672", "...", "100204"]
    },
    {
      "name": "testedgeneexpression",
      "size": 18000,
      "labels_truncated": true,
      "labels_sample": ["103", "226", "672", "...", "100204"]
    }
  ],
  "blocks": [
    {"name": "SW620", "loaded_at": "2026-05-27T19:30:00Z"},
    {"name": "HT29", "loaded_at": "2026-05-28T10:15:00Z"},
    {"name": "A549", "loaded_at": "2026-05-29T14:00:00Z"}
  ],
  "datatypes": [
    {"name": "ZScore", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"], "dtype": "float32"},
    {"name": "L2FC", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"], "dtype": "float32"},
    {"name": "TestMean", "dimensions": ["timepoint", "testedperturbation"], "dtype": "float32"},
    {"name": "nCtrlCells", "dimensions": ["timepoint"], "dtype": "float32"}
  ],
  "chunk_shape": [
    {"name": "testedperturbation", "size": 1000},
    {"name": "testedgeneexpression", "size": 5000}
  ]
}
```

Notes:

- `labels_truncated` is set when a dimension has more than 100 labels. The full list is omitted for response size.
- `datatypes` shows the logical dimensionality of each array. Some datatypes (e.g. `nCtrlCells`) have fewer dimensions than others.

---

### `DELETE /datasets/{dataset}`

Delete a dataset and its schema. All blocks must be deleted first, the dataset must be empty.

**Response** `200 OK`

```json
{
  "dataset": "pesca",
  "deleted": true
}
```

**Error cases**

| Status | Condition |
|--------|-----------|
| `404 Not Found` | Dataset doesn't exist |
| `409 Conflict` | Dataset still has blocks. Delete all blocks first. |

`409` response body:

```json
{
  "error": "dataset_not_empty",
  "message": "Dataset 'pesca' still has 30 block(s). Delete all blocks before deleting the dataset.",
  "blocks": ["SW620", "HT29", "A549", "..."]
}
```

---

### `POST /datasets/{dataset}/blocks`

Load a block from a Zarr source the **server** can read. Data is not uploaded
through the request — the body names a location (a `gs://` URL the server has
credentials for, or a path on the server's filesystem). Runs the same loader as
the CLI. Synchronous for v1 (the handler runs in a worker thread; very large
sources should move to a background job — see `docs/planning.md`).

**Request body**

```json
{
  "source": "gs://cds_perturbseq_datasets/perturb-scuba/PS-SC-1_degs_broadcast.zarr",
  "block": "PS-SC-1",
  "create_dataset": true,
  "last_dimension": "screen",
  "overwrite": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Server-readable Zarr store (`gs://…` or server path) |
| `block` | string | yes | Block name (one value of the last dimension, e.g. a screen ID) |
| `create_dataset` | bool | no | Infer + create the dataset schema if it doesn't exist (default `false`) |
| `last_dimension` | string | no | Block-key name when creating (default `"screen"`) |
| `overwrite` | bool | no | Replace the block if it already exists (default `false`) |

**Response** `201 Created`

```json
{
  "dataset": "perturb-scuba",
  "block": "PS-SC-1",
  "path": "/mnt/data/perturb-scuba/blocks/PS-SC-1",
  "dimensions": {"Timepoint": 2, "Target": 2, "Response": 14588},
  "datatypes": ["ZScore", "L2FC", "FDR", "..."]
}
```

**Error cases**

| Status | Condition |
|--------|-----------|
| `422 Unprocessable Entity` | Source unreadable, dataset missing without `create_dataset`, block exists without `overwrite`, or source not declared in the schema |

> The current query engine expects the **broadcasted** store form (every
> datatype spans all dimensions). Unbroadcasted stores load but aren't fully
> queryable yet (see `docs/planning.md`).

---

### `DELETE /datasets/{dataset}/blocks/{block}`

Remove a block and all its data from the dataset.

**Response** `200 OK`

```json
{
  "block": "MCF7",
  "deleted": true
}
```

**Error cases**

| Status | Condition |
|--------|-----------|
| `404 Not Found` | Block does not exist |

---

### `GET /gene_mappings`

Retrieve the gene mapping. It is sourced from Taiga using `taigapy` and loaded at server startup. The client uses this to translate between entrez IDs and gene symbols locally. The mapping is not used by the query API; translation is the client's responsibility.

**Response** `200 OK`

```json
{
  "name": "gene_mappings",
  "taiga_id": "internal-26q1-82aa.94/Gene",
  "entries_count": 20000,
  "entries": {
    "7157": "TP53",
    "4193": "MDM2",
    "3845": "KRAS",
    "672": "BRCA1",
    "...": "..."
  }
}
```

The client typically fetches this once, caches it locally, and uses it to:
1. Translate gene symbols → entrez IDs before making query requests
2. Translate entrez IDs → gene symbols in query responses

**Error cases**

| Status | Condition |
|--------|-----------|
| `404 Not Found` | No gene mapping has been loaded (server misconfigured or Taiga ID not set) |

---

### `POST /datasets/{dataset}/query`

The primary read endpoint. This is the most important, most complex, and most frequently called endpoint in the API.

**Note on labels:** The query API operates on raw labels (entrez IDs). The client is responsible for translating to/from human-readable labels using the mappings endpoint. All examples below use entrez IDs in requests and responses. The natural language descriptions reference gene symbols for readability.

#### What this endpoint does

Every query follows the same pipeline:

```
Request JSON
  → Validate fields against dataset schema (Pydantic will do it)
  → Identify last_dimension selections → determine which block store(s) to open
  → Identify array-dimension selections → determine which slices to read
  → For each block (in parallel if multiple):
      → Open Zarr store at data -> pesca -> blocks -> {block}/
      → Select the datatype array (e.g. ZScore)
      → Apply selections (.sel(timepoint=4, testedperturbation="4193"))
      → Apply aggregation or diagonal if requested
      → Materialize result into numpy array
  → If multi-block: combine per-block results (stack or aggregate)
  → Serialize to JSON response
```

Aggregation always happens **after** collecting raw values from all relevant blocks. This avoids the `mean(mean(X_i)) ≠ mean(X)` problem — raw data is gathered first, then aggregated once.

#### Supported query patterns

1. **Series** — fix all dimensions except one, return a vector
2. **Aggregation (mean)** — fix some dimensions, compute mean over another
3. **Aggregation (count_lt)** — fix some dimensions, count values below a threshold
4. **Diagonal** — extract values where two dimensions share coordinate labels
5. **Multi-datatype batch** — query several datatypes with the same selections
6. **Multi-block scalar** — same scalar query across blocks, aggregated
7. **Multi-block + multi-datatype** — combine both
8. **Cross-block vector aggregation** — aggregate a vector over blocks
9. **Multi-block with within-block aggregation** — aggregate within each block, return per-block

Patterns 1-5 are single-block queries. Patterns 6-9 are cross-block queries (N blocks queried in parallel, not yet benchmarked).

---

#### General request body

```json
{
  "datatype": "ZScore",
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4},
    {"dimension": "testedperturbation", "value": "4193"}
  ],
  "aggregate": null,
  "diagonal": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `datatype` | string or [string] | yes | Datatype(s) to read. String for one, list for batch. |
| `select` | list[Selection] | yes | Dimensions to fix. Includes the last dimension (e.g. `"screen"`). Omitting a dimension means "all values." |
| `aggregate` | AggregateSpec or null | no | Aggregation specification (see below) |
| `diagonal` | [string, string] or null | no | Two dimension names for diagonal extraction |

**How `select` works with the last dimension:**

The last dimension (e.g. `"screen"`) is treated like any other dimension in `select`:

| Selection | Meaning |
|-----------|---------|
| `{"dimension": "screen", "value": "SW620"}` | Query one block (fastest) |
| Screen omitted from `select` | Query every loaded block |

Under the hood, the server recognizes the last dimension and resolves it to folder selection rather than array indexing. But from the client's perspective, it's just another dimension.

Aggregate object (when not null):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `"mean"` or `"count_lt"` |
| `over` | string | yes | Dimension to aggregate across. Can be `"screen"` for cross-block aggregation. |
| `threshold` | number | for `count_lt` | Threshold value for counting. |

```json
{
  "aggregate": {
    "type": "mean",
    "over": "testedperturbation",
    "threshold": null
  }
}
```

---

#### Response fields

| Field | Type | Description |
|-------|------|-------------|
| `blocks` | [string] | Blocks that were queried (always present) |
| `aggregation` | string or null | Present when aggregation was applied over the last dimension |
| `shape` | [int] | Shape of the result array (one entry per `index` level) |
| `index` | list[IndexLevel] | The free (unconstrained, non-aggregated) dimensions and their labels. Analogous to a Pandas Index/MultiIndex. |
| `data` | object | Dict keyed by datatype name. Values are N-dimensional arrays where N = `len(index)`. |

**`data` format:**

- `data` is always a dict keyed by datatype name.
- Each value is an N-dimensional array matching the `index`:
  - 0 free dimensions → scalar (e.g. `-1.54`)
  - 1 free dimension → 1D array (e.g. `[0.48, -1.2, ...]`)
  - 2 free dimensions → 2D array (e.g. `[[0.48, ...], [0.39, ...]]`)

When multiple blocks are queried without cross-block aggregation, the last dimension appears in `index` as a regular dimension. The result is shaped accordingly (one extra dimension for blocks).

Notes:

- `null` and `NaN` values are serialized as JSON `null`.
- All labels in `index` are raw storage labels (e.g. entrez IDs). The client translates to human-readable labels using the mappings endpoint.

---

#### Case 1: Series query (single block, single datatype)

"Give me the ZScore for SW620 at day 4, perturbation MDM2."

Fixes block, timepoint, and perturbation; returns the gene expression vector.

**Request:**

```json
{
  "datatype": "ZScore",
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4},
    {"dimension": "testedperturbation", "value": "4193"}
  ]
}
```

**Response:**

```json
{
  "blocks": ["SW620"],
  "shape": [18000],
  "index": [{"dimension": "testedgeneexpression", "labels": ["29974", "127550", "...", "7525"]}],
  "data": {"ZScore": [0.482, -1.205, 0.017, "..."]}
}
```

One free dimension (`testedgeneexpression`), one flat array of 18,000 entrez IDs.
Expected latency: **~40 ms**.

---

#### Case 2: Aggregation — mean (single block)

"Average ZScore across all perturbations for SW620, day 4."

**Request:**

```json
{
  "datatype": "ZScore",
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4}
  ],
  "aggregate": {"type": "mean", "over": "testedperturbation"}
}
```

**Response:**

```json
{
  "blocks": ["SW620"],
  "shape": [18000],
  "index": [{"dimension": "testedgeneexpression", "labels": ["29974", "127550", "...", "7525"]}],
  "data": {"ZScore": [0.012, -0.034, 0.008, "..."]}
}
```

The server reads all 40 chunks (10 perturbation bands x 4 gene expression bands), computes the mean over perturbation, and returns one value per gene.
Expected latency: **~450 ms**.

---

#### Case 3: Aggregation — count_lt (single block)

"How many genes have FDR < 0.1 for each perturbation in SW620?"

**Request:**

```json
{
  "datatype": "FDR",
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4}
  ],
  "aggregate": {"type": "count_lt", "over": "testedgeneexpression", "threshold": 0.1}
}
```

**Response:**

```json
{
  "blocks": ["SW620"],
  "shape": [10000],
  "index": [{"dimension": "testedperturbation", "labels": ["103", "226", "...", "128611"]}],
  "data": {"FDR": [342, 17, 891, "..."]}
}
```

For each perturbation, counts how many of the 18,000 genes have FDR below 0.1.
Expected latency: **~450 ms**.

---

#### Case 4: Diagonal query (single block)

"Self-targeting: when Gene X was knocked out, how did Gene X's own expression change?"

**Request:**

```json
{
  "datatype": "L2FC",
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4}
  ],
  "diagonal": ["testedperturbation", "testedgeneexpression"]
}
```

**Response:**

```json
{
  "blocks": ["SW620"],
  "shape": [8500],
  "index": [{"dimension": "label", "labels": ["7157", "672", "3845", "..."]}],
  "data": {"L2FC": [-2.31, 0.05, -1.87, "..."]}
}
```

Finds coordinate labels common to both dimensions (entrez IDs that appear in both `testedperturbation` and `testedgeneexpression`) and extracts `L2FC[perturbation=X, expression=X]` for each.
Expected latency: **~40 ms**.

---

#### Case 5: Multi-datatype batch (single block)

"Give me L2FC, neg_log10_FDR, and FDR for perturbation KRAS to build a volcano plot."

**Request:**

```json
{
  "datatype": ["L2FC", "neg_log10_FDR", "FDR"],
  "select": [
    {"dimension": "screen", "value": "SW620"},
    {"dimension": "timepoint", "value": 4},
    {"dimension": "testedperturbation", "value": "3845"}
  ]
}
```

**Response:**

```json
{
  "blocks": ["SW620"],
  "shape": [18000],
  "index": [{"dimension": "testedgeneexpression", "labels": ["29974", "127550", "...", "7525"]}],
  "data": {
    "L2FC": [0.482, -1.205, 0.017, "..."],
    "neg_log10_FDR": [3.21, 0.54, 1.08, "..."],
    "FDR": [0.0006, 0.29, 0.083, "..."]
  }
}
```

All three datatypes share the same index. The server reads them in parallel using threads.
Expected latency: **~40-80 ms**.

---

#### Case 6: Cross-block scalar aggregation (not yet benchmarked)

"Get the TP53 response ZScore for MDM2 knockouts at day 7 across screens."

All array dimensions are fixed, so each block produces a scalar. The last dimension (`screen`) is omitted from `select` → all blocks are queried. Aggregation over `screen` collapses them to one number.

**Request:**

```json
{
  "datatype": "ZScore",
  "select": [
    {"dimension": "timepoint", "value": 7},
    {"dimension": "testedperturbation", "value": "4193"},
    {"dimension": "testedgeneexpression", "value": "7157"}
  ],
  "aggregate": {"type": "mean", "over": "screen"}
}
```

**Response:**

```json
{
  "blocks": ["SW620", "HT29", "A549", "...", "K562"],
  "aggregation": "mean",
  "shape": [],
  "index": [],
  "data": {"ZScore": -1.54}
}
```

No free dimensions → scalar result. `blocks` lists the 30 screens that contributed.

---

#### Case 7: Cross-block + multi-datatype aggregation (not yet benchmarked)

"Get the TP53 response ZScore, L2FC, and FDR for MDM2 knockouts at day 7, averaged across screens."

**Request:**

```json
{
  "datatype": ["ZScore", "L2FC", "FDR"],
  "select": [
    {"dimension": "timepoint", "value": 7},
    {"dimension": "testedperturbation", "value": "4193"},
    {"dimension": "testedgeneexpression", "value": "7157"}
  ],
  "aggregate": {"type": "mean", "over": "screen"}
}
```

**Response:**

```json
{
  "blocks": ["SW620", "HT29", "A549", "...", "K562"],
  "aggregation": "mean",
  "shape": [],
  "index": [],
  "data": {
    "ZScore": -1.54,
    "L2FC": -2.31,
    "FDR": 0.0004
  }
}
```

Three datatypes, 30 blocks, all collapsed to three scalars.

---

#### Case 8: Cross-block vector aggregation (not yet benchmarked)

"Get the average (across screens) response ZScore to AP2M1 knockouts at day 4."

Only timepoint and perturbation are fixed; `testedgeneexpression` is free, so each block produces an 18,000-element vector. Aggregating over `screen` averages the vectors into one.

**Request:**

```json
{
  "datatype": "ZScore",
  "select": [
    {"dimension": "timepoint", "value": 4},
    {"dimension": "testedperturbation", "value": "1173"}
  ],
  "aggregate": {"type": "mean", "over": "screen"}
}
```

**Response:**

```json
{
  "blocks": ["SW620", "HT29", "A549", "...", "K562"],
  "aggregation": "mean",
  "shape": [18000],
  "index": [{"dimension": "testedgeneexpression", "labels": ["29974", "127550", "...", "7525"]}],
  "data": {"ZScore": [0.443, -1.098, 0.041, "..."]}
}
```

The server collects all 30 raw vectors into a `(30, 18000)` array, then computes `.mean(axis=0)` once.

---

#### Case 9: Per-block within-block aggregation (client-side, not yet benchmarked)

"Get the average (within screen) response ZScore of LDHA, ALDOA, PGK1 after TFRC knockout at day 7 across screens."

Within each screen: fix timepoint=7, perturbation=TFRC, get the ZScore for those 3 specific response genes, average them → one scalar per screen. Assemble into a series indexed by screen.

Since the API only supports single-value selections, this requires **3 separate queries** (one per gene) and client-side averaging.

**Requests (3 queries):**

Each query fixes timepoint, perturbation, and one response gene. Screen is omitted → all blocks queried.

```json
{"datatype": "ZScore", "select": [{"dimension": "timepoint", "value": 7}, {"dimension": "testedperturbation", "value": "7037"}, {"dimension": "testedgeneexpression", "value": "3939"}]}
{"datatype": "ZScore", "select": [{"dimension": "timepoint", "value": 7}, {"dimension": "testedperturbation", "value": "7037"}, {"dimension": "testedgeneexpression", "value": "226"}]}
{"datatype": "ZScore", "select": [{"dimension": "timepoint", "value": 7}, {"dimension": "testedperturbation", "value": "7037"}, {"dimension": "testedgeneexpression", "value": "5230"}]}
```

Each response returns one scalar per block (all array dimensions are fixed, only `screen` is free):

```json
{
  "blocks": ["SW620", "HT29", "A549", "...", "K562"],
  "shape": [30],
  "index": [{"dimension": "screen", "labels": ["SW620", "HT29", "A549", "...", "K562"]}],
  "data": {"ZScore": [0.42, 0.31, 0.55, "...", 0.19]}
}
```

The client collects the 3 per-gene vectors (each length 30, one scalar per screen) and averages them element-wise to get one mean ZScore per screen. The final result the user sees:

```
Screen    Avg ZScore (LDHA, ALDOA, PGK1)
SW620     0.34
HT29      0.29
A549      0.41
...       ...
K562      0.18
```

Or equivalently, as a DataFrame:

```python
import pandas as pd

responses = [query_1_data, query_2_data, query_3_data]  # each is [30] floats
screens = responses[0]["index"][0]["labels"]
avg = [sum(r["data"]["ZScore"][i] for r in responses) / 3 for i in range(len(screens))]
result = pd.Series(avg, index=screens, name="mean_ZScore_LDHA_ALDOA_PGK1")
```

These 3 queries can be issued in parallel by the client.
Estimated latency: **~300 ms** (dominated by the single slowest query, since all 3 run concurrently).

---

#### Error cases

| Status | Condition |
|--------|-----------|
| `400 Bad Request` | Unknown datatype, unknown dimension in `select` |
| `404 Not Found` | Dataset or block not found |
| `422 Unprocessable Entity` | Invalid selection values (e.g., label doesn't exist), `count_lt` without `threshold`, `aggregate.over` references a fully-fixed dimension, malformed request body |

> **Note:** FastAPI/Pydantic return `422 Unprocessable Entity` (not `400`) for request body validation errors (missing required fields, wrong types). This is standard FastAPI behavior.

---

## Future considerations

- Pre-computed aggregations and caching for aggregated queries.
- Vectorized diagonal extraction for better latency (currently loops per label).
- Query engine integration tests against real xarray-exported Zarr stores.
- Router-level label validation before calling `.sel()` for cleaner error messages.
