# Cheesemonger REST API Design

**Draft Version:** v1
**Stack:** FastAPI + Pydantic + xarray, Zarr on Hyperdisk (big chunks)
**Storage model:** Separate-block (each block is an independent xarray Dataset stored as Zarr; block is not an array dimension)

---

## API Overview

Seven endpoints, all **read-only**. All request and response bodies are JSON.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Service health check |
| `GET` | `/datasets` | List all datasets |
| `GET` | `/datasets/{dataset}` | Get dataset metadata |
| `GET` | `/datasets/{dataset}/dimensions/{dim}` | Full (pageable) label list for one dimension |
| `GET` | `/gene_mappings` | Get the gene mappings (entrez ↔ symbol) |
| `POST` | `/datasets/{dataset}/query` | Query data as a dense grid (read; `POST` only to carry a JSON body) |
| `POST` | `/datasets/{dataset}/filter` | Return the cells of a datatype that pass a predicate, as long-form rows (read) |

All paths are mounted under the optional `API_PREFIX` setting (default empty).

**Mutations are not exposed over HTTP.** Creating a dataset, loading a block,
deleting a block, and deleting a dataset all happen through the CLI loader on the
server — see [Data mutations (CLI)](#data-mutations-cli) below. This keeps the
service surface read-only: a client can never alter stored data.

> **Note:** there is currently no authentication (see
> [project_status.md](project_status.md)). On dev.cds.team the service sits
> behind oauth2_proxy (Broad OAuth), which gates all of these at the network edge.

### Dimension names in the examples

The examples in this doc use **illustrative** names from the PESCA test fixtures:
array dimensions `timepoint`, `testedperturbation`, `testedgeneexpression`, and a
block key called `screen`. Deployed datasets use `Timepoint`, `Target`,
`Response`, and a block key that defaults to `Screen`. Names are
**case-sensitive** and come from the dataset itself: read
`GET /datasets/{dataset}` and use its `dimensions[*].name` and its
`last_dimension` (the block key name, set per dataset at load time with
`--last-dimension`).

### Gene symbols

Labels on the wire are always raw storage labels, so gene coordinates are
**entrez IDs** (strings such as `"7157"`), plus non-gene tokens like `Cas9`. The
server never translates them. Translation to gene symbols happens client-side,
using `GET /gene_mappings`. The cheesypy client translates only the dimensions in
its `GENE_DIMENSIONS` set and the string datatypes in `GENE_DATATYPES` (e.g.
`CorrelateResponse`), and leaves everything else (`Rank`, `Timepoint`, …) alone.
See [gene_symbol_relabel_issue.md](gene_symbol_relabel_issue.md).

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

**Why SQLite?** Compared to the per-dataset `schema.json` files of an earlier
design (no longer used; nothing on disk carries schema metadata now):

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

### Gene universe & present-union labels

> **Status: IMPLEMENTED** (load path, single-block query, and multi-block union +
> NaN-fill alignment). Full design:
> [data_model.md §11](data_model.md#11-gene-universe--present-union-labels).

To fix a class of bugs where screens carry different gene sets than the frozen
dataset labels:

- **`dataset`** gained a **`gene_universe`** JSON column (`{gene_dim: [labels]}`) — the
  validation superset, the entrez IDs from a pinned Taiga HGNC table
  (`hgnc-gene-table-e250.4`) plus the non-entrez token `Cas9`. At each load a
  block's labels along a universe dim must be a subset, else the load fails.
- The dataset's **`dimensions[].labels` is now the present union** of loaded
  blocks (maintained by the CLI: fold-in on load, recompute on `delete-block`),
  not a list frozen from the first block. `GET /datasets/{dataset}/dimensions/{dim}`
  therefore returns "what exists" — never the universe.
- Per-block labels are **not** persisted; the present union is recomputed from the
  blocks' Zarr coordinates on removal, and a `reconcile` CLI command rebuilds it.
- **All coordinate labels are strings** (entrez IDs like `"9992"`, plus tokens like
  `Cas9`).

The query `index` is built from **each block's own Zarr coordinates** (captured
at read time), so a single-block query's index length always matches its data —
fixing the "N columns passed, passed data had M columns" crash. Cross-block
queries over screens with *different* label sets are **aligned to the union of
labels with NaN-fill**: a gene a screen lacks reads as `null`, and NaN-aware
aggregations (mean/median/…) ignore the fills.

---

## Types

Shared type definitions used across endpoints.

### Schema types

```python
class Dimension(BaseModel):
    name: str                              # e.g. "timepoint"
    labels: list[str]                      # e.g. ["4", "7"] or ["103", "226", ...]

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
    value: str | list[str]                 # scalar: fix + collapse; list: select subset, keep dim
    # Integers are accepted on the wire and coerced to str (coordinate labels
    # are always strings). A list is valid on ANY dimension: it keeps the
    # dimension in the result, in the order given. On the block key it selects
    # that subset of blocks.

class AggregateSpec(BaseModel):
    type: Literal["mean", "median", "min", "max",
                  "count", "count_lt", "count_gt", "abs_gt"]
    over: str                              # dimension to aggregate across
    threshold: float | None = None         # required for count_lt/count_gt/abs_gt
    fill_value: float | None = None        # replace NaN before reducing; None = NaN-ignoring
```

Aggregation kinds:

| Type | Reduces the `over` axis to | Needs `threshold` |
|------|----------------------------|-------------------|
| `mean` | mean (NaN-ignoring) | no |
| `median` | median (NaN-ignoring) | no |
| `min` / `max` | min / max (NaN-ignoring) | no |
| `count` | number of non-NaN values | no |
| `count_lt` | count of values `< threshold` | **yes** |
| `count_gt` | count of values `> threshold` | **yes** |
| `abs_gt` | count of values with `abs(value) > threshold` | **yes** |

All operate the same whether `over` is a within-block dimension or the last
dimension (cross-block): raw values are gathered first, then reduced once.

A `Selection` with a **scalar** `value` fixes a dimension to one coordinate label,
removing it from the result. A **list** `value` restricts the dimension to those
labels, keeps it in the result, and orders it as given (like pandas fancy
indexing). Omitting a dimension from `select` means "all values along that
dimension."

Selection labels are validated against the dataset's present-union labels: a
label that **no** block carries is a `422`. A label that exists in the dataset but
is missing from a particular block reads as `null` (NaN) for that block instead of
failing the request.

### Response types

```python
class IndexLevel(BaseModel):
    dimension: str                         # e.g. "testedgeneexpression"
    labels: list[str]                      # e.g. ["29974", "127550", ...] (raw labels)
```

---

## Endpoints

### `GET /health`

Liveness plus a readiness signal for the gene mapping. `status` is `"ok"` as long
as the process can answer. The `gene_mapping` block reports whether the Taiga
mapping loaded at startup (it loads lazily and degrades silently if Taiga fails):

- `loaded: false, taiga_id: ""` — no mapping configured.
- `loaded: false, taiga_id: "…"` — configured but **failed to load** (check logs / token).
- `loaded: true` — mapping is serving `entries` genes.

**Response** `200 OK`

```json
{
  "status": "ok",
  "gene_mapping": {
    "loaded": true,
    "entries": 43849,
    "taiga_id": "hgnc-gene-table-e250.4/hgnc_complete_set"
  }
}
```

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

Full metadata for a dataset: its schema (dimensions, datatypes, chunk shape) plus runtime fields — loaded blocks and dimension sizes.

**Response** `200 OK`

```json
{
  "name": "pesca",
  "last_dimension": "screen",
  "dimensions": [
    {"name": "timepoint", "size": 2, "labels": ["4", "7"]},
    {
      "name": "testedperturbation",
      "size": 10000,
      "labels_truncated": true,
      "labels_sample": ["103", "226", "672", "1017", "1173"]
    },
    {
      "name": "testedgeneexpression",
      "size": 18000,
      "labels_truncated": true,
      "labels_sample": ["103", "226", "672", "1017", "1173"]
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

- `labels_truncated` is set when a dimension has more than 100 labels. The full list is omitted for response size and `labels_sample` holds the first 5 labels; use `GET /datasets/{dataset}/dimensions/{dim}` for the full list.
- All labels are strings. `last_dimension` is the block key's name for this dataset (here `screen`; deployed datasets default to `Screen`).
- `blocks` is sorted by name.
- `404 Not Found` if the dataset does not exist.
- `datatypes` shows the logical dimensionality of each array. Some datatypes (e.g. `nCtrlCells`) have fewer dimensions than others.

---

### `GET /datasets/{dataset}/dimensions/{dim}`

Full coordinate labels for a single dimension, with paging — unlike
`GET /datasets/{dataset}`, which truncates large label lists to a sample. Pass the
`last_dimension` name (e.g. `screen`) to list the loaded blocks instead.

**Query parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `offset` | int ≥ 0 | `0` | paging start index |
| `limit` | int > 0 | *(none)* | max labels returned; omit for all |

**Response** `200 OK`

```json
{
  "name": "testedgeneexpression",
  "size": 18000,
  "labels": ["103", "226", "672", "..."]
}
```

- `size` is the **total** count (before paging); `labels` is the `offset`/`limit` slice.

**Error cases**

| Status | Condition |
|--------|-----------|
| `404 Not Found` | dataset does not exist, or `dim` is not a dimension of it |

> **Present union (implemented, see [Gene universe & present-union labels](#gene-universe--present-union-labels)):**
> this returns the dataset's `dimensions[*].labels`, which are now the **present
> union** — every label actually present across the dataset's loaded blocks, so it
> reflects *what exists*. It is **not** the (larger) validation universe. The union
> is maintained at load and can be rebuilt with the `reconcile` CLI command.

---

### Data mutations (CLI)

All writes — creating a dataset, loading a block, deleting a block, deleting a
dataset — are performed with the CLI loader on the server, not over HTTP. This is
deliberate: the HTTP surface is read-only, so no client can alter stored data.
The commands run on the machine that has the data volume mounted and (for
`gs://` sources) the storage credentials.

```bash
# Load a block. The first block into a new dataset also creates it, inferring
# the schema (dimensions, labels, datatypes) from the source store.
python -m cheesemonger load \
    --source gs://.../PS-SC-1_degs.zarr \
    --dataset perturb-scuba --block PS-SC-1 --create-dataset \
    --chunk Target=1 --chunk Timepoint=1

# Rerun a bulk load safely: blocks that are already loaded are skipped.
python -m cheesemonger load --source ... --dataset perturb-scuba --block PS-SC-2 --skip-existing

# Delete a single block (removes its DB row and its Zarr directory).
python -m cheesemonger delete-block --dataset perturb-scuba --block PS-SC-1

# Delete a dataset. Refuses if it still has blocks unless --force removes them first.
python -m cheesemonger delete-dataset --dataset perturb-scuba --force

# Inspect what is loaded, and rebuild a dataset's present-union labels from disk.
python -m cheesemonger status --dataset perturb-scuba
python -m cheesemonger reconcile --dataset perturb-scuba
```

| Command | Flags | Notes |
|---------|-------|-------|
| `load` | `--source` (local path or `gs://`), `--dataset`, `--block` (all required); `--data-dir`; `--last-dimension` (default `Screen`); `--create-dataset`; `--skip-existing`; `--chunk DIM=SIZE` (repeatable); `--gene-dim DIM` (repeatable); `--gene-universe-taiga-id`; `--gene-universe-manifest`; `--gene-universe-extra LABEL` (repeatable) | Loads one block. `--create-dataset` infers and creates the dataset if it doesn't exist; `--last-dimension` and `--chunk` apply only then (unlisted dims stay unchunked). Loading a block that is already loaded is an error, or a skip with `--skip-existing`. **There is no overwrite**: to replace a block, `delete-block` it first. Dots in the block name are normalized to hyphens (`PS-SC-000651.GG01` → `PS-SC-000651-GG01`). |
| `delete-block` | `--dataset`, `--block`, `--data-dir` | Errors if the dataset or block doesn't exist. Recomputes the present-union labels from the remaining blocks. |
| `delete-dataset` | `--dataset`, `--data-dir`, `--force` | Without `--force`, refuses when blocks remain; with it, deletes all blocks first |
| `status` | `--dataset` (optional; default all) | Prints loaded datasets, their blocks, dimensions, and datatypes |
| `reconcile` | `--dataset`, `--data-dir` | Rebuilds the dataset's present-union labels from the blocks on disk |

`--data-dir` defaults to `DATA_DIR` from settings/`.env`. All commands read and
write metadata in the SQLite DB at `SQLALCHEMY_DATABASE_URL`.

> **Gene universe (implemented).** `load` accepts an explicit gene **universe** on
> `--create-dataset`: `--gene-dim DIM` (repeatable) plus a source —
> `--gene-universe-taiga-id hgnc-gene-table-e250.4` (uses the `entrez_id` column) or
> `--gene-universe-manifest PATH` — plus `--gene-universe-extra Cas9`. Every subsequent
> `load` validates the block's labels along each gene dim are a subset of the
> stored universe. A `reconcile --dataset NAME` command rebuilds the present-union
> labels from the blocks on disk (the union's safety net). See
> [Gene universe & present-union labels](#gene-universe--present-union-labels).

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
  → Pydantic validates the body shape (422 on malformed input)
  → Router validates names against the dataset schema in SQLite
      (datatypes, select/aggregate dimensions, block names, rule combinations)
  → Estimate the result size from schema + selections; reject with 413 if too large
  → Validate selection labels against the dataset's present-union labels (422)
  → Identify last_dimension selection → determine which block store(s) to open
  → Identify array-dimension selections → determine which slices to read
  → For each block (in parallel if multiple):
      → Open Zarr store at {DATA_DIR}/{dataset}/blocks/{block}/
      → Select the datatype array (e.g. ZScore)
      → Apply selections (.sel(timepoint="4", testedperturbation="4193")),
        NaN-filling labels this block lacks
      → Apply within-block aggregation or diagonal if requested
      → Materialize result into numpy array
  → If multi-block: align blocks to the union of their labels (NaN-fill),
    then stack (block dim kept) or aggregate over the block key
  → Serialize to JSON response (NaN → null)
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
9. **Multi-block with within-block aggregation** — list-select a few labels across blocks in one query, reduce per block client-side

Patterns 1-5 are single-block queries. Patterns 6-9 are cross-block queries (N blocks queried in parallel, not yet benchmarked).

---

#### General request body

```json
{
  "datatypes": ["ZScore"],
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
| `datatypes` | [string] | yes | One or more datatypes to read at the same coordinates (at least one). Multiple datatypes must share dimensions; they come back under `data`, keyed by name, sharing one `index`. |
| `select` | list[Selection] | no (default `[]`) | Dimensions to fix or subset. May include the last dimension (block key, e.g. `"screen"`). Omitting a dimension means "all values." A scalar `value` fixes and collapses the dimension; a list `value` selects a named subset while keeping the dimension in the result, in the given order. Lists work on any dimension. Integer values are coerced to strings. A selection on a dim the datatype doesn't have (a reduced-rank datatype) is ignored for that datatype. |
| `aggregate` | AggregateSpec or null | no | Aggregation specification (see below) |
| `diagonal` | [string, string] or null | no | Two dimension names of the datatype for diagonal extraction. Cannot be combined with `aggregate`. |

**How `select` works with the last dimension:**

The last dimension (e.g. `"screen"`) is treated like any other dimension in `select`:

| Selection | Meaning |
|-----------|---------|
| `{"dimension": "screen", "value": "SW620"}` | Query one block (fastest); the block dim is collapsed |
| `{"dimension": "screen", "value": ["SW620", "HT29"]}` | Query that subset of blocks; the block dim is kept (in the given order), even for a one-element list |
| Screen omitted from `select` | Query every loaded block; the block dim is kept |

Every named block must exist (`404` otherwise), and an empty list is a `422`
(omit the key to span all blocks). Under the hood, the server recognizes the last
dimension and resolves it to folder selection rather than array indexing. But from
the client's perspective, it's just another dimension.

Aggregate object (when not null):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | One of `mean`, `median`, `min`, `max`, `count`, `count_lt`, `count_gt`, `abs_gt` (see the table above). |
| `over` | string | yes | Dimension to aggregate across. Must be a dimension of the datatype, or the block key (e.g. `"screen"`) for cross-block aggregation. It cannot be a dimension fixed in `select` (`422`), with one exception: the block key may be selected as a **list**, which aggregates over that subset of blocks. |
| `threshold` | number | for `count_lt`/`count_gt`/`abs_gt` | Threshold value for the count. |
| `fill_value` | number or null | no | If set, NaN cells are replaced with this constant before reducing. Default `null` preserves NaN-ignoring behavior (missing values are excluded). |

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
| `blocks` | [string] | Blocks that were queried, in query order (always present) |
| `aggregation` | string or null | The aggregation type when aggregation was applied over the last dimension; `null` otherwise (including within-block aggregation) |
| `shape` | [int] | Shape of the result array (one entry per `index` level) |
| `index` | list[IndexLevel] | The free (unconstrained, non-aggregated) dimensions and their labels. Analogous to a Pandas Index/MultiIndex. |
| `data` | object | Dict keyed by datatype name. Values are N-dimensional arrays where N = `len(index)`. |

**`data` format:**

- `data` is always a dict keyed by datatype name.
- Each value is an N-dimensional array matching the `index`:
  - 0 free dimensions → scalar (e.g. `-1.54`)
  - 1 free dimension → 1D array (e.g. `[0.48, -1.2, ...]`)
  - 2 free dimensions → 2D array (e.g. `[[0.48, ...], [0.39, ...]]`)

When the block key is omitted or selected as a list, and there is no cross-block aggregation, the last dimension appears in `index` as a regular dimension: it is the **first** index level, and the data gains a leading block axis. Blocks whose label sets differ are aligned to the union of their labels, and a label a block lacks reads as `null`.

Notes:

- `null` and `NaN` values are serialized as JSON `null`.
- Values keep their dtype: integer datatypes come back as integers, string datatypes (e.g. `CorrelateResponse`) as strings.
- Diagonal results have a single index level named `label`.
- All labels in `index` are raw storage labels (e.g. entrez IDs). The client translates to human-readable labels using the mappings endpoint.

---

#### Case 1: Series query (single block, single datatype)

"Give me the ZScore for SW620 at day 4, perturbation MDM2."

Fixes block, timepoint, and perturbation; returns the gene expression vector.

**Request:**

```json
{
  "datatypes": ["ZScore"],
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
  "datatypes": ["ZScore"],
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
  "datatypes": ["FDR"],
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
  "datatypes": ["L2FC"],
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
  "datatypes": ["L2FC", "neg_log10_FDR", "FDR"],
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
  "datatypes": ["ZScore"],
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
  "datatypes": ["ZScore", "L2FC", "FDR"],
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
  "datatypes": ["ZScore"],
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

#### Case 9: Per-block within-block aggregation (one query + client-side mean, not yet benchmarked)

"Get the average (within screen) response ZScore of LDHA, ALDOA, PGK1 after TFRC knockout at day 7 across screens."

Within each screen: fix timepoint=7, perturbation=TFRC, get the ZScore for those 3 specific response genes, average them → one scalar per screen. Assemble into a series indexed by screen.

A **list** selection on `testedgeneexpression` fetches all 3 genes for every screen in **one query**. The final mean over the 3 genes is done client-side: `aggregate.over` cannot name a dimension that is selected in `select` (scalar or list), so `{"type": "mean", "over": "testedgeneexpression"}` with a gene list is rejected with `422`. The only exception is a list on the block key. To restrict to some screens, add a block-key list, e.g. `{"dimension": "screen", "value": ["SW620", "HT29"]}`.

**Request (1 query):**

Fixes timepoint and perturbation and subsets the response gene to the 3 genes. Screen is omitted, so all blocks are queried.

```json
{
  "datatypes": ["ZScore"],
  "select": [
    {"dimension": "timepoint", "value": 7},
    {"dimension": "testedperturbation", "value": "7037"},
    {"dimension": "testedgeneexpression", "value": ["3939", "226", "5230"]}
  ]
}
```

The response is a `(screens × genes)` grid. `screen` is the first index level, and the gene dimension is kept in the requested order:

```json
{
  "blocks": ["SW620", "HT29", "A549", "...", "K562"],
  "shape": [30, 3],
  "index": [
    {"dimension": "screen", "labels": ["SW620", "HT29", "A549", "...", "K562"]},
    {"dimension": "testedgeneexpression", "labels": ["3939", "226", "5230"]}
  ],
  "data": {"ZScore": [[0.42, 0.25, 0.35], [0.31, 0.22, 0.34], "...", [0.19, 0.12, 0.23]]}
}
```

The client averages each row (axis 1) to get one mean ZScore per screen. The final result the user sees:

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
import numpy as np
import pandas as pd

screens = resp["index"][0]["labels"]
grid = np.array(resp["data"]["ZScore"], dtype=float)  # (30, 3); null -> nan
result = pd.Series(np.nanmean(grid, axis=1), index=screens,
                   name="mean_ZScore_LDHA_ALDOA_PGK1")
```

A gene a screen didn't measure comes back as `null`. `nanmean` ignores it, which
matches the server's own NaN-ignoring aggregations.

---

#### Error cases

| Status | Condition |
|--------|-----------|
| `400 Bad Request` | Unknown datatype; unknown dimension in `select` or `aggregate.over` |
| `404 Not Found` | Dataset not found; block(s) named in `select` not found (all missing names are listed) |
| `413 Content Too Large` | Estimated result size exceeds `MAX_RESULT_BYTES` (setting `max_result_bytes`, default `1_000_000_000` ≈ 1 GB). The size is estimated from the schema and selections before any data is read, at about 24 bytes per returned value. Narrow the query, aggregate, or fix more dimensions. |
| `422 Unprocessable Entity` | Selection label not found in any block of the dataset; `count_lt`/`count_gt`/`abs_gt` without `threshold`; `aggregate.over` is fixed by `select` (other than a block-key list) or is not a dimension of the datatype; `diagonal` combined with `aggregate`, or a `diagonal` dim that isn't a dimension of the datatype; mismatched datatype dimensions; empty list for the block key; a block read failure (`Failed reading block '…': …`); malformed request body (e.g. empty `datatypes`) |

> **Note:** FastAPI/Pydantic return `422 Unprocessable Entity` (not `400`) for request body validation errors (missing required fields, wrong types). This is standard FastAPI behavior.

**Errors common to all endpoints:**

- Error bodies are `{"detail": "<message>"}`. Pydantic validation errors use FastAPI's usual list-of-errors `detail`.
- `400 Bad Request` for a name that is not a safe path component (letters, digits, `_`, `-`; 1–128 characters; no dots or slashes) when it reaches on-disk path construction. In practice, unknown dataset and block names are rejected with `404` before this check.
- `500 Internal Server Error` for any unhandled exception. The body is `{"detail": "Internal error: <ExceptionType>: <message>"}`, and the traceback is logged on the server.

---

### `POST /datasets/{dataset}/filter`

Value filtering. Returns the individual cells of one datatype that pass a
predicate (`>`, `<`, `=`, `>=`, `<=`, `in`), with their coordinate labels and,
optionally, the values of co-located datatypes at those cells. The comparison
runs server-side in numpy/xarray, so only the passing cells go over the wire, not
the whole cube.

Use `/filter` when the question is "**which** cells pass?" (sparse, ragged
results). Use `/query` when you want a dense grid. They are separate endpoints
because the response shapes are different: `/query` returns `shape` + `index` +
dense `data`, while `/filter` returns long-form columns.

#### Request body (`FilterIn`)

```json
{
  "filter": {"datatype": "Correlation", "op": "gt", "value": 0.75},
  "datatypes": ["CorrelateResponse"],
  "select": [{"dimension": "Timepoint", "value": "1"}],
  "limit": 1000
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `filter` | Filter | yes | The predicate (see below) |
| `datatypes` | [string] | no (default `[]`) | Extra **co-located** datatypes to return at each passing cell. The filtered datatype is **always** returned, as the first column, whether or not it's listed. All must have exactly the same dimensions as the filtered datatype. |
| `select` | list[Selection] | no (default `[]`) | Applied **before** filtering, with the same semantics as `/query`: a scalar fixes and collapses a dimension, a list subsets it and keeps it, and the block key routes to one block, a subset (list), or all blocks (omitted). |
| `limit` | int ≥ 1 or null | no | Client cap on returned rows. The server also enforces a hard cap of `MAX_FILTER_ROWS = 100_000` (in `services/query.py`), so the effective cap is `min(limit, 100_000)`. |

`Filter` object:

| Field | Type | Description |
|-------|------|-------------|
| `datatype` | string | The datatype the predicate tests, e.g. `"Correlation"` |
| `op` | `"gt"` \| `"lt"` \| `"eq"` \| `"ge"` \| `"le"` \| `"in"` | `>`, `<`, `=`, `>=`, `<=`, membership |
| `value` | number \| string \| list[number \| string] | A single value for `gt`/`lt`/`eq`/`ge`/`le`; a **list** for `in` (and only for `in`). Numbers compare numerically. Strings compare against string-valued datatypes, e.g. `CorrelateResponse in ["7157", "4193"]`. |

NaN cells never pass any predicate. A label selected in `select` that a block
lacks reads as NaN, so that block contributes no rows for it and the request does
not fail. A label no block carries is still a `422`, as in `/query`.

#### Response body (`FilterOut`)

Columnar, long-form: one row per passing cell, all lists parallel. It loads
straight into a pandas DataFrame (`pd.DataFrame({**coords, **data})`).

| Field | Type | Description |
|-------|------|-------------|
| `dimensions` | [string] | The coordinate columns: the free dims in the datatype's axis order, then the block key **last**. The block key is **always** a column, even when a single block was selected with a scalar. |
| `coords` | {dim: [string]} | Per-row coordinate labels for each entry in `dimensions` |
| `data` | {datatype: [value]} | Per-row values for each returned datatype, filtered datatype first. Integers stay integers, strings stay strings, and NaN is `null`. |
| `count` | int | Number of rows **returned**, which equals the length of every column. It is **not** the total number of matching cells: when `truncated` is true, `count` is the cap. |
| `truncated` | bool | `true` if `limit` or the server cap cut the result short |

Rows come block by block in the order the blocks were targeted, and in row-major
(C) order within a block. When a cap applies, the **first** passing cells in that
order are kept, so truncated results are not a random sample. A dimension fixed
with a scalar `select` is not a column. If nothing passes, `count` is `0`, the
columns are empty lists, and `dimensions` still names the free dims plus the block
key. (If every array dim was fixed with a scalar and no cell passes, `dimensions`
and `coords` are empty.)

#### Example: `Correlation > 0.75` across all screens

The motivating case is the `response_corr` stores. There, `Correlation` (float),
`CorrelateResponse` (string, an entrez ID) and `CorrelateTimepoint` (string) are
all **data variables** over `(Rank, Timepoint, Response)`. `Rank` is the
dimension, and at each cell `CorrelateResponse` says *which gene* sits at that
rank. To get "the correlates and their identities", filter on `Correlation` and
return `CorrelateResponse` alongside. The block key is omitted, so every screen is
searched.

```json
{
  "filter": {"datatype": "Correlation", "op": "gt", "value": 0.75},
  "datatypes": ["CorrelateResponse"]
}
```

```json
{
  "dimensions": ["Rank", "Timepoint", "Response", "Screen"],
  "coords": {
    "Rank":      ["3", "7", "3"],
    "Timepoint": ["1", "1", "1"],
    "Response":  ["1017", "1017", "5230"],
    "Screen":    ["PS-SC-1", "PS-SC-1", "PS-SC-3"]
  },
  "data": {
    "Correlation":       [0.82, 0.79, 0.91],
    "CorrelateResponse": ["7157", "4193", "1234"]
  },
  "count": 3,
  "truncated": false
}
```

#### Example: `CorrelateResponse in [gene set]` on one screen

`in` works on string datatypes too. This finds every cell whose correlate is one
of a gene set (entrez IDs). The block key is selected with a scalar, but `Screen`
is still returned as a column:

```json
{
  "filter": {"datatype": "CorrelateResponse", "op": "in", "value": ["7157", "4193", "3845"]},
  "datatypes": ["Correlation"],
  "select": [{"dimension": "Screen", "value": "PS-SC-1"}],
  "limit": 5000
}
```

#### Error cases

| Status | Condition |
|--------|-----------|
| `400 Bad Request` | Unknown datatype (in `filter.datatype` or `datatypes`); unknown dimension in `select` |
| `404 Not Found` | Dataset not found; block(s) named in `select` not found |
| `422 Unprocessable Entity` | A datatype in `datatypes` doesn't share the filtered datatype's dimensions; empty list for the block key; selection label not found in any block; `op`/`value` cardinality mismatch (`in` without a list, or a list with any other op); `limit < 1`; a block read failure; malformed request body |

There is **no** byte-based `413` for `/filter`. The output is bounded only by the
row cap. The predicate is evaluated over the whole selected array of the filtered
datatype in each block, so scan cost scales with the source size, not the number
of rows returned. Co-located datatypes are gathered pointwise at the passing cells
only.

#### Shipped vs. the original proposal

The original design proposal was removed when `/filter` shipped. The shipped
behavior differs from it in a few ways:

- `count` is the number of rows **returned**, not the total matches before the cap.
- The block key is **always** a coordinate column. The proposal dropped it when a single block was fixed.
- `datatypes` defaults to `[]`, and the filtered datatype is always returned (first). There is no "test-only" filter datatype.
- The cap is rows only (`MAX_FILTER_ROWS = 100_000`). No byte cap, and `limit` is never mandatory.

---

## Future considerations

- Pre-computed aggregations and caching for aggregated queries.
- Authentication (today the service relies on oauth2_proxy at the network edge).
- Client reshaping for results with 3+ free dimensions.
- `/filter`: multiple AND-combined predicates, combining `filter` with `aggregate`, filtering on a datatype whose dims differ from the returned datatypes, and a byte-based cap (like `/query`'s `413`) in addition to the row cap.
- Server-declared gene dimensions/datatypes (so clients don't hard-code `GENE_DIMENSIONS` / `GENE_DATATYPES`; see [gene_symbol_relabel_issue.md](gene_symbol_relabel_issue.md)).
