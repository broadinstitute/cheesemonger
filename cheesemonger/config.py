"""Application settings, loaded from environment variables.

Uses pydantic-settings to read env vars like DATA_DIR, TAIGA_GENE_MAPPING_ID, etc.
The @lru_cache ensures a singleton; the get_settings() wrapper exists so tests
can monkeypatch _get_settings without breaking the cache.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_prefix: str = ""
    data_dir: str = "/mnt/data"
    taiga_gene_mapping_id: str = "" # e.g. internal-26q1-82aa.94/Gene
    taiga_token_path: str = "" # /data2/taiga/token
    thread_pool_size: int = 4


@lru_cache
def _get_settings() -> Settings:
    return Settings()  # pyright: ignore [reportCallIssue]


def get_settings() -> Settings:
    return _get_settings()
