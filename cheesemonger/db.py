import sqlite3
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if type(dbapi_connection) is sqlite3.Connection:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # Turn on Write-Ahead Logging to allow reads while writes are in progress
        cursor.execute("PRAGMA journal_mode=WAL;")
        # allow sqlite to use 1GB
        sqlite3_memory_in_kb = 1024 * 1024
        cursor.execute(f"PRAGMA cache_size = -{sqlite3_memory_in_kb}")
        cursor.close()


def get_engine(sqlalchemy_database_url: str):
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


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Session:
    return SessionLocal(settings.sqlalchemy_database_url)
