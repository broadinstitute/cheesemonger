import pytest

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.schemas.common import InvalidName, sanitize_name
from cheesemonger.schemas.dataset import DatasetIn

PESCA_SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": ["4", "7"]},
        {"name": "testedperturbation", "labels": ["103", "226", "672"]},
        {"name": "testedgeneexpression", "labels": ["103", "226", "672", "7157"]},
    ],
    "datatypes": [
        {
            "name": "ZScore",
            "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"],
        },
        {
            "name": "L2FC",
            "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"],
        },
    ],
    "chunk_shape": [
        {"name": "testedperturbation", "size": 1000},
        {"name": "testedgeneexpression", "size": 5000},
    ],
}


def _seed(db, schema=PESCA_SCHEMA):
    """Create a dataset directly (the API is read-only; mutations go via crud/loader)."""
    ds_crud.create_dataset(db, DatasetIn(**schema))
    db.commit()


# --- Read endpoints --------------------------------------------------------


def test_list_datasets_empty(client):
    response = client.get("/datasets")
    assert response.status_code == 200
    assert response.json() == {"datasets": []}


def test_list_datasets(client, db):
    _seed(db)
    response = client.get("/datasets")
    assert response.status_code == 200
    names = [d["name"] for d in response.json()["datasets"]]
    assert names == ["pesca"]


def test_get_dataset(client, db):
    _seed(db)
    response = client.get("/datasets/pesca")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "pesca"
    assert body["last_dimension"] == "screen"
    assert len(body["dimensions"]) == 3
    assert body["blocks"] == []


def test_get_missing_dataset(client):
    response = client.get("/datasets/nonexistent")
    assert response.status_code == 404


# --- Dimension labels ------------------------------------------------------


def test_dimension_labels_full(client, db):
    _seed(db)
    r = client.get("/datasets/pesca/dimensions/testedgeneexpression")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "testedgeneexpression"
    assert body["size"] == 4
    assert body["labels"] == ["103", "226", "672", "7157"]  # full, not truncated


def test_dimension_labels_paging(client, db):
    _seed(db)
    body = client.get(
        "/datasets/pesca/dimensions/testedgeneexpression?offset=1&limit=2"
    ).json()
    assert body["size"] == 4  # total, before paging
    assert body["labels"] == ["226", "672"]


def test_dimension_labels_last_dimension_lists_blocks(client, db):
    _seed(db)
    ds_crud.create_block(db, "pesca", "SW620")
    ds_crud.create_block(db, "pesca", "HT29")
    db.commit()
    r = client.get("/datasets/pesca/dimensions/screen")
    assert r.status_code == 200
    assert r.json()["labels"] == ["HT29", "SW620"]  # sorted block names


def test_dimension_labels_unknown_dim_404(client, db):
    _seed(db)
    assert client.get("/datasets/pesca/dimensions/nope").status_code == 404


def test_dimension_labels_missing_dataset_404(client):
    assert client.get("/datasets/nope/dimensions/timepoint").status_code == 404


# --- Mutations are not exposed over HTTP -----------------------------------


def test_create_and_delete_endpoints_removed(client):
    """Datasets/blocks are managed via the CLI loader, not the API."""
    assert client.post("/datasets", json=PESCA_SCHEMA).status_code == 405
    assert client.delete("/datasets/pesca").status_code == 405
    assert client.post("/datasets/pesca/blocks", json={}).status_code == 404
    assert client.delete("/datasets/pesca/blocks/SW620").status_code == 404


# --- Name sanitization (unit level) ----------------------------------------


def test_sanitize_name_rejects_traversal():
    """sanitize_name rejects '..' and other unsafe names."""
    for bad_name in ["..", ".", "../etc", "foo/bar", "a b", "foo.bar"]:
        with pytest.raises(InvalidName):
            sanitize_name(bad_name)


def test_sanitize_name_allows_cell_line_names():
    """Real cell-line names (digit-leading, hyphens) are valid."""
    for name in ["22Rv1", "786-O", "769-P", "NCI-H460", "SW620"]:
        assert sanitize_name(name) == name


def test_dataset_in_rejects_unsafe_name():
    """The dataset schema still enforces SafeName (used by the loader)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DatasetIn(**dict(PESCA_SCHEMA, name="../evil"))
