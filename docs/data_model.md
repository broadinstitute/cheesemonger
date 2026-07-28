# Cheesemonger Data Model Guide

How cheesemonger models perturb-seq data — the domain concepts, the two stores
that hold them (SQLite metadata + Zarr on disk), their fields and relationships,
the invariants, and how it all maps onto queries and loading.

Companion docs: [codebase_guide.md](codebase_guide.md) (code structure),
[data_storage_design.md](data_storage_design.md) (chunking/benchmarks),
[api_design.md](api_design.md) (HTTP contract).

## Contents
1. [The big picture: two stores](#1-the-big-picture-two-stores)
2. [Domain concepts](#2-domain-concepts)
3. [Metadata model (SQLite)](#3-metadata-model-sqlite)
4. [The schema payload (JSON columns)](#4-the-schema-payload-json-columns)
5. [Data model (Zarr on disk)](#5-data-model-zarr-on-disk)
6. [How metadata and data connect](#6-how-metadata-and-data-connect)
7. [Invariants & validation](#7-invariants--validation)
8. [Worked example: perturb-scuba / PS-SC-1](#8-worked-example-perturb-scuba--ps-sc-1)
9. [Lifecycle & consistency](#9-lifecycle--consistency)
10. [Known limitations](#10-known-limitations)
11. [Planned changes: gene universe & per-block labels](#11-planned-changes-gene-universe--per-block-labels)

---

## 1. The big picture: two stores

Cheesemonger splits a dataset into **metadata** and **data**, stored separately:

| | Store | Holds | Accessed via |
|---|---|---|---|
| **Metadata** | SQLite (SQLAlchemy) | what datasets exist, their schema (dimensions, datatypes, labels), which blocks are loaded | `crud/` layer |
| **Data** | Zarr directories on disk (a Persistent Disk in prod) | the actual N-dimensional numeric arrays | `services/query.py` + `services/loader.py` |

Why split them: metadata is small, relational, and queried constantly (validate a
request, list blocks, build a response index) — SQLite answers those in
microseconds without opening a single Zarr chunk. The data is large and
array-shaped — Zarr + xarray handle chunked, label-indexed reads. Keeping them
apart means "does block X exist?" and "what are this dataset's datatypes?" never
touch the filesystem.

---

## 2. Domain concepts

| Concept | Definition | PESCA example |
|---|---|---|
| **Dataset** | A named collection sharing one schema. | `perturb-scuba` |
| **Last dimension** | The *organizational key*. Its values are **blocks**, stored as folders — **not** an array axis. Named per dataset (default `screen`). | `screen` |
| **Block** | One value of the last dimension = one independent Zarr store on disk (one screen's data). | `PS-SC-1` |
| **Dimension** | An **array axis** inside every block, with an ordered list of coordinate **labels**. | `Timepoint`, `Target`, `Response` |
| **Datatype** | A measured quantity = one array (data variable). Each declares which dimensions it spans (may be fewer than all). | `ZScore`, `L2FC`, `FDR`, `nCtrlCells` |
| **Label** | A coordinate value along a dimension. Genes are stored as **entrez IDs (strings)**. | `Target="23293"`, `Timepoint="D4"` |
| **Chunk shape** | Intended Zarr chunk sizes per dimension (storage/perf tuning). | `[]` (auto) |
| **Gene mapping** | Separate entrez↔symbol lookup (from Taiga), served for client-side translation. Not part of any dataset row. | — |

The pivotal idea: **the last dimension is a folder, not an axis.** A query
*refers* to it like any dimension (`screen="PS-SC-1"`), but resolving it means
"open that block's folder," whereas resolving `Timepoint="D4"` means "slice the
array." This is what makes adding/deleting a screen an O(1) folder + one DB row,
never a rewrite of a big array.

---

## 3. Metadata model (SQLite)

Two tables (`models/dataset.py`). Both use a string UUID primary key (`UUIDMixin`).

```mermaid
erDiagram
    DATASET ||--o{ BLOCK : "has"
    DATASET {
        string   id PK
        string   name UK "unique, indexed"
        string   last_dimension
        json     dimensions "list of {name, labels}; labels = present union"
        json     datatypes  "list of {name, dimensions, dtype}"
        json     chunk_shape "list of {name, size}"
        json     gene_universe "gene-dim -> allowed labels (validation superset)"
        datetime created_at
    }
    BLOCK {
        string   id PK
        string   dataset_id FK "-> dataset.id, ON DELETE CASCADE"
        string   name "indexed"
        datetime loaded_at
    }
```

**`dataset`**

| Column | Type | Notes |
|---|---|---|
| `id` | str (UUID) | primary key |
| `name` | str | **unique**, indexed — the dataset name |
| `last_dimension` | str | the organizational-key name (e.g. `screen`) |
| `dimensions` | JSON | list of `{name, labels}` — `labels` is the **present union** across loaded blocks (§11) |
| `datatypes` | JSON | list of `{name, dimensions, dtype}` |
| `chunk_shape` | JSON | list of `{name, size}` (may be empty) |
| `gene_universe` | JSON | `{gene_dim: [labels]}` — validation superset; blocks must be a subset (§11) |
| `created_at` | datetime (tz-aware) | set on insert |

**`block`**

| Column | Type | Notes |
|---|---|---|
| `id` | str (UUID) | primary key |
| `dataset_id` | str (FK) | → `dataset.id`, `ON DELETE CASCADE` |
| `name` | str | indexed; the block/screen name |
| `loaded_at` | datetime (tz-aware) | set on insert |

**Constraints & relationships**
- `UniqueConstraint(dataset_id, name)` — a block name is unique *within* a dataset (two datasets may both have a `SW620`).
- `Dataset.blocks` is a relationship with `cascade="all, delete-orphan"`; combined with the FK `ON DELETE CASCADE` (and `PRAGMA foreign_keys=ON`), deleting a dataset removes its block rows.
- The block row records only that a block **exists** (name + when loaded). The actual arrays live on disk (§5); there are no per-cell rows.

**Why JSON columns for dimensions/datatypes/chunk_shape?** They're nested,
read/written as a unit, and a dimension can carry 50k+ labels — modeling each
label as a row would be millions of rows for zero query benefit. They're
effectively a document embedded in the dataset row.

---

## 4. The schema payload (JSON columns)

The three JSON columns store Pydantic models (`schemas/common.py`), dumped to
plain dicts. Shapes:

**Dimension** — one array axis and its ordered labels.
```json
{"name": "Timepoint", "labels": ["D4", "D7"]}
```
- `name`: `SafeName` (see §7).
- `labels`: `list[str]`, ≤ 50,000. Numeric keys (timepoints, ranks) are stored as strings too. Order is the array's coordinate order.

**DatatypeSpec** — one measured array and the dimensions it spans.
```json
{"name": "ZScore", "dimensions": ["Timepoint", "Target", "Response"], "dtype": "float32"}
```
- `dimensions` is a subset of the dataset's dimension names, in array order. A datatype may span **fewer** dimensions than the full set (a "reduced-rank" datatype, e.g. `nCtrlCells` over just `["Timepoint"]`).
- `dtype` defaults to `"float32"`.

**ChunkDim** — an intended chunk size for one dimension.
```json
{"name": "Response", "size": 5000}
```
- Omitted dimensions default to their full extent. An empty list means "no explicit chunking." (See §10 — the loader currently rechunks to dask `auto` rather than honoring this.)

**API-facing views** (`schemas/dataset.py`) reshape the same data for responses:
- `DimensionInfo` adds `size`; for dimensions with > 100 labels it returns `labels_truncated=True` + a `labels_sample` (first 5) instead of the full list, to keep responses small.
- `BlockInfo` = `{name, loaded_at}`.
- `DatasetDetail` = the whole schema + blocks; `DatasetSummary` = `{name, blocks, datatypes}` counts for the list endpoint.

---

## 5. Data model (Zarr on disk)

Each **block** is an independent xarray-exported Zarr store:

```
{data_dir}/
  {dataset}/                         e.g. perturb-scuba/
    blocks/
      {block}/                       e.g. PS-SC-1/   (one screen)
        zarr.json / .zmetadata       group metadata
        {datatype}/                  e.g. ZScore/    (one data variable)
          zarr.json                  array metadata (dtype, shape, chunks)
          c/…                        chunk files
        {dimension}/                 e.g. Response/  (coordinate array)
          …
        .zattrs                      xarray dims metadata (_ARRAY_DIMENSIONS)
```

- Written by `xarray.Dataset.to_zarr()`, so dimension names and coordinate labels are embedded in the store (xarray reads them back with `.sel()` label indexing — no manual index math).
- **The last dimension is not present here** — it's the folder name (`PS-SC-1`), one level up.
- Each datatype is its own array; each dimension is a coordinate array. A block’s arrays all share the block's dimension labels.
- On load, cheesemonger **rechunks** the store (dask `auto`, ~128 MB chunks) and strips the source's chunk encoding, so a pathologically over-chunked delivery (100k+ tiny files) collapses to a handful of chunks. See [data_storage_design.md](data_storage_design.md).

---

## 6. How metadata and data connect

A query uses **both** stores; understanding the split explains the whole system.

```
POST /datasets/perturb-scuba/query
  select: screen=PS-SC-1, Timepoint=D4, Target=23293   datatype: ZScore
```

1. **SQLite** (`crud.get_schema_dict`) → the schema dict. Used to *validate*
   (is `ZScore` a datatype? is `Timepoint` a dimension? is the aggregate legal?)
   and to know the `last_dimension` name.
2. **SQLite** (`crud.list_block_names`) → the blocks; confirms `PS-SC-1` exists.
3. The selection is split:
   - `screen=PS-SC-1` matches `last_dimension` → **folder routing**: open `{data_dir}/perturb-scuba/blocks/PS-SC-1`.
   - `Timepoint=D4`, `Target=23293` → **array selection**: `da.sel(...)` on the Zarr.
4. **Zarr** returns the `Response` vector.
5. The response **index labels** come from the **block's own Zarr coordinates**
   (captured during the read), so the index length always matches the data — see
   §11. (SQLite's `dimensions[*].labels` present union is what
   `GET /dimensions/{dim}` returns, a *dataset-wide* view — not the per-query
   index.)

| Question | Answered by |
|---|---|
| Does this dataset/block exist? What are its datatypes/dimensions? | SQLite |
| Which folder holds block X? | derived: `{data_dir}/{dataset}/blocks/{block}` (sanitized) |
| What are the actual numbers? | Zarr on disk |
| What labels does the per-query response index use? | the queried block's Zarr coordinates |
| What labels does `GET /dimensions/{dim}` return? | SQLite present union (across all blocks) |

---

## 7. Invariants & validation

Enforced at dataset creation (`api/datasets.py`) and by the schema types:

- **Names are `SafeName`** — `^[A-Za-z0-9][A-Za-z0-9_\-]*$`, ≤128 chars. Applies to dataset, block, dimension, datatype, and chunk-dim names. No slashes/dots ⇒ no path traversal. Leading digits allowed (cell-line names like `22Rv1`). Enforced on request bodies (Pydantic → 422) and at filesystem path construction (`services/dataset.py` → `InvalidName` → 400). See [codebase_guide.md §15](codebase_guide.md#15-name-sanitization).
- **`last_dimension` must not appear in `dimensions`** — it's a folder key, not an axis.
- **Every `datatype.dimensions` entry must be a declared dimension.**
- **No dimension may have empty `labels`.**
- **Uniqueness** — dataset `name` is globally unique; block `name` is unique per dataset.
- **Size caps** — ≤ 20 dimensions, ≤ 50,000 labels/dimension, ≤ 100 datatypes.
- **Block gene labels ⊆ universe** (implemented, §11). For each dimension present in `dataset.gene_universe`, a block's coordinate labels along that dim must be a subset of the universe, else the load fails with a clear error. Blocks may still carry *different* label sets from one another (that is the point) — they just each have to fall within the shared universe. Dims absent from the universe (e.g. `Timepoint`) are not label-validated.

---

## 8. Worked example: perturb-scuba / PS-SC-1

**`dataset` row**
```json
{
  "name": "perturb-scuba",
  "last_dimension": "screen",
  "dimensions": [
    {"name": "Timepoint", "labels": ["D4", "D7"]},
    {"name": "Target",    "labels": ["23293", "55149"]},
    {"name": "Response",  "labels": ["10", "100", "…", "9997"]}
  ],
  "datatypes": [
    {"name": "ZScore",     "dimensions": ["Timepoint", "Target", "Response"], "dtype": "float32"},
    {"name": "L2FC",       "dimensions": ["Timepoint", "Target", "Response"], "dtype": "float32"},
    {"name": "FDR",        "dimensions": ["Timepoint", "Target", "Response"], "dtype": "float32"},
    {"name": "nCtrlCells", "dimensions": ["Timepoint"],                        "dtype": "int32"}
  ],
  "chunk_shape": []
}
```
(`nCtrlCells` shown as a reduced-rank datatype — spans only `Timepoint`.)

**`block` rows**: `{name: "PS-SC-1", dataset_id: <perturb-scuba id>}`, one per screen.

**On disk**
```
/mnt/data/perturb-scuba/blocks/PS-SC-1/
  ZScore/  L2FC/  FDR/  nCtrlCells/  …        ← data variables
  Timepoint/  Target/  Response/              ← coordinate arrays
```

**A query** `screen=PS-SC-1, Timepoint=D4, Target=23293, datatype=ZScore` →
open `…/blocks/PS-SC-1`, `ds["ZScore"].sel(Timepoint="D4", Target="23293")` →
a `Response`-length vector, indexed by the `Response` labels from SQLite.

---

## 9. Lifecycle & consistency

**Create dataset** → insert `dataset` row + `mkdir …/blocks/` → commit.
**Load block** → validate/infer schema, write the Zarr dir (rechunked), insert
`block` row → commit.
**Query** → read metadata (SQLite) + arrays (Zarr).
**Delete block** → delete `block` row + `rmtree` its dir → commit.
**Delete dataset** → refuse if blocks remain (409); else delete row (cascades) +
`rmtree` dataset dir → commit.

**Two-store consistency.** SQLite and the filesystem can't share a transaction,
so ordering is chosen to fail safe: data is written to disk *before* the DB row
is committed, and DB rows are deleted *before* the directory is removed. The
residual risk is an **orphaned Zarr directory** (data on disk with no `block`
row) if a process dies between the write and the commit — surfaced as wasted
disk, never as a phantom block in query results (queries enumerate blocks from
the DB). A reconcile/cleanup step is a future item (§10).

---

## 10. Known limitations

Tracked in [planning.md](planning.md):

- **Per-block coordinate labels (`TODO(per-block-coords)`) — RESOLVED (§11).**
  The response index is built from each block's own Zarr coordinates; a dataset
  declares a gene universe for load-time validation; and multi-block queries over
  screens with different label sets are aligned to the union with NaN-fill.
- **Broadcasted form required.** The query engine applies every fixed-dimension
  selection to each datatype, so it needs the "broadcasted" store (every datatype
  spans all selected dims). Reduced-rank datatypes are representable in the model
  but a query that fixes a dimension they lack is rejected until
  `TODO(unbroadcast)` lands.
- **`chunk_shape` not yet honored (`TODO(rechunk)`).** The column exists and is
  returned, but the loader rechunks to dask `auto` rather than to the declared
  chunk shape.
- **Orphaned directories.** No reconcile step yet for data-on-disk-without-a-row
  (see §9).

---

## 11. Gene universe & present-union labels

> **Status: IMPLEMENTED** — load path, single-block query index, and multi-block
> union + NaN-fill alignment. Resolves the per-block-coords limitation (§10).
> Full problem writeup: [per_block_labels_issue.md](per_block_labels_issue.md).
>
> **Implemented (load path):**
> - `dataset.gene_universe` column ({dim: [labels]}) — the validation superset.
> - Load-time validation: a block's labels along a universe dim must be a
>   subset, else the load fails (`loader._validate_against_universe`).
> - `dataset.dimensions[*].labels` is maintained as the **present union**:
>   fold-in on add (`crud.union_present_labels`), recompute-from-disk on
>   delete/overwrite (`loader._recompute_present_union`).
> - `reconcile` CLI command rebuilds the present union from the blocks on disk
>   (`loader.reconcile_dataset`) — the cache's safety net.
> - Universe sourced from Taiga (`hgnc-gene-table-e250.4` `entrez_id` column) or
>   a manifest, plus extra tokens like `Cas9` (`services/universe.py`).
> - `GET /datasets/{dataset}/dimensions/{dim}` already reads
>   `dimensions[*].labels`, so it now returns the present union with no change.
>
> **Implemented (query path):** the response `index` is built from each block's
> *own Zarr coordinates* (captured at read in `query._free_coords`), so index
> length always matches the data — the fix for the reported crash. Multi-block
> queries over screens with *different* label sets are aligned to the union of
> labels with NaN-fill (`query._align_blocks`); NaN-aware aggregators ignore the
> fills, so a gene measured in only some screens aggregates over those screens.
>
> **Design note — no per-block `labels` column.** Rather than persist each
> block's labels, the present union is maintained at the dataset level and
> recomputed from the blocks' Zarr coordinates on the (rare) delete/overwrite.
> This keeps the `block` table lean; the trade is that a removal reads the
> remaining blocks' coordinate arrays from disk. See the "Per-block labels" row
> below for the alternative that was considered.

### Why

Today a dimension's `labels` are frozen from the *first* block loaded (§6), but
screens legitimately measure different gene sets. That produces two bugs:

1. **Query mismatch** — the response `index` (from the frozen dataset labels) and
   `data` (from the block's Zarr) can have different lengths, crashing the client
   (`pandas`: "N columns passed, passed data had M columns").
2. **Stale `dimension_labels`** — `GET /datasets/{dataset}/dimensions/{dim}`
   returns only the first block's genes, silently omitting genes other screens
   added.

**Guiding principle:** the query `index` must come from the *same artifact that
sets the data length* — the block's Zarr coordinate — so index and data can never
disagree. Any other source (a frozen dataset column) is a claim that can drift.

### The model: three lists, three jobs

| List | Scope | Source | Purpose |
|---|---|---|---|
| **Universe** | dataset (gene dims) | pinned Taiga HGNC table (`hgnc-gene-table-e250.4`, the `entrez_id` column) **+ `Cas9`** | **load-time validation only** (`block ⊆ universe`) |
| **Per-block labels** | block | the screen's own Zarr coords, normalized to strings at load | source of truth; maintains the union; enables correct delete/overwrite |
| **Present union** | dataset (gene dims) | union of all loaded blocks' labels, maintained at load | what `GET /dimensions/{dim}` returns ("**what exists**") |

A single universe (HGNC entrez + `Cas9`) is used for every gene dimension —
simpler than a per-dimension universe, and sufficient for validation. The
endpoint returns the *present* set (what's actually loaded), **never** the
universe, so it never claims a gene exists when no screen has it.

### Metadata deltas (SQLite)

- **`dataset.dimensions[dim]`** gains a **`gene_universe`** (validation superset), and
  its **`labels` is redefined as the present union** — maintained at load, no
  longer frozen from the first block. The pinned Taiga id used to build the
  universe is recorded on the dataset for provenance.
- **`block`** gains a **`labels`** JSON column: `{dim_name: [labels…]}` — the
  screen's own coordinate labels (strings). This is the source of truth for
  maintaining the union and for recomputing it on delete/overwrite.
  *No migration for existing data — it is repopulated on reload.*

ERD delta (**bold** = new/changed):

```
DATASET  … , dimensions: [{name, universe(new), labels(=present union)}] , universe_taiga_id(new)
BLOCK    … , labels(new): {dim: [str, …]}
```

### Behavior changes

- **Query `index`** — built from the opened **block's Zarr coordinates**
  (post-`.sel()`), not the dataset row. Index length always equals data length.
- **`GET /datasets/{dataset}/dimensions/{dim}`** — returns the **present union**
  (what's actually loaded), read from the maintained union column: one fast DB
  read, no disk. Per-block `labels` keep the union correct through deletes and
  overwrites without re-reading Zarr.
- **Multi-block queries** across screens with different gene sets — align to the
  **union** and fill missing cells with **NaN** (a gene absent from a screen reads
  as NaN), matching the biology ("a missing gene ≡ that gene with all-NAs").
- **Labels are strings everywhere** — coordinate arrays are normalized to strings
  at load, so validation, `.sel()`, the index, and the union all compare
  apples-to-apples (no `"9992"` vs `9992` vs `9992.0`). Non-entrez tokens like
  `Cas9` live in the same string lists.

### Validation delta (§7)

The "per-block label agreement not enforced" note is replaced by:

- **Each block's gene labels must be ⊆ the dataset universe**, else the load
  fails with a clear error naming the offending IDs.
- The universe is **declared at dataset creation** (from the pinned Taiga table),
  never inferred from the first block. A library/probe-set change is handled as a
  **new dataset** (no in-place universe edits).

### Lifecycle delta (§9)

- **Load block** → normalize coords to strings → validate `⊆ universe` → write the
  Zarr → store the block's `labels` → fold them into the dataset's present union.
- **Delete / overwrite block** → drop/replace the block's `labels` → recompute the
  present union from the remaining blocks' labels (local, fast).
