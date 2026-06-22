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
