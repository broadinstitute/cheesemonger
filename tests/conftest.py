import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import cheesemonger.config
from cheesemonger.config import Settings
from cheesemonger.startup import create_app


class MustProvideOverriddenConfig(Exception):
    pass


@pytest.fixture(autouse=True)
def disable_default_config(monkeypatch):
    """Prevent tests from accidentally using real settings."""

    def _must_override():
        raise MustProvideOverriddenConfig(
            "Tests must provide their own settings via the 'settings' fixture."
        )

    monkeypatch.setattr(cheesemonger.config, "_get_settings", _must_override)


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    s = Settings(data_dir=data_dir, taiga_gene_mapping_id="")
    monkeypatch.setattr(cheesemonger.config, "_get_settings", lambda: s)
    return s


@pytest.fixture()
def app(settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)
