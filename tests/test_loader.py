"""Tests for the CLI block loader (services/loader.py).

Sources are small synthetic xarray-exported Zarr stores written to a temp dir,
so the tests are self-contained (no dependency on data/ or GCS).
"""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.models.base import Base
from cheesemonger.schemas.query import QueryIn, Selection
from cheesemonger.services.loader import (
    LoaderError,
    delete_block,
    delete_dataset,
    load_block,
)
from cheesemonger.services.query import QueryService

TP = ["D4", "D7"]
TARGET = ["23293", "55149"]
RESPONSE = ["10", "100", "10000", "10001"]


@pytest.fixture()
def loader_db(tmp_path):
    """A temporary SQLite DB and session for loader tests."""
    db_path = str(tmp_path / "loader_test.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()


def _source_store(tmp_path, name="PS-SC-1_degs.zarr"):
    """A small broadcasted store: every datatype spans all three dims."""
    dims = ["Timepoint", "Target", "Response"]
    coords = {"Timepoint": TP, "Target": TARGET, "Response": RESPONSE}
    shape = (2, 2, 4)
    rng = np.random.default_rng(0)

    def da(values):
        return xr.DataArray(values, dims=dims, coords=coords)

    ds = xr.Dataset(
        {
            "ZScore": da(rng.standard_normal(shape).astype("float32")),
            "L2FC": da(rng.standard_normal(shape).astype("float32")),
            "nCtrlCells": da(np.full(shape, 2048, dtype="int32")),
        }
    )
    path = tmp_path / "src" / name
    ds.to_zarr(path, mode="w")
    return str(path)


def test_load_creates_dataset_and_block(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")

    summary = load_block(
        source, "perturb-scuba", "PS-SC-1", data_dir,
        db=loader_db,
        last_dimension="screen", create_dataset=True,
    )

    assert summary["dataset"] == "perturb-scuba"
    assert summary["block"] == "PS-SC-1"
    assert summary["dimensions"] == {"Timepoint": 2, "Target": 2, "Response": 4}
    assert set(summary["datatypes"]) == {"ZScore", "L2FC", "nCtrlCells"}

    # Verify via the DB
    assert ds_crud.dataset_exists(loader_db, "perturb-scuba")
    schema = ds_crud.get_schema_dict(loader_db, "perturb-scuba")
    assert schema["last_dimension"] == "screen"
    assert {d["name"] for d in schema["dimensions"]} == {"Timepoint", "Target", "Response"}
    zscore = next(d for d in schema["datatypes"] if d["name"] == "ZScore")
    assert zscore["dimensions"] == ["Timepoint", "Target", "Response"]
    assert ds_crud.list_block_names(loader_db, "perturb-scuba") == ["PS-SC-1"]


def test_loaded_block_is_queryable(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    schema = ds_crud.get_schema_dict(loader_db, "ds1")
    block_names = ds_crud.list_block_names(loader_db, "ds1")
    qs = QueryService(thread_pool_size=2)
    out = qs.execute(
        QueryIn(
            datatype="ZScore",
            select=[
                Selection(dimension="screen", value="PS-SC-1"),
                Selection(dimension="Timepoint", value="D4"),
                Selection(dimension="Target", value="23293"),
            ],
        ),
        schema=schema,
        block_names=block_names,
        get_block_path=lambda b: Path(data_dir) / "ds1" / "blocks" / b,
    )
    assert out.blocks == ["PS-SC-1"]
    assert out.shape == [4]
    assert [lvl.dimension for lvl in out.index] == ["Response"]

    src = xr.open_zarr(source)
    expected = src["ZScore"].sel(Timepoint="D4", Target="23293").values.tolist()
    src.close()
    assert out.data["ZScore"] == pytest.approx(expected)


def test_unbroadcasted_store_loads_and_queries(tmp_path, loader_db):
    """A store with reduced-rank datatypes loads faithfully and is queryable
    even when a query fixes a dimension a datatype doesn't have."""
    dims3 = ["Timepoint", "Target", "Response"]
    coords3 = {"Timepoint": TP, "Target": TARGET, "Response": RESPONSE}
    src = xr.Dataset(
        {
            # Full-rank
            "ZScore": xr.DataArray(
                np.arange(16).reshape(2, 2, 4).astype("float32"), dims=dims3, coords=coords3
            ),
            # Reduced-rank: no Target axis (the unbroadcasted form)
            "CtrlMean": xr.DataArray(
                np.array([[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]], dtype="float32"),
                dims=["Timepoint", "Response"],
                coords={"Timepoint": TP, "Response": RESPONSE},
            ),
        }
    )
    src_path = tmp_path / "src" / "unbroadcast.zarr"
    src.to_zarr(src_path, mode="w")
    data_dir = str(tmp_path / "data")

    load_block(str(src_path), "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    # CtrlMean lacks Target; the schema records that faithfully.
    schema = ds_crud.get_schema_dict(loader_db, "ds1")
    ctrl = next(d for d in schema["datatypes"] if d["name"] == "CtrlMean")
    assert ctrl["dimensions"] == ["Timepoint", "Response"]

    # Querying CtrlMean while fixing Target must succeed (Target is ignored).
    qs = QueryService(thread_pool_size=1)
    out = qs.execute(
        QueryIn(
            datatype="CtrlMean",
            select=[
                Selection(dimension="screen", value="PS-SC-1"),
                Selection(dimension="Timepoint", value="D4"),
                Selection(dimension="Target", value="23293"),  # not a CtrlMean dim
            ],
        ),
        schema=schema,
        block_names=["PS-SC-1"],
        get_block_path=lambda b: Path(data_dir) / "ds1" / "blocks" / b,
    )
    assert [lvl.dimension for lvl in out.index] == ["Response"]
    assert out.data["CtrlMean"] == pytest.approx([1.0, 2.0, 3.0, 4.0])


def test_chunk_shape_is_honored_and_reused(tmp_path, loader_db):
    """--chunk sizes are applied to the Zarr, stored on the dataset, and reused
    by later blocks. Unlisted dims stay whole (one chunk)."""
    source = _source_store(tmp_path)  # dims (Timepoint=2, Target=2, Response=4)
    data_dir = str(tmp_path / "data")

    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True,
               chunk_shape={"Target": 1})

    b1 = xr.open_zarr(str(Path(data_dir) / "ds1" / "blocks" / "PS-SC-1"))
    try:
        # ZScore dims (Timepoint, Target, Response) -> Target chunked to 1,
        # Timepoint and Response left whole.
        assert b1["ZScore"].encoding["chunks"] == (2, 1, 4)
    finally:
        b1.close()

    # Stored on the dataset.
    schema = ds_crud.get_schema_dict(loader_db, "ds1")
    assert schema["chunk_shape"] == [{"name": "Target", "size": 1}]

    # A second block reuses the stored chunking without repeating --chunk.
    load_block(source, "ds1", "PS-SC-2", data_dir, db=loader_db)
    b2 = xr.open_zarr(str(Path(data_dir) / "ds1" / "blocks" / "PS-SC-2"))
    try:
        assert b2["ZScore"].encoding["chunks"] == (2, 1, 4)
    finally:
        b2.close()


def test_status_command_lists_datasets(tmp_path, loader_db, capsys, monkeypatch):
    import argparse

    import cheesemonger.config as cfg
    from cheesemonger.__main__ import _cmd_status
    from cheesemonger.config import Settings

    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    url = f"sqlite:///{tmp_path / 'loader_test.db'}"  # same file loader_db uses
    monkeypatch.setattr(
        cfg, "_get_settings",
        lambda: Settings(
            sqlalchemy_database_url=url, data_dir=data_dir, taiga_gene_mapping_id=""
        ),
    )
    _cmd_status(argparse.Namespace(dataset=None))
    out = capsys.readouterr().out
    assert "1 dataset(s)" in out
    assert "ds1" in out
    assert "PS-SC-1" in out


def test_block_name_dot_is_normalized(tmp_path, loader_db):
    """A screen ID with a dot (PS-SC-000651.GG01) is stored with the dot
    replaced by a hyphen, and delete accepts the raw dotted name too."""
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")

    summary = load_block(
        source, "ds1", "PS-SC-000651.GG01", data_dir, db=loader_db, create_dataset=True
    )
    assert summary["block"] == "PS-SC-000651-GG01"
    assert ds_crud.list_block_names(loader_db, "ds1") == ["PS-SC-000651-GG01"]
    assert (Path(data_dir) / "ds1" / "blocks" / "PS-SC-000651-GG01").exists()

    # Deleting with the raw dotted ID resolves to the normalized block.
    delete_block("ds1", "PS-SC-000651.GG01", data_dir, db=loader_db)
    assert ds_crud.list_block_names(loader_db, "ds1") == []


def test_default_chunking_handles_object_dtype(tmp_path, loader_db):
    """Default (auto) chunking must not crash on object/string variables like
    the correlates store's CorrelateTarget — dask can't byte-size those."""
    dims = ["Timepoint", "Target", "Rank"]
    coords = {"Timepoint": TP, "Target": ["1", "2"], "Rank": [1, 2, 3]}
    ds = xr.Dataset(
        {
            "Correlation": xr.DataArray(
                np.random.default_rng(0).random((2, 2, 3)).astype("float32"),
                dims=dims, coords=coords,
            ),
            "CorrelateTarget": xr.DataArray(
                np.full((2, 2, 3), "g", dtype=object), dims=dims, coords=coords
            ),
        }
    )
    src = tmp_path / "src" / "corr.zarr"
    ds.to_zarr(src, mode="w")
    data_dir = str(tmp_path / "data")

    # No chunk_shape -> the default auto path; must succeed despite the object var.
    summary = load_block(str(src), "corr", "S1", data_dir, db=loader_db, create_dataset=True)
    assert set(summary["datatypes"]) == {"Correlation", "CorrelateTarget"}

    written = xr.open_zarr(str(Path(data_dir) / "corr" / "blocks" / "S1"))
    try:
        assert str(written["CorrelateTarget"].values[0, 0, 0]) == "g"
    finally:
        written.close()


def test_load_missing_dataset_without_create_errors(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="does not exist"):
        load_block(source, "nope", "PS-SC-1", data_dir, db=loader_db, create_dataset=False)


def test_load_existing_block_requires_overwrite(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    with pytest.raises(LoaderError, match="already exists"):
        load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db)

    summary = load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, overwrite=True)
    assert summary["block"] == "PS-SC-1"


def test_load_rejects_undeclared_datatype(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    dims = ["Timepoint", "Target", "Response"]
    coords = {"Timepoint": TP, "Target": TARGET, "Response": RESPONSE}
    extra = xr.Dataset(
        {"Mystery": xr.DataArray(np.zeros((2, 2, 4), dtype="float32"), dims=dims, coords=coords)}
    )
    extra_path = tmp_path / "src" / "extra.zarr"
    extra.to_zarr(extra_path, mode="w")

    with pytest.raises(LoaderError, match="not declared"):
        load_block(str(extra_path), "ds1", "PS-SC-2", data_dir, db=loader_db)


def test_last_dimension_collision_is_rejected(tmp_path, loader_db):
    """If last_dimension names an actual store dim, schema inference must fail."""
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="must not be one of the source"):
        load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db,
                   last_dimension="Target", create_dataset=True)


# --- Deletion --------------------------------------------------------------


def test_delete_block_removes_row_and_dir(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)
    load_block(source, "ds1", "PS-SC-2", data_dir, db=loader_db)

    block_path = Path(data_dir) / "ds1" / "blocks" / "PS-SC-1"
    assert block_path.exists()

    summary = delete_block("ds1", "PS-SC-1", data_dir, db=loader_db)
    assert summary == {"dataset": "ds1", "block": "PS-SC-1", "deleted": True}
    assert not block_path.exists()
    # The dataset and its other block survive.
    assert ds_crud.list_block_names(loader_db, "ds1") == ["PS-SC-2"]
    assert ds_crud.dataset_exists(loader_db, "ds1")


def test_delete_missing_block_or_dataset_errors(tmp_path, loader_db):
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="Dataset 'nope' does not exist"):
        delete_block("nope", "PS-SC-1", data_dir, db=loader_db)

    source = _source_store(tmp_path)
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)
    with pytest.raises(LoaderError, match="Block 'ghost' does not exist"):
        delete_block("ds1", "ghost", data_dir, db=loader_db)


def test_delete_dataset_refuses_when_blocks_remain(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)

    with pytest.raises(LoaderError, match="still has 1 block"):
        delete_dataset("ds1", data_dir, db=loader_db)
    # Nothing was removed.
    assert ds_crud.dataset_exists(loader_db, "ds1")
    assert (Path(data_dir) / "ds1" / "blocks" / "PS-SC-1").exists()


def test_delete_dataset_force_removes_everything(tmp_path, loader_db):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, db=loader_db, create_dataset=True)
    load_block(source, "ds1", "PS-SC-2", data_dir, db=loader_db)

    summary = delete_dataset("ds1", data_dir, db=loader_db, force=True)
    assert summary == {"dataset": "ds1", "deleted": True, "blocks_deleted": 2}
    assert not ds_crud.dataset_exists(loader_db, "ds1")
    assert not (Path(data_dir) / "ds1").exists()


def test_delete_empty_dataset(tmp_path, loader_db):
    from cheesemonger.schemas.dataset import DatasetIn

    ds_crud.create_dataset(loader_db, DatasetIn(
        name="empty", last_dimension="screen",
        dimensions=[{"name": "Timepoint", "labels": TP}],
        datatypes=[{"name": "X", "dimensions": ["Timepoint"]}],
    ))
    loader_db.commit()
    data_dir = str(tmp_path / "data")

    summary = delete_dataset("empty", data_dir, db=loader_db)
    assert summary["blocks_deleted"] == 0
    assert not ds_crud.dataset_exists(loader_db, "empty")


def test_delete_missing_dataset_errors(tmp_path, loader_db):
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="does not exist"):
        delete_dataset("nope", data_dir, db=loader_db)
