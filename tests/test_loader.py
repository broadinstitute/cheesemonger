"""Tests for the CLI block loader (services/loader.py).

Sources are small synthetic xarray-exported Zarr stores written to a temp dir,
so the tests are self-contained (no dependency on data/ or GCS).
"""

import numpy as np
import pytest
import xarray as xr

from cheesemonger.schemas.query import QueryIn, Selection
from cheesemonger.services.dataset import DatasetService
from cheesemonger.services.loader import LoaderError, load_block
from cheesemonger.services.query import QueryService

TP = ["D4", "D7"]
TARGET = ["23293", "55149"]
RESPONSE = ["10", "100", "10000", "10001"]


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


def test_load_creates_dataset_and_block(tmp_path):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")

    summary = load_block(
        source, "perturb-scuba", "PS-SC-1", data_dir,
        last_dimension="screen", create_dataset=True,
    )

    assert summary["dataset"] == "perturb-scuba"
    assert summary["block"] == "PS-SC-1"
    assert summary["dimensions"] == {"Timepoint": 2, "Target": 2, "Response": 4}
    assert set(summary["datatypes"]) == {"ZScore", "L2FC", "nCtrlCells"}

    svc = DatasetService(data_dir)
    assert svc.exists("perturb-scuba")
    schema = svc.get_schema("perturb-scuba")
    assert schema["last_dimension"] == "screen"
    assert {d["name"] for d in schema["dimensions"]} == {"Timepoint", "Target", "Response"}
    # Inferred per-datatype dims preserved.
    zscore = next(d for d in schema["datatypes"] if d["name"] == "ZScore")
    assert zscore["dimensions"] == ["Timepoint", "Target", "Response"]
    assert svc.list_block_names("perturb-scuba") == ["PS-SC-1"]


def test_loaded_block_is_queryable(tmp_path):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, create_dataset=True)

    svc = DatasetService(data_dir)
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
        schema=svc.get_schema("ds1"),
        block_names=svc.list_block_names("ds1"),
        get_block_path=lambda b: svc.get_block_zarr_path("ds1", b),
    )
    assert out.blocks == ["PS-SC-1"]
    assert out.shape == [4]
    assert [lvl.dimension for lvl in out.index] == ["Response"]

    # Cross-check the returned values against the source store directly.
    src = xr.open_zarr(source)
    expected = src["ZScore"].sel(Timepoint="D4", Target="23293").values.tolist()
    src.close()
    assert out.data["ZScore"] == pytest.approx(expected)


def test_load_missing_dataset_without_create_errors(tmp_path):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="does not exist"):
        load_block(source, "nope", "PS-SC-1", data_dir, create_dataset=False)


def test_load_existing_block_requires_overwrite(tmp_path):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, create_dataset=True)

    with pytest.raises(LoaderError, match="already exists"):
        load_block(source, "ds1", "PS-SC-1", data_dir)

    # With overwrite it succeeds.
    summary = load_block(source, "ds1", "PS-SC-1", data_dir, overwrite=True)
    assert summary["block"] == "PS-SC-1"


def test_load_rejects_undeclared_datatype(tmp_path):
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    load_block(source, "ds1", "PS-SC-1", data_dir, create_dataset=True)

    # A second source with an extra, undeclared datatype must be rejected.
    dims = ["Timepoint", "Target", "Response"]
    coords = {"Timepoint": TP, "Target": TARGET, "Response": RESPONSE}
    extra = xr.Dataset(
        {"Mystery": xr.DataArray(np.zeros((2, 2, 4), dtype="float32"), dims=dims, coords=coords)}
    )
    extra_path = tmp_path / "src" / "extra.zarr"
    extra.to_zarr(extra_path, mode="w")

    with pytest.raises(LoaderError, match="not declared"):
        load_block(str(extra_path), "ds1", "PS-SC-2", data_dir)


def test_last_dimension_collision_is_rejected(tmp_path):
    """If last_dimension names an actual store dim, schema inference must fail."""
    source = _source_store(tmp_path)
    data_dir = str(tmp_path / "data")
    with pytest.raises(LoaderError, match="must not be one of the source"):
        load_block(source, "ds1", "PS-SC-1", data_dir,
                   last_dimension="Target", create_dataset=True)
