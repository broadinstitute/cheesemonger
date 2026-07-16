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
              screen="PS-SC-1", Timepoint="D4", Target="23293")
s.head()
```

`cm` holds an HTTP connection; reuse one instance. It's also a context manager:

```python
with Cheesemonger("https://cheesemonger.internal") as cm:
    df = cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
                   screen="PS-SC-1", Timepoint="D4", Target="23293")
```

## Concepts

A few terms that shape every query:

- **Dataset** — a named collection sharing one schema (e.g. `perturb-scuba`).
- **Block** — one value of the dataset's *last dimension* (the organizational
  key, usually `screen`). Each block is one screen's data. In a query you refer
  to it like any other dimension (e.g. `screen="PS-SC-1"`); omit it to span all
  blocks.
- **Dimensions** — the array axes within a block, e.g. `Timepoint`, `Target`
  (perturbed gene), `Response` (measured gene).
- **Datatype** — a measured quantity: `ZScore`, `L2FC`, `FDR`, `MeanDifference`,
  … Query one, or a list to get several at the same coordinates.

A query **fixes** some dimensions (via keyword arguments) and returns the ones
left free. Fix all but one → a vector (`Series`); fix all → a scalar.

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
cm.dimension_labels("degs-dmc3", "screen")      # the loaded blocks (block key)
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

All reads accept fixed dimensions as keyword arguments. The block key (`screen`)
is just one of them.

```python
# 1. Series — one perturbation, all response genes (e.g. a volcano-plot axis)
cm.series("perturb-scuba", "ZScore",
          screen="PS-SC-1", Timepoint="D4", Target="23293")          # -> Series

# 2. Several datatypes at the same coordinates (the whole volcano in one call)
cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
          screen="PS-SC-1", Timepoint="D4", Target="23293")          # -> DataFrame

# 3. Mean over a dimension within a screen (one value per response gene)
cm.aggregate("perturb-scuba", "ZScore", over="Target", how="mean",
             screen="PS-SC-1", Timepoint="D4")                       # -> Series

# 4. Count significant hits per perturbation (FDR < 0.1)
cm.aggregate("perturb-scuba", "FDR", over="Response",
             how="count_lt", threshold=0.1,
             screen="PS-SC-1", Timepoint="D4")                       # -> Series

# 5. Mean ACROSS screens (omit the block key, aggregate over it)
cm.aggregate("perturb-scuba", "ZScore", over="screen", how="mean",
             Timepoint="D4", Target="23293")                         # -> Series

# 6. Span multiple screens without aggregating (compare them side by side)
cm.series("perturb-scuba", "ZScore",
          Timepoint="D4", Target="23293")            # -> DataFrame (screen x Response)

# 7. Diagonal: values where two dimensions share a label (self-targeting)
cm.diagonal("perturb-scuba", "L2FC", dims=("Target", "Response"),
            screen="PS-SC-1", Timepoint="D4")                        # -> Series

# 8. Full control / anything not covered by the helpers
cm.query("perturb-scuba", "ZScore",
         select={"screen": "PS-SC-1", "Timepoint": "D4"},
         aggregate={"type": "mean", "over": "Target"})
```

Need the unprocessed server response? Pass `raw=True` to any read to get the
response dict instead of pandas.

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

Missing values come back as `NaN`.

## Gene symbols

The server stores entrez IDs. Enable symbol mode to query by gene symbol and get
results indexed by symbol; labels not in the mapping (e.g. timepoints) pass
through unchanged. The mapping is fetched once and cached.

```python
cm = Cheesemonger("https://cheesemonger.internal", gene_symbols=True)
cm.series("perturb-scuba", "ZScore", screen="PS-SC-1", Target="MDM2")  # MDM2 -> 4193
# -> Series indexed by gene symbol
```

You can also fetch the raw mapping yourself: `cm.gene_mappings()`.

## Loading data

The client is **read-only** — it cannot create, load, or delete datasets and
blocks. Those are admin operations run with the cheesemonger CLI on the server:

```bash
python -m cheesemonger load --source gs://.../PS-SC-1_degs_broadcast.zarr \
    --dataset perturb-scuba --block PS-SC-1 --create-dataset
python -m cheesemonger delete-block   --dataset perturb-scuba --block PS-SC-1
python -m cheesemonger delete-dataset --dataset perturb-scuba --force
```

## Errors

HTTP errors are raised as exceptions carrying the server's message:

```python
from cheesypy import CheesemongerError, DatasetNotFound, QueryError

try:
    cm.series("perturb-scuba", "ZScore", screen="does-not-exist")
except DatasetNotFound:        # 404
    ...
except QueryError as e:        # 400 / 422 (bad datatype, label, aggregate, …)
    print(e)                   # the server's detail message
# both subclass CheesemongerError
```

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
# bump version in pyproject.toml, then:
git tag cheesypy-v0.1.0
git push origin cheesypy-v0.1.0
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
