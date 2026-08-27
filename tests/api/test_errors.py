"""Unexpected failures must report what went wrong.

FastAPI's default 500 body is the bare string "Internal Server Error", which
tells a client nothing about whether its request or the data on disk is at
fault. startup.create_app registers a handler that returns the exception's type
and message as `detail` instead; these tests pin that contract.

TestClient is built with raise_server_exceptions=False because Starlette sends
the handler's response and *then* re-raises the exception (so the server still
logs it) — the default TestClient would surface that re-raise instead of the
response we want to inspect.
"""

import pytest
from fastapi.testclient import TestClient

from cheesemonger.db import get_db


@pytest.fixture()
def raising_client(app, db):
    app.dependency_overrides[get_db] = lambda: db

    @app.get("/_boom")
    def _boom() -> None:
        raise RuntimeError("something specific broke")

    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_unhandled_exception_returns_type_and_message(raising_client):
    r = raising_client.get("/_boom")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "RuntimeError" in detail
    assert "something specific broke" in detail


def test_unhandled_exception_is_logged_with_the_request_path(raising_client, caplog):
    with caplog.at_level("ERROR", logger="cheesemonger.startup"):
        raising_client.get("/_boom")
    assert any("/_boom" in rec.getMessage() for rec in caplog.records)
    # the traceback stays server-side
    assert any(rec.exc_info for rec in caplog.records)
