import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if type(dbapi_connection) is sqlite3.Connection:
        cursor = dbapi_connection.cursor()
        # Wait up to 30s for a lock instead of failing immediately. Without
        # this, gunicorn workers booting concurrently race on the WAL switch
        # below (which needs a brief exclusive lock) and the losers crash with
        # "database is locked". Must be set before any locking operation.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Turn on Write-Ahead Logging to allow reads while writes are in progress
        cursor.execute("PRAGMA journal_mode=WAL;")
        # allow sqlite to use 1GB
        sqlite3_memory_in_kb = 1024 * 1024
        cursor.execute(f"PRAGMA cache_size = -{sqlite3_memory_in_kb}")
        cursor.close()


@cache
def get_engine(sqlalchemy_database_url: str) -> Engine:
    # Cached: one Engine (and connection pool) per URL, reused across requests.
    return create_engine(
        sqlalchemy_database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )


def SessionLocal(sqlalchemy_database_url: str) -> Session:
    engine = get_engine(sqlalchemy_database_url)
    session = sessionmaker(
        autoflush=False,
        bind=engine,
        future=True,
    )()
    return session


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[Session]:
    # Generator dependency so FastAPI closes the session after the request.
    db = SessionLocal(settings.sqlalchemy_database_url)
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(sqlalchemy_database_url: str) -> Iterator[Session]:
    """A transactional session scope for callers that own the session.

    Commits on clean exit, rolls back on error, always closes. CLI commands wrap
    their work in this so the loader/services stay pure — they receive a Session
    and never create, commit, or close one.
    """
    db = SessionLocal(sqlalchemy_database_url)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
