from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class GeneMappingService:
    def __init__(self, taiga_id: str, entries: dict[str, str]):
        self.taiga_id = taiga_id
        self.entries = entries

    @classmethod
    def empty(cls) -> GeneMappingService:
        return cls(taiga_id="", entries={})

    @classmethod
    def from_taiga(cls, taiga_id: str, token_path: str = "") -> GeneMappingService:
        """Load gene mapping from Taiga at startup.

        Args:
            taiga_id: Taiga dataset ID (e.g. "internal-26q1-82aa.94/Gene").
            token_path: Path to Taiga token file. If provided, sets
                TAIGA_TOKEN_DIR so taigapy reads the token from that location.
                In Docker, mount the token file and set TAIGA_TOKEN_PATH.
        """
        if token_path:
            # taigapy looks for a token file inside the directory pointed
            # to by TAIGA_TOKEN_DIR (defaults to ~/.taiga). By setting this
            # env var, we redirect it to wherever the file is mounted.
            token_dir = os.path.dirname(os.path.abspath(token_path))
            os.environ["TAIGA_TOKEN_DIR"] = token_dir
            logger.info("Using Taiga token from: %s", token_path)

        try:
            from taigapy import create_taiga_client_v3

            tc = create_taiga_client_v3()
            df = tc.get(taiga_id)
        except Exception:
            logger.exception("Failed to load gene mapping from Taiga: %s", taiga_id)
            return cls.empty()

        entries: dict[str, str] = {}
        if df is not None and not df.empty:
            if df.index.name and "entrez" in df.index.name.lower():
                for idx, row in df.iterrows():
                    symbol = row.iloc[0] if len(row) > 0 else str(idx)
                    entries[str(idx)] = str(symbol)
            elif len(df.columns) >= 2:
                col_id = df.columns[0]
                col_symbol = df.columns[1]
                for _, row in df.iterrows():
                    entries[str(row[col_id])] = str(row[col_symbol])

        logger.info("Loaded %d gene mappings from Taiga: %s", len(entries), taiga_id)
        return cls(taiga_id=taiga_id, entries=entries)

    @property
    def is_loaded(self) -> bool:
        return len(self.entries) > 0
