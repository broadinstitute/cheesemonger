# Cheesemonger Data Storage Design

## 1. Definitions

### Dimension

A dimension is a named axis of a multi-dimensional array.

Cheesemonger's data has **3 array dimensions**: `timepoint`, `testedperturbation`, and `testedgeneexpression`. To locate a single measurement within a block, you must specify a value along every dimension. The `testedperturbation` dimension has cardinality 10,000 by default (there are 10,000 genes that can be knocked out).

There is also a **last dimension** (e.g. `screen`) which is not an array axis — it is an organizational key that determines which block to read from. See [Last dimension](#last-dimension) below.

### Coordinate

A coordinate is a set of labels attached to a dimension. Coordinates map human-readable names (or stable identifiers) to integer indices.

Without coordinates, `data[0, 4327, 0]` is hard to understand. With coordinates, we know that position 0 along `timepoint` is day 4, position 4327 along `testedperturbation` is entrez ID `"4609"` (MYC), and position 0 along `testedgeneexpression` is entrez ID `"29974"` (A1CF).

Coordinate labels for gene dimensions are stored as **entrez IDs** (string integers). A separate gene mapping (entrez ↔ gene symbol) is available via the API for client-side translation.

### Datatype (shard)

A datatype is one of the measured quantities stored for each combination of dimensions. Examples: `ZScore`, `L2FC`, `FDR`, `nTestCells`.

Queries specify one or more datatypes, and the system reads only those shards. Each shard has its own dimensions, shape, chunk layout, and files on disk. Datatype is deliberately not a dimension — not all datatypes span the same dimensions (e.g. `nCtrlCells` only has a `timepoint` axis).

### Chunk

A chunk is a fixed-size rectangular sub-block of a shard. Chunk shape is chosen to match query access patterns. For cheesemonger, chunks are shaped `(1, 1000, 5000)`, meaning one chunk covers 1 timepoint, 1,000 tested perturbations, and 5,000 tested gene expressions.

One chunk contains `1 × 1,000 × 5,000 = 5,000,000` float32 values = **20 MB**.

### Last dimension

The last dimension is the organizational key along which new data arrives incrementally. In cheesemonger, this is `screen` — each new biological experiment is loaded as a new **block** (an independent Zarr store in its own folder).

The last dimension is **not** an axis inside the arrays. It is a folder name on disk. This means:

- **Adding a block** = create a new directory, write chunk files. No existing data is touched.
- **Deleting a block** = `rm -rf {block}/`. O(1), no rewriting.
- **Querying one block** = open that block's Zarr store. Other blocks are never accessed.
- **Querying across blocks** = open N stores in parallel, combine results in memory.

In queries, the last dimension behaves like any other dimension from the client's perspective — it can appear in `select`, be aggregated over, etc. The server internally routes it to folder selection instead of array indexing.

### Dataset

A dataset is a collection of blocks that share the same schema (dimensions, coordinates, datatypes, chunk shape). It is the top-level unit of organization in cheesemonger. A dataset has:

- A **name** (e.g. `pesca`)
- A **last dimension** name (e.g. `screen`)
- A set of **dimensions** with their coordinate labels
- A list of **datatype shards** (each with its own subset of dimensions)
- A **chunk shape**

The schema is immutable after creation. Blocks are added incrementally via CLI. All blocks in a dataset must conform to the same schema.

---

## 2. The PESCA Data Model

### Source data

The raw data arrives as **xarray-exported Zarr stores** (one per screen), written via `xarray.Dataset.to_zarr()`. Each store contains 15 datatype arrays as data variables, plus coordinate arrays that encode dimension labels within the Zarr store itself. This is important because raw Zarr has no concept of named coordinates — xarray embeds them using its own conventions (stored in `.zattrs` metadata files).

Each store has the following index structure:

| Index Column | Dimension | Cardinality | Description |
|-------------|-----------|-------------|-------------|
| Screen | `screen` (last dimension) | 30+ | Cell line / experiment identifier |
| Timepoint | `timepoint` | 2 | Day of measurement after knockout (4, 7) |
| TestedPerturbation | `testedperturbation` | 10,000 | Gene that was knocked out (entrez ID) |
| TestedGeneExpression | `testedgeneexpression` | 18,000 | Gene whose expression was measured (entrez ID) |

Each coordinate tuple `(screen, timepoint, testedperturbation, testedgeneexpression)` has values for all 15 datatypes. The full dataset has `30 × 2 × 10,000 × 18,000 = 10.8 billion` measurements per datatype.

Each dimension has a 1-D coordinate array stored alongside the data. Because the stores are xarray-exported, coordinates are embedded in the Zarr store itself (as separate Zarr arrays with xarray metadata in `.zattrs`). The server reads them via `xarray.open_zarr()`, which reconstructs the full labeled Dataset with named dimensions and coordinates automatically.

### On-disk layout (separate-block model)

Dataset metadata (schema, dimensions, datatypes, block registry) is stored in a **SQLite database** (`cheesemonger.db`), not on disk alongside the blocks. See the [API design doc](api_design.md#metadata-storage) for the ERD and details.

Each block's **data** is an xarray Dataset exported as Zarr. The coordinate arrays (`timepoint/`, `testedperturbation/`, etc.) are written by xarray alongside the data variables.

```
/mnt/data/pesca/
  blocks/
    SW620/                       ← one block = one xarray Dataset as Zarr
      .zattrs                    ← xarray metadata (dimension names, conventions)
      .zgroup                    ← Zarr group marker
      .zmetadata                 ← consolidated metadata (optional, for faster open)
      ZScore/                    ← data variable — 3-D Zarr array
        .zarray                  ← Zarr array metadata (shape, chunks, dtype)
        .zattrs                  ← xarray attrs (dimension names: [timepoint, ...])
        0.0.0                    ← chunk files
        0.0.1
        ...
      L2FC/                      ← data variable
        ...
      FDR/
      ...
      timepoint/                 ← coordinate array written by xarray
        .zarray
        .zattrs
        0
      testedperturbation/        ← coordinate array (10,000 entrez IDs)
        .zarray
        .zattrs
        0
      testedgeneexpression/      ← coordinate array (18,000 entrez IDs)
        .zarray
        .zattrs
        0
    HT29/
      ...
    A549/
      ...
```

The `blocks/` subdirectory mirrors the dataset/block naming: the CLI
`delete-block --dataset pesca --block SW620` maps directly to
`rm -rf /mnt/data/pesca/blocks/SW620/`.

**Why xarray-exported Zarr?** Raw Zarr has no concept of named coordinates. When Bella writes `ds.to_zarr(...)`, xarray embeds coordinate labels as separate Zarr arrays and records dimension names in `.zattrs`. When the server reads with `xr.open_zarr(...)`, it reconstructs the full labeled Dataset. This enables `.sel(timepoint=4, testedperturbation="4193")` for label-based indexing instead of manual integer-index lookups.

---

## 3. Chunking Strategy

### Current chunk shape

`(timepoint=1, testedperturbation=1000, testedgeneexpression=5000)`

One chunk contains `1 × 1,000 × 5,000 = 5,000,000` float32 values = **20 MB**.

### Why this shape

1. **Timepoint = 1**: The query fixes one timepoint. Chunk size 1 means no other timepoint's data is loaded.
2. **TestedPerturbation = 1000**: With 10,000 perturbations and chunk size 1,000, there are 10 perturbation chunks. The query fixes one perturbation, which falls in exactly 1 of the 10 chunks.
3. **TestedGeneExpression = 5000**: With 18,000 gene expressions and chunk size 5,000, there are 4 expression chunks (5000 + 5000 + 5000 + 3000). The query reads all gene expressions, so it reads all 4 chunks.

A typical series query (fix one timepoint + one perturbation, return all gene expressions) reads **4 chunks = ~80 MB**.

### Chunk count

Per block, per full-dimensional datatype (e.g. ZScore):

`2 timepoints × 10 perturbation bands × 4 expression bands = 80 chunks`

Across the full dataset (30 blocks × 15 datatypes, though not all datatypes are full-dimensional):

`30 blocks × 80 chunks × 8 full-dim datatypes = 19,200 chunks` (plus smaller counts for reduced-dimension datatypes)

---

## 4. Benchmark Findings

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
