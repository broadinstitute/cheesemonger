PESCA_SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": [4, 7]},
        {"name": "testedperturbation", "labels": ["103", "226", "672"]},
        {"name": "testedgeneexpression", "labels": ["103", "226", "672", "7157"]},
    ],
    "datatypes": [
        {"name": "ZScore", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
        {"name": "L2FC", "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"]},
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
