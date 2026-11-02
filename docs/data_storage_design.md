# Cheesemonger Data Storage Design

## 1. Definitions

### Dimension

A dimension is a named axis of a multi-dimensional array.

The deployed perturb-seq data (the `*_degs` hypercube) has **3 array dimensions**: `Timepoint`, `Target` (the gene that was knocked out), and `Response` (the gene whose expression was measured). To locate a single measurement within a block, you must specify a value along every dimension. In the design-era sizing used throughout this doc, `Target` has cardinality ~10,000 and `Response` ~18,000; real screens vary.

> Dimension names are not hard-coded — they are inferred from the source store when a dataset is created. Older design docs and the test fixtures use the PESCA-era names `timepoint` / `testedperturbation` / `testedgeneexpression`; they map onto `Timepoint` / `Target` / `Response`.

There is also a **last dimension** (default `Screen`, set per dataset with `load --last-dimension`; case-sensitive) which is not an array axis — it is an organizational key that determines which block to read from. See [Last dimension](#last-dimension) below.

### Coordinate

A coordinate is a set of labels attached to a dimension. Coordinates map human-readable names (or stable identifiers) to integer indices.

Without coordinates, `data[0, 4327, 0]` is hard to understand. With coordinates, we know that position 0 along `Timepoint` is day 4 (label `"D4"`), position 4327 along `Target` is entrez ID `"4609"` (MYC), and position 0 along `Response` is entrez ID `"29974"` (A1CF).

Coordinate labels are stored as **strings** for every dimension (the loader stringifies them on write, so a numeric label such as a rank `0` is stored and queried as `"0"`). Gene dimensions use **entrez IDs**. A separate gene mapping (entrez ↔ gene symbol) is available via `GET /gene_mappings` for client-side translation.

### Datatype (shard)

A datatype is one of the measured quantities stored for each combination of dimensions. Examples: `ZScore`, `L2FC`, `FDR`, `nTestCells`.

Queries specify one or more datatypes, and the system reads only those shards. Each shard has its own dimensions, shape, chunk layout, and files on disk. Datatype is deliberately not a dimension — not all datatypes span the same dimensions (e.g. `nCtrlCells` only has a `Timepoint` axis).

### Chunk

A chunk is a fixed-size rectangular sub-block of a shard. Chunk shape is chosen to match query access patterns. Chunking is set **per dataset** when the dataset is created (see [§3](#3-chunking-strategy)). The recommended layout for the degs hypercube is `(Timepoint=1, Target=1000, Response=5000)`: one chunk covers 1 timepoint, 1,000 targets, and 5,000 responses.

One such chunk contains `1 × 1,000 × 5,000 = 5,000,000` float32 values = **20 MB**.

### Last dimension

The last dimension is the organizational key along which new data arrives incrementally. In cheesemonger, this is `Screen` by default — each new biological experiment is loaded as a new **block** (an independent Zarr store in its own folder).

The last dimension is **not** an axis inside the arrays. It is a folder name on disk. This means:

- **Adding a block** = create a new directory, write chunk files, insert a block row in SQLite. No existing data is touched.
- **Deleting a block** = delete its SQLite row and `rm -rf {block}/`. No data rewriting (only the dataset's present-union labels are recomputed; see below).
- **Querying one block** = open that block's Zarr store. Other blocks are never accessed.
- **Querying across blocks** = open N stores in parallel, combine results in memory.

In queries, the last dimension behaves like any other dimension from the client's perspective — it can appear in `select`, be aggregated over, etc. The server internally routes it to folder selection instead of array indexing.

### Dataset

A dataset is a collection of blocks that share the same schema (dimension names, datatypes, chunk shape). It is the top-level unit of organization in cheesemonger. Its metadata lives in the SQLite database (see [On-disk layout](#on-disk-layout-separate-block-model)). A dataset has:

- A **name** (e.g. `ps100_degs`)
- A **last dimension** name (default `Screen`)
- A set of **dimensions** with their coordinate labels
- A list of **datatype shards** (each with its own subset of dimensions)
- A **chunk shape** (possibly empty, meaning the loader's default chunking)
- An optional **gene universe** — the allowed label superset for gene dimensions

The dataset is created by the first `load --create-dataset`, which infers it from that block's source store. After that:

- **Fixed at creation:** the last dimension, the dimension *names*, the datatypes, and the chunk shape. Every later block must use only dimensions and datatypes the dataset declares, and is written with the stored chunk shape (any `--chunk` flags on later loads are ignored).
- **Not fixed:** the dimension *labels*. A dataset's `dimensions[*].labels` is the **present union** — the labels actually present across its loaded blocks. Blocks may carry different label sets (screens legitimately measure different genes); each block's labels must only be a subset of the gene universe, if one is declared. The union grows when a block is loaded, is recomputed from the remaining blocks on `delete-block` (it can shrink), and can be rebuilt from disk at any time with `python -m cheesemonger reconcile --dataset NAME`.

Blocks are added and removed via the CLI only; the REST API is read-only.

---

## 2. The PESCA Data Model

### Source data

The raw data arrives as **xarray-exported Zarr stores** (one per screen), written via `xarray.Dataset.to_zarr()` with zarr-python 3.1.1, i.e. **Zarr format v3**. Each store contains the datatype arrays as data variables (~15 in the design-era sizing), plus coordinate arrays that encode dimension labels within the Zarr store itself. In Zarr v3, each array's `zarr.json` records its `dimension_names` natively, and xarray writes each coordinate as its own 1-D array.

Each degs store has the following index structure:

| Index Column | Dimension | Cardinality | Description |
|-------------|-----------|-------------|-------------|
| Screen | `Screen` (last dimension) | 30+ | Cell line / experiment identifier |
| Timepoint | `Timepoint` | 2 | Day of measurement after knockout (`D4`, `D7`) |
| Target | `Target` | ~10,000 | Gene that was knocked out (entrez ID) |
| Response | `Response` | ~18,000 | Gene whose expression was measured (entrez ID) |

Each coordinate tuple `(Screen, Timepoint, Target, Response)` has values for the full-dimensional datatypes. At design-era sizing the full dataset has `30 × 2 × 10,000 × 18,000 = 10.8 billion` measurements per datatype.

Besides the degs hypercube, each screen also ships aggregate stores (`*_response_agg`, `*_target_agg`) and correlate stores (`*_response_corr`, `*_target_corr`). Each kind is loaded as its own dataset (see `docs/bulk_load_cheesemonger_data.sh`), and each has its own dimensions and chunking.

Each dimension has a 1-D coordinate array stored alongside the data. Because the stores are xarray-exported, coordinates are embedded in the Zarr store itself (as separate Zarr arrays). The server reads them via `xarray.open_zarr()`, which reconstructs the full labeled Dataset with named dimensions and coordinates automatically.

### On-disk layout (separate-block model)

Dataset metadata (last dimension, dimensions and their present-union labels, datatypes, chunk shape, gene universe, block registry) is stored in a **SQLite database** via SQLAlchemy, not on disk alongside the blocks. The location comes from `SQLALCHEMY_DATABASE_URL` (default `sqlite:///./cheesemonger.db`). See the [data model guide](data_model.md) and the [API design doc](api_design.md#metadata-storage) for the ERD and details.

Each block's **data** is an xarray Dataset that the CLI loader rewrites as a Zarr v3 store under `DATA_DIR/{dataset}/blocks/{block}/` (`DATA_DIR` defaults to `/mnt/data`). The loader stringifies the coordinates and rechunks to the dataset's chunk shape. The coordinate arrays (`Timepoint/`, `Target/`, `Response/`) are written by xarray alongside the data variables.

```
/mnt/data/ps100_degs/
  blocks/
    SW620/                       ← one block = one xarray Dataset as Zarr v3
      zarr.json                  ← group metadata (node_type "group", zarr_format 3)
                                   plus inline consolidated metadata for every array
      ZScore/                    ← data variable — 3-D Zarr array
        zarr.json                ← array metadata: shape, data_type, chunk_grid,
                                   codecs (e.g. bytes + zstd), fill_value,
                                   dimension_names ["Timepoint", "Target", "Response"]
        c/                       ← chunk files, one path segment per dimension
          0/0/0
          0/0/1
          ...
      L2FC/                      ← data variable
        ...
      FDR/
      ...
      Timepoint/                 ← coordinate array written by xarray
        zarr.json
        c/0
      Target/                    ← coordinate array (entrez IDs, as strings)
        zarr.json
        c/0
      Response/                  ← coordinate array (entrez IDs, as strings)
        zarr.json
        c/0
    HT29/
      ...
    A549/
      ...
```

(This layout matches a store written locally by the loader, e.g. `data/perturb-scuba/blocks/PS-SC-1/`.)

The `blocks/` subdirectory mirrors the dataset/block naming: the CLI
`delete-block --dataset ps100_degs --block SW620` deletes the block's SQLite row,
removes `/mnt/data/ps100_degs/blocks/SW620/`, and recomputes the dataset's
present-union labels from the remaining blocks.

**Why xarray-exported Zarr?** Raw Zarr arrays carry dimension names (in v3) but no coordinate labels. When the pipeline writes `ds.to_zarr(...)`, xarray stores each coordinate's labels as a separate Zarr array and links it to the data variables through their `dimension_names`. When the server reads with `xr.open_zarr(...)`, it reconstructs the full labeled Dataset. This enables `.sel(Timepoint="D4", Target="4193")` for label-based indexing instead of manual integer-index lookups.

---

## 3. Chunking Strategy

### How chunking is configured

Chunking is **per dataset**, chosen when the dataset is created:

```bash
python -m cheesemonger load --create-dataset \
    --source gs://.../SW620_degs.zarr --dataset ps100_degs --block SW620 \
    --chunk Timepoint=1 --chunk Target=1000 --chunk Response=5000
```

- `--chunk DIM=SIZE` is repeatable. Each listed dimension is chunked to `SIZE`; **every unlisted dimension is kept whole** (one chunk along that axis). Entries naming a dimension the source doesn't have are dropped.
- The resulting chunk shape is stored on the dataset in SQLite (`chunk_shape`) and **reused for every later block**, so all blocks of a dataset are chunked identically. `--chunk` on a load into an existing dataset is ignored. To change a dataset's chunking, delete the dataset and reload it.
- With **no** `--chunk` flags, the dataset's chunk shape is empty and the loader falls back to dask `"auto"` chunking with an **~8 MiB per-chunk target** (`_DEFAULT_CHUNK_TARGET` in `cheesemonger/services/loader.py`). That is far below dask's ~128 MiB default, so a point query doesn't pull a huge band. Object-dtype (string) data variables are written as a single chunk, because dask can't size them automatically.
- Chunk shape is reported by `GET /datasets/{dataset}` (`chunk_shape`).

The bulk loader (`docs/bulk_load_cheesemonger_data.sh`) uses `Timepoint=1, Target=1000, Response=5000` for the degs hypercube. It uses the correlate settings [below](#correlate-stores-_target_corr-_response_corr) for the correlate stores, and the 8 MiB default for the small aggregate stores.

### Recommended degs layout and its rationale

`(Timepoint=1, Target=1000, Response=5000)`

One chunk contains `1 × 1,000 × 5,000 = 5,000,000` float32 values = **20 MB**. This is the "big chunks" layout that won the benchmarks in [§4](#4-benchmark-findings). Those benchmarks used the earlier PESCA naming and are the rationale for these defaults.

1. **Timepoint = 1**: The query fixes one timepoint. Chunk size 1 means no other timepoint's data is loaded.
2. **Target = 1000**: With 10,000 targets and chunk size 1,000, there are 10 target chunks. The query fixes one target, which falls in exactly 1 of the 10 chunks.
3. **Response = 5000**: With 18,000 responses and chunk size 5,000, there are 4 response chunks (5000 + 5000 + 5000 + 3000). The query reads all responses, so it reads all 4 chunks.

A typical series query (fix one timepoint + one target, return all responses) reads **4 chunks = ~80 MB**.

The layout is a 2-D balance: "fix Target, sweep Response" and "fix Response, sweep Target" both stay acceptable. Workloads dominated by one series direction can chunk more aggressively, e.g. `--chunk Target=1 --chunk Timepoint=1` (Response whole) makes each `(Timepoint, Target)` series exactly one chunk.

### Chunk count

Per block, per full-dimensional datatype (e.g. ZScore), with the recommended layout:

`2 timepoints × 10 target bands × 4 response bands = 80 chunks`

Across the full dataset (30 blocks × 15 datatypes, though not all datatypes are full-dimensional):

`30 blocks × 80 chunks × 8 full-dim datatypes = 19,200 chunks` (plus smaller counts for reduced-dimension datatypes)

### Correlate stores (`*_target_corr`, `*_response_corr`)

The correlate stores have a different shape and are chunked separately from the
degs hypercube. Their dimensions are `(Rank, Timepoint, <keyed dim>)`, where the
keyed dim is `Target` (target_corr) or `Response` (response_corr), and `Rank` is
the top-N leaderboard position (e.g. 100). Datatypes: `Correlation` (float32) plus
the object-dtype coordinate arrays `CorrelateTarget` / `CorrelateResponse` /
`CorrelateTimepoint`.

**Chunk shape:** `(Rank=whole, Timepoint=1, <keyed dim>=1024)`.

Two access patterns must both stay fast, which sets the keyed-dim chunk width:

1. **Single-entity lookup** — "read one target's top-N correlates" fixes the keyed
   dim + Timepoint and reads the whole `Rank` axis. Wants a *small* keyed chunk so
   the lookup reads ≈ one chunk.
2. **Full-axis value-filter scan** — `POST /filter` with no keyed selection scans
   every entity to find passing cells. Wants a *large* keyed chunk so the scan
   touches few chunks (per-chunk read overhead, not bytes, dominates here — the
   whole `Correlation` cube is only ~a few MB).

`1024` is the balance point. For a 9,776-target store, `Correlation`
(`Rank=100 × Timepoint=1 × Target=1024` × 4 B ≈ **410 KB/chunk**) gives:

- **scan:** `⌈9776/1024⌉ × 2 timepoints ≈ 20 chunks` (was **19,552** at the old
  `keyed=1`, which made value-filter scans take ~a minute);
- **lookup:** exactly **1 chunk** (~410 KB) per (entity, timepoint).

Set via the loader (`--chunk Target=1024 --chunk Timepoint=1`) — see the
`TARGET_CHUNK` knob in `docs/bulk_load_cheesemonger_data.sh` (default `1024`).
Lower it (e.g. 256) to favor point lookups, raise it (e.g. 4096) to favor scans;
**never use `1`** — one tiny chunk per entity produces tens of thousands of files
and cripples scans.

---

## 4. Benchmark Findings

> These benchmarks were run before chunking became configurable per dataset. They are kept as the **rationale for the recommended defaults** in [§3](#3-chunking-strategy): Zarr on Hyperdisk, with the "big" `(1, 1000, 5000)` degs layout. The 4-tuple chunk shapes below include a leading screen axis of size 1 from the benchmark harness. They used the PESCA-era names (perturbation = `Target`, gene expression = `Response`). The results are still valid for those layouts.

**Date:** 2026-05-27
**Machine:** c3-standard-4 (4 vCPU, 16 GB RAM)
**Dataset:** 1 screen, 2 timepoints, 10,000 perturbations, 18,000 gene expressions
**Uncompressed size:** ~20 GB (15 datatypes × 360M float32 values)

### Test matrix

| Config | Format | Storage | Chunk preset | Chunk shape | ~MB/chunk |
|--------|--------|---------|-------------|-------------|-----------|
| A1 | Zarr | GCS bucket | big | (1,1,1000,5000) | 19.1 |
| A2 | Zarr | GCS bucket | small | (1,1,250,1000) | 1.0 |
| B1 | NetCDF | Hyperdisk | big | (1,1,1000,5000) | 19.1 |
| B2 | NetCDF | Hyperdisk | small | (1,1,250,1000) | 1.0 |
| C1 | Zarr | Hyperdisk | big | (1,1,1000,5000) | 19.1 |
| C2 | Zarr | Hyperdisk | small | (1,1,250,1000) | 1.0 |

### Cold-read results (single screen)

20 measured iterations per query.

**A1 — Zarr on GCS, big chunks** `(1, 1, 1000, 5000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            413.5ms   541.4ms   659.5ms   924.1ms   990.3ms   (18000,)
series/neg_log10_FDR                     407.2ms   488.5ms   665.3ms   672.5ms   674.3ms   (18000,)
series/L2FC                              405.2ms   474.9ms   619.2ms   631.8ms   634.9ms   (18000,)
series/TestMean                          382.5ms   424.7ms   549.3ms   590.6ms   601.0ms   (18000,)
series/CtrlMean                          343.0ms   431.3ms   528.9ms   626.2ms   650.5ms   (18000,)
series/nTestCells                        386.0ms   448.0ms   584.7ms   771.6ms   818.3ms   (18000,)
agg/ZScore_mean_over_perturbation        1855.8ms  1947.6ms  2243.8ms  2875.5ms  3033.4ms  (18000,)
agg/FDR_count_lt_0.1                     1732.1ms  1937.3ms  2195.4ms  2969.7ms  3163.2ms  (10000,)
```

**A2 — Zarr on GCS, small chunks** `(1, 1, 250, 1000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            388.3ms   435.8ms   544.1ms   625.0ms   645.2ms   (18000,)
series/neg_log10_FDR                     396.9ms   440.2ms   591.2ms   635.6ms   646.7ms   (18000,)
series/L2FC                              363.4ms   432.5ms   550.4ms   583.0ms   591.1ms   (18000,)
series/TestMean                          361.3ms   441.7ms   609.2ms   656.8ms   668.6ms   (18000,)
series/CtrlMean                          366.0ms   411.8ms   488.2ms   562.8ms   581.5ms   (18000,)
series/nTestCells                        368.4ms   409.0ms   744.0ms   906.2ms   946.7ms   (18000,)
agg/ZScore_mean_over_perturbation        7486.5ms  7959.9ms  9506.8ms  12554.9ms 13316.9ms (18000,)
agg/FDR_count_lt_0.1                     7552.2ms  8692.6ms  9816.5ms  12796.8ms 13541.9ms (10000,)
```

**B1 — NetCDF on Hyperdisk, big chunks** `(1, 1, 1000, 5000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            284.0ms   295.5ms   326.3ms   608.6ms   679.2ms   (18000,)
series/neg_log10_FDR                     283.4ms   296.7ms   327.9ms   625.7ms   700.2ms   (18000,)
series/L2FC                              283.3ms   289.0ms   317.6ms   552.1ms   610.7ms   (18000,)
series/TestMean                          285.1ms   297.1ms   314.6ms   447.5ms   480.7ms   (18000,)
series/CtrlMean                          285.9ms   298.8ms   333.6ms   703.4ms   795.9ms   (18000,)
series/nTestCells                        285.2ms   297.7ms   317.8ms   455.7ms   490.1ms   (18000,)
agg/ZScore_mean_over_perturbation        2964.0ms  3097.6ms  3275.1ms  4563.5ms  4885.6ms  (18000,)
agg/FDR_count_lt_0.1                     2948.3ms  3041.1ms  3267.6ms  4704.6ms  5063.8ms  (10000,)
```

**B2 — NetCDF on Hyperdisk, small chunks** `(1, 1, 250, 1000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            87.4ms    88.5ms    116.4ms   210.5ms   234.1ms   (18000,)
series/neg_log10_FDR                     88.4ms    89.8ms    96.1ms    168.5ms   186.6ms   (18000,)
series/L2FC                              88.5ms    89.4ms    115.3ms   168.9ms   182.3ms   (18000,)
series/TestMean                          88.1ms    89.7ms    94.7ms    161.0ms   177.5ms   (18000,)
series/CtrlMean                          89.2ms    89.9ms    116.6ms   175.2ms   189.8ms   (18000,)
series/nTestCells                        88.1ms    89.5ms    96.0ms    167.4ms   185.2ms   (18000,)
agg/ZScore_mean_over_perturbation        2069.3ms  2137.1ms  2384.3ms  4417.6ms  4925.9ms  (18000,)
agg/FDR_count_lt_0.1                     2246.7ms  2272.3ms  2473.0ms  4899.5ms  5506.2ms  (10000,)
```

**C1 — Zarr on Hyperdisk, big chunks** `(1, 1, 1000, 5000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            30.9ms    44.0ms    161.1ms   162.9ms   163.3ms   (18000,)
series/neg_log10_FDR                     31.1ms    34.5ms    161.9ms   162.0ms   162.1ms   (18000,)
series/L2FC                              31.3ms    35.4ms    160.6ms   162.4ms   162.9ms   (18000,)
series/TestMean                          31.4ms    35.1ms    161.0ms   161.6ms   161.7ms   (18000,)
series/CtrlMean                          31.7ms    45.1ms    161.3ms   161.5ms   161.6ms   (18000,)
series/nTestCells                        31.4ms    36.8ms    162.5ms   163.0ms   163.2ms   (18000,)
agg/ZScore_mean_over_perturbation        419.4ms   447.9ms   467.2ms   467.7ms   467.8ms   (18000,)
agg/FDR_count_lt_0.1                     339.1ms   344.8ms   435.0ms   1377.9ms  1613.6ms  (10000,)
```

**C2 — Zarr on Hyperdisk, small chunks** `(1, 1, 250, 1000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            33.7ms    34.7ms    59.2ms    76.3ms    80.5ms    (18000,)
series/neg_log10_FDR                     34.5ms    35.5ms    38.2ms    56.3ms    60.8ms    (18000,)
series/L2FC                              34.4ms    35.6ms    63.1ms    65.1ms    65.6ms    (18000,)
series/TestMean                          34.9ms    36.0ms    38.5ms    67.6ms    74.9ms    (18000,)
series/CtrlMean                          34.8ms    36.0ms    38.5ms    56.0ms    60.3ms    (18000,)
series/nTestCells                        35.2ms    36.1ms    56.9ms    64.8ms    66.8ms    (18000,)
agg/ZScore_mean_over_perturbation        1089.9ms  1132.9ms  1179.9ms  1567.8ms  1664.8ms  (18000,)
agg/FDR_count_lt_0.1                     1083.7ms  1094.2ms  1165.5ms  1683.4ms  1812.8ms  (10000,)
```

### Cold-read results — triple screens (C1 only)

Same methodology, run against a 3-screen dataset (~60 GB). Queries target Screen_000.

**C1 — Zarr on Hyperdisk, big chunks, 3 screens** `(1, 1, 1000, 5000)`

```
Query                                    Min       P50       P95       P99       Max       Shape
series/ZScore                            31.3ms    44.4ms    161.8ms   167.2ms   168.5ms   (18000,)
series/neg_log10_FDR                     31.5ms    35.5ms    161.5ms   162.1ms   162.2ms   (18000,)
series/L2FC                              32.1ms    35.9ms    161.5ms   162.2ms   162.3ms   (18000,)
series/TestMean                          31.9ms    35.5ms    161.8ms   162.2ms   162.3ms   (18000,)
series/CtrlMean                          31.7ms    47.5ms    161.9ms   162.0ms   162.1ms   (18000,)
series/nTestCells                        32.1ms    36.3ms    161.4ms   161.7ms   161.8ms   (18000,)
agg/ZScore_mean_over_perturbation        431.7ms   454.3ms   463.0ms   479.5ms   483.6ms   (18000,)
agg/FDR_count_lt_0.1                     346.4ms   355.0ms   440.1ms   1376.9ms  1611.1ms  (10000,)
```

Adding more screens does **not** degrade single-screen query performance. Each block is an independent Zarr store — querying one block never touches another's files.

### Summary

**Winner: C1 = Zarr + Hyperdisk + big chunks**

**Series queries** (average P50 across 6 series queries):

| Config | Format + Storage | Chunks | Avg P50 |
|--------|-----------------|--------|---------|
| C1 | Zarr + Hyperdisk | big | **33 ms** |
| C2 | Zarr + Hyperdisk | small | 36 ms |
| B2 | NetCDF + Hyperdisk | small | 89 ms |
| B1 | NetCDF + Hyperdisk | big | 296 ms |
| A2 | Zarr + GCS | small | 429 ms |
| A1 | Zarr + GCS | big | 468 ms |

**Aggregation queries** (average P50 across 2 agg queries):

| Config | Format + Storage | Chunks | Avg P50 |
|--------|-----------------|--------|---------|
| C1 | Zarr + Hyperdisk | big | **414 ms** |
| C2 | Zarr + Hyperdisk | small | 1,114 ms |
| A1 | Zarr + GCS | big | 1,943 ms |
| B2 | NetCDF + Hyperdisk | small | 2,205 ms |
| B1 | NetCDF + Hyperdisk | big | 3,069 ms |
| A2 | Zarr + GCS | small | 8,326 ms |

### Key takeaways

- **C1 (Zarr + Hyperdisk + big chunks) is the clear winner** across both query types.
- Big chunks are better for aggregation queries in general, except for NetCDF + Hyperdisk (probably due to optimized seek).
- GCS's per-query HTTP round-trip cost is enormous: C1 (Zarr Hyperdisk, 33 ms) vs A1 (Zarr GCS, 468 ms). This also causes small chunks on GCS to perform terribly for aggregation queries.
- **NetCDF has a global lock.** HDF5 is not thread-safe by default, requiring a `threading.Lock` around all reads. This makes it hard to serve multiple requests with multithreading. Zarr has no such limitation — each chunk is an independent file.
- Adding more screens to disk has **zero impact** on single-screen query latency (confirmed by triple-screen benchmark).

---

## 5. GCP Cost Estimate (1 TB dataset)

Based on benchmarking, the production deployment is **Zarr on Hyperdisk Balanced** served from an `n4-standard-4` VM.

### Component pricing (us-central1)

| Component | Spec | Unit price | Monthly cost |
|-----------|------|------------|-------------|
| **Hyperdisk Balanced capacity** | 1,000 GiB | $0.080/GiB/month | **$80** |
| **Hyperdisk Balanced IOPS** | 3,000 baseline (included free) | $0.005/IOPS/month above 3,000 | **$0** |
| **Hyperdisk Balanced throughput** | 140 MBps baseline (included free) | $0.040/MBps/month above 140 | **$0** |
| **n4-standard-4 VM** (on-demand) | 4 vCPU, 16 GB RAM | $0.1814/hr | **~$132** |

| | On-demand | 1-year CUD (~30% discount) |
|---|----------|------------|
| **VM** | $132/mo | ~$93/mo |
| **Disk** | $80/mo | $80/mo |
| **Total** | **$212/mo** | **~$173/mo** |

### Notes

- Get `cds.team` to be able to mount Hyperdisks.
- Should use newer machines (n4 series).
