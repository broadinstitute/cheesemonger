import pytest

from cheesemonger.services.dataset import DatasetService, InvalidName

SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": [4, 7]},
        {"name": "testedperturbation", "labels": ["103", "226", "672"]},
    ],
    "datatypes": [
        {"name": "ZScore", "dimensions": ["timepoint", "testedperturbation"]},
    ],
}


def test_delete_missing_block(client):
    client.post("/datasets", json=SCHEMA)
    response = client.delete("/datasets/pesca/blocks/SW620")
    assert response.status_code == 404


def test_delete_block_traversal_preserves_dataset(client):
    """DELETE .../blocks/.. must not be able to rmtree the dataset itself.

    %2e%2e decodes to '..', a single path segment that routes to {block}.
    Before the name-validation fix this resolved to the dataset directory and
    shutil.rmtree wiped the whole dataset, bypassing the not-empty guard on
    DELETE /datasets/{dataset}.
    """
    client.post("/datasets", json=SCHEMA)

    response = client.delete("/datasets/pesca/blocks/%2e%2e")
    assert response.status_code in (400, 404)

    # The dataset must still be intact.
    assert client.get("/datasets/pesca").status_code == 200


def test_service_rejects_traversal_names(settings):
    """Definitive check at the service layer (independent of HTTP normalization)."""
    ds = DatasetService(settings.data_dir)

    with pytest.raises(InvalidName):
        ds.delete_block("pesca", "..")
    with pytest.raises(InvalidName):
        ds.get_block_zarr_path("pesca", "../../etc")
    with pytest.raises(InvalidName):
        ds.delete_dataset("..")
    with pytest.raises(InvalidName):
        ds.block_exists("pesca", "..")


def test_service_allows_cell_line_block_names(settings):
    """Real cell-line names (digit-leading, hyphens) are valid block names.

    These must be loadable and deletable through the same name rules, so the
    sanitizer must not reject digit-leading or hyphenated identifiers.
    """
    ds = DatasetService(settings.data_dir)
    for name in ["22Rv1", "786-O", "769-P", "NCI-H460", "SW620"]:
        # Should not raise — these resolve to a path inside the dataset.
        assert ds.get_block_zarr_path("pesca", name).name == name


# --- Ingest endpoint: POST /datasets/{dataset}/blocks ---


def _write_source_store(tmp_path, name="src.zarr"):
    """A small broadcasted source store the server can read from local disk."""
    import numpy as np
    import xarray as xr

    dims = ["timepoint", "testedperturbation"]
    coords = {"timepoint": [4, 7], "testedperturbation": ["103", "226", "672"]}
    ds = xr.Dataset(
        {"ZScore": xr.DataArray(np.arange(6).reshape(2, 3).astype("float32"),
                                dims=dims, coords=coords)}
    )
    path = tmp_path / name
    ds.to_zarr(path, mode="w")
    return str(path)


def test_load_block_via_api_creates_and_queries(client, tmp_path):
    source = _write_source_store(tmp_path)

    r = client.post(
        "/datasets/pesca/blocks",
        json={"source": source, "block": "SW620", "create_dataset": True},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["dataset"] == "pesca"
    assert body["block"] == "SW620"
    assert body["dimensions"] == {"timepoint": 2, "testedperturbation": 3}
    assert body["datatypes"] == ["ZScore"]

    # The dataset now exists and the block is queryable end-to-end.
    assert client.get("/datasets/pesca").status_code == 200
    q = client.post("/datasets/pesca/query", json={
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
    })
    assert q.status_code == 200, q.text
    assert q.json()["data"]["ZScore"] == [0.0, 1.0, 2.0]


def test_load_block_existing_requires_overwrite(client, tmp_path):
    source = _write_source_store(tmp_path)
    client.post("/datasets/pesca/blocks",
                json={"source": source, "block": "SW620", "create_dataset": True})

    dup = client.post("/datasets/pesca/blocks", json={"source": source, "block": "SW620"})
    assert dup.status_code == 422
    assert "already exists" in dup.json()["detail"]

    ok = client.post("/datasets/pesca/blocks",
                     json={"source": source, "block": "SW620", "overwrite": True})
    assert ok.status_code == 201


def test_load_block_missing_dataset_without_create(client, tmp_path):
    source = _write_source_store(tmp_path)
    r = client.post("/datasets/pesca/blocks", json={"source": source, "block": "SW620"})
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]


def test_load_block_bad_source(client):
    r = client.post(
        "/datasets/pesca/blocks",
        json={"source": "/nonexistent/path.zarr", "block": "SW620", "create_dataset": True},
    )
    assert r.status_code == 422
    assert "Could not open source" in r.json()["detail"]
