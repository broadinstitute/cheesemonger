# Cheesemonger

Low-latency REST API for multi-dimensional perturb-seq data (PESCA). Serves xarray-exported Zarr stores from Hyperdisk with ~40ms series queries.

## What this project does

Cheesemonger stores and queries large N-dimensional biological datasets. Each dataset has a schema defining dimensions (deployed data: `Timepoint`, `Target`, `Response`; test fixtures use `timepoint`, `testedperturbation`, `testedgeneexpression`), datatypes (ZScore, L2FC, FDR, etc.), and a "last dimension" (default `Screen`) that serves as the organizational key — stored as folders on disk, not as an array axis.

## Key concepts

- **Last dimension**: The organizational key (default `Screen`, set per dataset via `--last-dimension`; case-sensitive). Each value is a separate Zarr store in its own folder. In queries, it behaves like any other dimension from the client's perspective (scalar = one block, list = subset of blocks, omitted = all blocks).
- **Block**: One value of the last dimension. Physically a folder containing an xarray Dataset exported as Zarr (data variables + coordinate arrays). Read with `xr.open_zarr()`.
- **Datatype/shard**: A measured quantity (ZScore, L2FC, etc.). Each is a data variable in the xarray Dataset. Not a dimension.
- **Gene mapping**: Coordinate labels are entrez IDs. Translation to gene symbols is client-side via `/gene_mappings`.

## Architecture

Layered FastAPI app:

- `api/` — HTTP routers, read-only (`/datasets`, `/query`, `/filter`, `/gene_mappings`, `/health`). No direct disk access. Use `Depends()` for services.
- `services/` — Business logic. Query + filter engine (xarray/Zarr reads via `xr.open_zarr()` + `.sel()`), block loader (`loader.py`), gene universe validation, gene mapping (Taiga). `dataset.py` holds on-disk path helpers.
- `crud/` + `models/` + `db.py` — Dataset/block metadata in SQLite via SQLAlchemy (`SQLALCHEMY_DATABASE_URL`). Services take a session; DB init happens at the composition root (`startup.py`, `__main__.py`).
- `schemas/` — Pydantic models. `*In` for requests, `*Out` for responses. Shared types in `common.py`.
- `config.py` — `pydantic-settings` with `@lru_cache`. Test-friendly via monkeypatch on `_get_settings`.
- `__main__.py` — Admin CLI (`python -m cheesemonger load | delete-block | delete-dataset | status | reconcile`). Writes happen only here, never through the API.
- `clients/cheesypy/` — Separate Python client package with its own env, tests, and version. CI fails if `clients/cheesypy/cheesypy/` changes without a version bump in its `pyproject.toml`.

## Disk layout

```
/mnt/data/{dataset}/
  blocks/
    {block}/              ← xarray Dataset stored as Zarr (v3, zarr==3.1.1)
      zarr.json           ← group metadata
      {datatype}/         ← data variable (Zarr array)
      {dim_name}/         ← coordinate array (written by xarray)
```

Dataset schema (dimensions, labels, datatypes, blocks) lives in the SQLite DB, not on disk.

## Running

```bash
uv run uvicorn cheesemonger.main:app --reload    # dev server
uv run pytest tests/ -v                          # tests
make check                                       # ruff + pyright + pytest (CI gates)
```

## Design docs

- `docs/api_design.md` — Full API spec with 9 query cases
- `docs/data_model.md` — SQLite metadata model + Zarr layout
- `docs/data_storage_design.md` — Storage model, chunking, benchmarks
- `docs/architecture_diagram.md` — Mermaid diagrams
