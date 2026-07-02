import pytest

from cheesemonger.schemas.common import InvalidName, sanitize_name

PESCA_SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": [4, 7]},
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


def test_list_datasets_empty(client):
    response = client.get("/datasets")
    assert response.status_code == 200
    assert response.json() == {"datasets": []}


def test_create_dataset(client):
    response = client.post("/datasets", json=PESCA_SCHEMA)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "pesca"
    assert body["last_dimension"] == "screen"
    assert body["dimensions"] == 3
    assert body["datatypes"] == 2


def test_create_duplicate_dataset(client):
    client.post("/datasets", json=PESCA_SCHEMA)
    response = client.post("/datasets", json=PESCA_SCHEMA)
    assert response.status_code == 409


def test_get_dataset(client):
    client.post("/datasets", json=PESCA_SCHEMA)
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


def test_delete_empty_dataset(client):
    client.post("/datasets", json=PESCA_SCHEMA)
    response = client.delete("/datasets/pesca")
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    response = client.get("/datasets/pesca")
    assert response.status_code == 404


def test_delete_missing_dataset(client):
    response = client.delete("/datasets/nonexistent")
    assert response.status_code == 404


def test_create_dataset_bad_dimension_ref(client):
    bad_schema = {
        "name": "bad",
        "last_dimension": "screen",
        "dimensions": [{"name": "timepoint", "labels": [4, 7]}],
        "datatypes": [{"name": "ZScore", "dimensions": ["timepoint", "nonexistent"]}],
    }
    response = client.post("/datasets", json=bad_schema)
    assert response.status_code == 400


# --- Sanitization tests ---


def test_create_dataset_path_traversal_name(client):
    """Dataset name with path traversal must be rejected."""
    bad = dict(PESCA_SCHEMA, name="../evil")
    response = client.post("/datasets", json=bad)
    assert response.status_code == 422

    bad2 = dict(PESCA_SCHEMA, name="../../etc")
    response = client.post("/datasets", json=bad2)
    assert response.status_code == 422


def test_create_dataset_dot_name(client):
    """Names that are just '.' or '..' must be rejected."""
    bad = dict(PESCA_SCHEMA, name="..")
    response = client.post("/datasets", json=bad)
    assert response.status_code == 422


def test_create_dataset_slash_in_name(client):
    """Slashes in names must be rejected."""
    bad = dict(PESCA_SCHEMA, name="foo/bar")
    response = client.post("/datasets", json=bad)
    assert response.status_code == 422


def test_create_dataset_space_in_name(client):
    """Spaces in names must be rejected."""
    bad = dict(PESCA_SCHEMA, name="foo bar")
    response = client.post("/datasets", json=bad)
    assert response.status_code == 422


def test_create_dataset_dot_in_name(client):
    """Dots are disallowed (no hidden files, no extension confusion)."""
    bad = dict(PESCA_SCHEMA, name="foo.bar")
    response = client.post("/datasets", json=bad)
    assert response.status_code == 422


def test_create_dataset_valid_names(client):
    """Underscores, hyphens, mixed case, and digit-leading (cell-line) names."""
    for name in ["My-Dataset", "pesca_v2", "ABC", "22Rv1", "786-O"]:
        schema = dict(PESCA_SCHEMA, name=name)
        response = client.post("/datasets", json=schema)
        assert response.status_code == 201, f"Name {name!r} should be valid"


def test_sanitize_name_rejects_traversal():
    """sanitize_name rejects '..' and other unsafe names."""
    for bad_name in ["..", ".", "../etc", "foo/bar", "a b"]:
        with pytest.raises(InvalidName):
            sanitize_name(bad_name)


def test_sanitize_name_allows_cell_line_names():
    """Real cell-line names (digit-leading, hyphens) are valid."""
    for name in ["22Rv1", "786-O", "769-P", "NCI-H460", "SW620"]:
        assert sanitize_name(name) == name
