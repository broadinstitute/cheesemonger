# Cheesemonger

Low-latency REST API for multi-dimensional perturb-seq data (PESCA). Serves xarray-exported Zarr stores from Hyperdisk with ~40ms series queries.

## What this project does

Cheesemonger stores and queries large N-dimensional biological datasets. Each dataset has a schema defining dimensions (timepoint, testedperturbation, testedgeneexpression), datatypes (ZScore, L2FC, FDR, etc.), and a "last dimension" (e.g. screen) that serves as the organizational key — stored as folders on disk, not as an array axis.

## Key concepts

- **Last dimension**: The organizational key (e.g. "screen"). Each value is a separate Zarr store in its own folder. In queries, it behaves like any other dimension from the client's perspective.
- **Block**: One value of the last dimension. Physically a folder containing an xarray Dataset exported as Zarr (data variables + coordinate arrays). Read with `xr.open_zarr()`.
- **Datatype/shard**: A measured quantity (ZScore, L2FC, etc.). Each is a data variable in the xarray Dataset. Not a dimension.
- **Gene mapping**: Coordinate labels are entrez IDs. Translation to gene symbols is client-side via `/gene_mappings`.

## Architecture

Layered FastAPI app:

- `api/` — HTTP routers. No direct disk access. Use `Depends()` for services.
- `services/` — Business logic. Dataset CRUD (disk-based), query engine (xarray/Zarr reads via `xr.open_zarr()` + `.sel()`), gene mapping (Taiga).
- `schemas/` — Pydantic models. `*In` for requests, `*Out` for responses. Shared types in `common.py`.
- `config.py` — `pydantic-settings` with `@lru_cache`. Test-friendly via monkeypatch on `_get_settings`.

## Disk layout

```
/mnt/data/{dataset}/
  schema.json
  blocks/
    {block}/              ← xarray Dataset stored as Zarr
      .zattrs/.zgroup     ← xarray metadata
      {datatype}/         ← data variable (Zarr array)
      {dim_name}/         ← coordinate array (written by xarray)
```

## Running

```bash
uv run uvicorn cheesemonger.main:app --reload    # dev server
uv run pytest tests/ -v                          # tests
```

## Design docs

- `docs/api_design.md` — Full API spec with 9 query cases
- `docs/data_storage_design.md` — Storage model, chunking, benchmarks
- `docs/architecture_diagram.md` — Mermaid diagrams
