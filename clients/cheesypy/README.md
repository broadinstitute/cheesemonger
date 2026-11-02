# cheesypy

Python client for the [Cheesemonger](../../README.md) perturb-seq API. It talks
to a Cheesemonger server over HTTP and hands you results as pandas objects, so
you can go from a query to a plot in one line. Depends only on `httpx` and
`pandas`.

- [Install](#install)
- [Quick start](#quick-start)
- [Concepts](#concepts)
- [Discovering labels](#discovering-labels)
- [Reading data](#reading-data)
- [Filtering by value](#filtering-by-value)
- [Return types](#return-types)
- [Gene symbols](#gene-symbols)
- [Loading data](#loading-data)
- [Errors](#errors)
- [Development](#development)
- [Publishing](#publishing)

## Install

From the CDS Artifact Registry Python repo:

```bash
# pip
pip install --extra-index-url \
  https://us-central1-python.pkg.dev/cds-artifacts/public-python/simple/ cheesypy

# uv (in a project)
uv add cheesypy --index \
  https://us-central1-python.pkg.dev/cds-artifacts/public-python/simple/
```

Or pin the index in your project's `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "public-python"
url = "https://us-central1-python.pkg.dev/cds-artifacts/public-python/simple/"
explicit = true

[tool.uv.sources]
cheesypy = { index = "public-python" }
```

## Quick start

```python
from cheesypy import Cheesemonger

cm = Cheesemonger("https://cheesemonger.internal")   # base URL of the server

cm.list_datasets()                       # -> DataFrame of datasets
meta = cm.metadata("perturb-scuba")      # dims, labels, blocks, datatypes

# ZScore across all response genes for one perturbation at one timepoint
s = cm.series("perturb-scuba", "ZScore",
              Screen="PS-SC-1", Timepoint="D4", Target="23293")
s.head()
```

`cm` holds an HTTP connection; reuse one instance. Constructor options:
`api_key=` (sent as a Bearer token), `auth=` (a `(user, password)` tuple sent as
HTTP Basic, or any `httpx.Auth`), `timeout=` (seconds, default 300), and
`gene_symbols=` (see [Gene symbols](#gene-symbols)). It's also a context manager
(or call `cm.close()`):

```python
# Behind a password-protected proxy (e.g. dmc.depmap.org)
cm = Cheesemonger("https://dmc.depmap.org/cheesemonger",
                  auth=("<username>", "<password>"), gene_symbols=True)
```

```python
with Cheesemonger("https://cheesemonger.internal") as cm:
    df = cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
                   Screen="PS-SC-1", Timepoint="D4", Target="23293")
```

## Concepts

A few terms that shape every query:

- **Dataset** — a named collection sharing one schema (e.g. `perturb-scuba`).
- **Block** — one value of the dataset's *last dimension* (the organizational
  key, usually `Screen`). Each block is one screen's data. In a query you refer
  to it like any other dimension (e.g. `Screen="PS-SC-1"`); omit it to span all
  blocks. Its name is set per dataset and is **case-sensitive** (some datasets
  use `Screen`, others `screen`). Check `cm.metadata(ds)["last_dimension"]`.
- **Dimensions** — the array axes within a block, e.g. `Timepoint`, `Target`
  (perturbed gene), `Response` (measured gene).
- **Datatype** — a measured quantity: `ZScore`, `L2FC`, `FDR`, `MeanDifference`,
  … Query one, or a list to get several at the same coordinates.

A query **fixes** some dimensions (via keyword arguments) and returns the ones
left free. Fix all but one → a vector (`Series`); fix all → a scalar. Passing a
**list** of labels instead of a single one keeps that dimension in the result,
restricted to those labels in that order.

`cm.metadata("perturb-scuba")` shows you the available dimensions, their labels,
the loaded blocks, and the datatypes.

## Discovering labels

Before you can fix a dimension in a query you need to know its valid labels.
`metadata()` gives an overview, but it **truncates** any dimension with more than
100 labels to a small sample (so pulling a dataset's metadata stays cheap). To
get the *complete*, untruncated list of one dimension, use `dimension_labels()`:

```python
cm.dimension_labels("degs-dmc3", "Target")      # every perturbed gene
cm.dimension_labels("degs-dmc3", "Timepoint")   # every timepoint
cm.dimension_labels("degs-dmc3", "Screen")      # the loaded blocks (block key)
```

It returns a plain `list`. Large dimensions can be paged:

```python
cm.dimension_labels("degs-dmc3", "Target", offset=0, limit=1000)   # first 1000
```

With `gene_symbols=True`, gene labels come back as symbols; non-gene labels
(timepoints, screens) pass through unchanged:

```python
cm = Cheesemonger("https://cheesemonger.internal", gene_symbols=True)
cm.dimension_labels("degs-dmc3", "Target")      # -> ['TP53', 'MDM2', ...]
```

## Reading data

All reads accept fixed dimensions as keyword arguments. The block key (`Screen`)
is just one of them.

```python
# 1. Series — one perturbation, all response genes (e.g. a volcano-plot axis)
cm.series("perturb-scuba", "ZScore",
          Screen="PS-SC-1", Timepoint="D4", Target="23293")          # -> Series

# 2. Several datatypes at the same coordinates (the whole volcano in one call)
cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
          Screen="PS-SC-1", Timepoint="D4", Target="23293")          # -> DataFrame

# 3. Mean over a dimension within a screen (one value per response gene)
cm.aggregate("perturb-scuba", "ZScore", over="Target", how="mean",
             Screen="PS-SC-1", Timepoint="D4")                       # -> Series

# 4. Count significant hits per perturbation (FDR < 0.1)
cm.aggregate("perturb-scuba", "FDR", over="Response",
             how="count_lt", threshold=0.1,
             Screen="PS-SC-1", Timepoint="D4")                       # -> Series

# 5. Mean ACROSS screens (omit the block key, aggregate over it)
cm.aggregate("perturb-scuba", "ZScore", over="Screen", how="mean",
             Timepoint="D4", Target="23293")                         # -> Series

# 6. Span multiple screens without aggregating (compare them side by side)
cm.series("perturb-scuba", "ZScore",
          Timepoint="D4", Target="23293")            # -> DataFrame (Screen x Response)

# 7. Lists keep a dimension: a subset of screens, or several targets
cm.series("perturb-scuba", "ZScore", Screen=["PS-SC-1", "PS-SC-2"],
          Timepoint="D4", Target="23293")            # -> DataFrame (Screen x Response)
cm.series("perturb-scuba", "ZScore", Screen="PS-SC-1",
          Timepoint="D4", Target=["23293", "4193"])  # -> DataFrame (Target x Response)

# 8. Treat missing cells as a constant before reducing: a gene measured in only
#    some screens is averaged over ALL screens (missing = 0), not just those
cm.aggregate("perturb-scuba", "nLargeZScoreUp", over="Screen", how="mean",
             fill_value=0, Timepoint="D4")                           # -> Series

# 9. Diagonal: values where two dimensions share a label (self-targeting)
cm.diagonal("perturb-scuba", "L2FC", dims=("Target", "Response"),
            Screen="PS-SC-1", Timepoint="D4")                        # -> Series

# 10. Full control / anything not covered by the helpers
cm.query("perturb-scuba", "ZScore",
         select={"Screen": "PS-SC-1", "Timepoint": "D4"},
         aggregate={"type": "mean", "over": "Target"})
```

`aggregate(how=...)` accepts `mean`, `median`, `min`, `max`, `count` (non-NaN
cells), and `count_lt` / `count_gt` / `abs_gt` (which need `threshold=`).

Need the unprocessed server response? Pass `raw=True` to any read to get the
response dict instead of pandas.

## Filtering by value

`filter()` returns only the cells of a datatype that pass a predicate, with
their coordinates. The comparison runs server-side, so you download the hits
rather than the whole cube. The result is a tidy `DataFrame`: one row per
passing cell, with one column per dimension (including the block key) followed
by the datatypes.

```python
# Significant responses for one target, with effect sizes read alongside
cm.filter("perturb-scuba", "FDR", "<", 0.05,
          datatypes=["L2FC", "ZScore"],       # co-located datatypes to return
          select={"Screen": "PS-SC-1", "Timepoint": "D4", "Target": "23293"})

# Membership: which correlates are in a gene set (symbols work with gene_symbols=True)
cm.filter("ps100_response_corr", "CorrelateResponse", "in", ["TP53", "MDM2"],
          datatypes=["Correlation"], limit=1000)
```

- `op` is one of `>`, `<`, `=`, `>=`, `<=`, `in` (`in` takes a list).
- `datatypes` must share the filtered datatype's dimensions.
- `select` works as in `query()`; omit the block key to search every block.
- `limit` caps the returned rows. The server also enforces a hard cap
  (100,000 rows); pass `raw=True` to see the `truncated` flag and `count`.

## Return types

The free dimensions and number of datatypes determine the shape:

| free dims | datatypes | result |
| --------- | --------- | ------ |
| 0 | 1 | scalar |
| 0 | N | `Series` (indexed by datatype) |
| 1 | 1 | `Series` (indexed by the free dim) |
| 1 | N | `DataFrame` (rows = labels, cols = datatypes) |
| 2 | 1 | `DataFrame` (e.g. screen × response gene) |
| 2 | N | `dict[datatype -> DataFrame]` |
| 3+ | any | raises `CheesemongerError`; use `raw=True` and reshape yourself |

Missing values come back as `NaN`. `filter()` always returns a flat `DataFrame`
(see [Filtering by value](#filtering-by-value)).

## Gene symbols

The server stores entrez IDs. Enable symbol mode to query by gene symbol and get
results indexed by symbol. The mapping is fetched once and cached.

```python
cm = Cheesemonger("https://cheesemonger.internal", gene_symbols=True)
cm.series("perturb-scuba", "ZScore", Screen="PS-SC-1", Target="MDM2")  # MDM2 -> 4193
# -> Series indexed by gene symbol
```

### What gets translated

Only the names the client knows to be gene-typed:

| | default |
| --- | --- |
| `GENE_DIMENSIONS` — dimensions whose **labels** are entrez IDs | `Target`, `Response`, `testedperturbation`, `testedgeneexpression` |
| `GENE_DATATYPES` — datatypes whose string **values** are entrez IDs | `CorrelateTarget`, `CorrelateResponse` |

(`CorrelateTimepoint` is deliberately absent — its values are timepoints.)

Everything else is returned exactly as stored. This matters because an entrez ID
is indistinguishable from any other small integer — entrez `1` is `A1BG`, `2` is
`A2M`, `9` is `NAT1` — so translating every label turned a `Rank` axis of `1..99`
into `A1BG, A2M, A2MP1, 4, 5, …`. Deciding by *name* rather than by whether a
lookup happens to hit is what prevents that.

A name in neither list is left untranslated — the safe answer, since an
untranslated ID is obvious on sight while a rank rewritten as a gene symbol is
not. If a dataset's gene dimension is missing from the list you'll see entrez
IDs where you expected symbols; add the name to `cheesypy.client.GENE_DIMENSIONS`
(or `GENE_DATATYPES`).

`raw=True` skips translation entirely and hands back the server's response dict,
which is the quickest way to see the stored labels.

You can also fetch the raw mapping yourself: `cm.gene_mappings()`.

## Loading data

The client is **read-only** — it cannot create, load, or delete datasets and
blocks. Those are admin operations run with the cheesemonger CLI on the server:

```bash
python -m cheesemonger load --source gs://.../PS-SC-1_degs_broadcast.zarr \
    --dataset perturb-scuba --block PS-SC-1 --create-dataset
python -m cheesemonger delete-block   --dataset perturb-scuba --block PS-SC-1
python -m cheesemonger delete-dataset --dataset perturb-scuba --force
python -m cheesemonger status
```

## Errors

HTTP errors are raised as exceptions carrying the server's message:

```python
from cheesypy import CheesemongerError, DatasetNotFound, QueryError

try:
    cm.series("perturb-scuba", "ZScore", Screen="does-not-exist")
except DatasetNotFound:        # 404
    ...
except QueryError as e:        # 400 / 413 / 422 (bad datatype, label, aggregate,
    print(e)                   # result too large, …): the server's detail message
    print(e.status_code)
# both subclass CheesemongerError
```

A 413 means the query's estimated result exceeds the server's size cap; narrow
it by fixing more dimensions or using lists. In `gene_symbols=True` mode, if a
value you passed wasn't a known symbol and the server rejects it, the message
says so.

The installed version is available as `cheesypy.__version__`.

## Development

```bash
cd clients/cheesypy
uv sync                 # create the env from uv.lock
uv run pytest -q        # tests (httpx MockTransport — no server needed)
uv run ruff check cheesypy tests
```

## Publishing

CI publishes automatically on a version tag (see
`.github/workflows/publish-cheesypy.yml`):

```bash
# bump version in pyproject.toml (CI fails if cheesypy/ changes without a bump), then:
git tag cheesypy-vX.Y.Z
git push origin cheesypy-vX.Y.Z
```

To publish manually (requires `gcloud auth application-default login` with
Artifact Registry Writer on the repo):

```bash
cd clients/cheesypy
uv build
uv run --with twine --with keyrings.google-artifactregistry-auth \
  twine upload \
  --repository-url https://us-central1-python.pkg.dev/cds-artifacts/public-python/ \
  dist/*
```
