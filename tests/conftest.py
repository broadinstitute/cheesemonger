import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import cheesemonger.config
from cheesemonger.config import Settings
from cheesemonger.db import get_db
from cheesemonger.models.base import Base
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
    db_path = str(tmp_path / "test.db")
    data_dir = str(tmp_path / "data")
    s = Settings(
        data_dir=data_dir,
        sqlalchemy_database_url=f"sqlite:///{db_path}",
        taiga_gene_mapping_id="",
    )
    monkeypatch.setattr(cheesemonger.config, "_get_settings", lambda: s)
    return s


@pytest.fixture()
def db(settings):
    engine = create_engine(
        settings.sqlalchemy_database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()


@pytest.fixture()
def app(settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture()
def client(app: FastAPI, db: Session):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()
