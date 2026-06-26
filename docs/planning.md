# Cheesemonger — Planning & TODOs

A living tracker of outstanding work, deferred decisions, and suggestions. Update
it as items are done (`[x]`), dropped (`~~struck~~` with a reason), or added.

**Status legend:** `[ ]` open · `[~]` in progress · `[x]` done · `[-]` decided not needed

Last reviewed: 2026-06-26.

---

## Security (highest priority before real exposure)

- [ ] **Authentication / authorization.** The API has no auth — anyone who can
  reach it can read, create/delete datasets & blocks, and trigger ingests. The
  `cheesypy` client already sends `Authorization: Bearer <api_key>`; the server
  just ignores it. Plan: a shared-API-key dependency (key in settings; `401`
  when missing/wrong; if unset, stay open for local dev/tests), applied to
  mutating routes (and optionally reads). Pairs with edge auth (oauth2_proxy /
  IAP) on dev.cds.team. See `api/blocks.py` `TODO(security)`.
- [ ] **Ingest source is an SSRF-style surface.** `POST /datasets/{ds}/blocks`
  makes the *server* read a caller-supplied `source` (gs:// or server path) with
  the server's credentials. Once auth exists, gate this; consider restricting
  `source` to an allow-list of URI prefixes (e.g. `gs://cds_perturbseq_*`).
- [ ] **Rate limiting / resource limits** for expensive queries (cross-screen
  aggregation reads everything) and for ingest (large/slow loads can fill disk
  or saturate the thread pool).
- [x] **Path-traversal hardening** for dataset/block names (single `sanitize_name`,
  `SafeName` on bodies, `resolve()` containment on deletes).

## Query engine

- [ ] **Validate selection labels in the router** before calling the engine, so
  bad labels return a clean `422` instead of relying on xarray `KeyError`
  strings. (`api/query.py` top TODO.)
- [ ] **`TODO(unbroadcast)`** — support unbroadcasted stores: skip a selection for
  dims a datatype doesn't have, instead of erroring. Lets us ingest the
  storage-efficient unbroadcasted delivery directly. Until then we load the
  broadcasted form. (`services/query.py`, `services/loader.py`.)
- [ ] **`TODO(perf)` diagonal** — replace the per-label `.sel()` loop with
  vectorized pointwise selection (`da.sel(a=xr.DataArray(common), b=...)`).
- [ ] **`aggregate` field semantics** — the response's top-level `aggregation`
  is only set for cross-screen aggregation; within-screen aggregations leave it
  `null` (by design). Document clearly or populate it for both.
- [x] Multi-datatype batch shape mismatch → `422` (datatypes in a batch must
  share dimensions).
- [x] `aggregate.over` on a fixed/absent dim, and `diagonal`+`aggregate`, → `422`.
- [x] Cross-block `count_lt` `threshold is None` guard.

## Loader / data model

- [ ] **`TODO(per-block-coords)`** — schema dimension labels are dataset-level,
  but screens legitimately differ in their Target/Response label sets. Decide how
  per-block coordinate labels feed the response index for multi-screen datasets.
  (Bella confirmed: screens share Timepoint, but not the Response gene set.)
- [ ] **`TODO(rechunk)`** — honor the dataset's `chunk_shape` on load instead of
  copying the source chunking verbatim.
- [x] CLI loader (`python -m cheesemonger load`) — local + `gs://` sources.
- [x] Server ingest endpoint (`POST /datasets/{ds}/blocks`) reusing the loader.
- [x] **Zarr format pin** — pinned `zarr==3.1.1` to match delivered stores (v3);
  `requires-python>=3.11`.

## Ops / deployment

- [ ] **Deploy to dev.cds.team** via a `cheesemonger` ansible role in
  cds-ansible-configs (see `docs/deployment.md`): systemd + docker, `/data2`
  persistent disk, oauth2_proxy upstream at `/cheesemonger/`.
- [ ] **Async ingest jobs** — `POST .../blocks` is synchronous; for very large
  sources switch to a background job + status poll. (`api/blocks.py` `TODO(async)`.)
- [ ] **Graceful shutdown** for the query engine's `ThreadPoolExecutor` (FastAPI
  `lifespan` hook).
- [x] Dockerfile + image build/push CI to `cds-docker-containers` AR.
- [x] CI: lint/typecheck/test for server and client (`cheesypy`).

## Client (cheesypy)

- [ ] **Artifact Registry publishing needs GCP config** — the publish workflow
  needs repo vars `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_SERVICE_ACCOUNT`
  (or swap to a SA JSON key). Until set, publish manually (`uv build` + twine).
- [ ] **Cross-package integration test** (real client ↔ real server) pinned in CI
  (optional; smoke-tested manually so far).
- [x] `cheesypy` package: query/series/aggregate/diagonal, pandas returns,
  gene-symbol translation, `load()`, typed errors; mocked-transport tests.

## Misc quality / robustness

- [ ] **`loaded_at` uses `st_ctime`** (metadata-change time, not creation; can
  drift). Minor.
- [ ] **Corrupt `schema.json` → 500** in dataset reads (no `json.loads` guard).
  Edge case.
- [ ] **Reconcile 400 vs 422** status codes and document the convention (router
  uses 400 for unknown datatype/dimension, 422 for aggregate/batch/Pydantic).
- [x] Added `ruff` (lint) + CI gates.

---

## Decided / not needed

- [-] **Object storage for serving** — data is served from a Persistent Disk
  mounted to the VM, not from a bucket. GCS is only a *source* for the loader.
- [-] **`eval_type_backport` / Python 3.9–3.10 support** — dropped; the stack
  (zarr 3.1.1, pandas) requires `>=3.11`.
- [-] **Broadcast-on-load flag** — for now we assume broadcasted input and keep
  the unbroadcasted-query support as a TODO instead.
