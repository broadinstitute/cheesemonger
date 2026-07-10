from __future__ import annotations

import logging
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_entrez(value: object) -> str | None:
    """Coerce a raw entrez id cell to a clean string key, or None to skip it.

    Coordinate labels in the stores are entrez ids, looked up client-side via
    ``str(label)`` — so the map keys must be the plain integer form ("1"), not
    a float ("1.0") or a NaN. HGNC stores entrez_id as float64 with missing
    values, hence the normalization here.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        as_float = float(text)  # normalizes "1.0" / numpy floats to "1"
    except ValueError:
        return text  # non-numeric id (already a plain string)
    if math.isnan(as_float):
        return None
    return str(int(as_float)) if as_float.is_integer() else str(as_float)


def _entries_from_dataframe(df: pd.DataFrame) -> dict[str, str]:
    """Build an {entrez_id: symbol} mapping from a Taiga gene table.

    Column layout is auto-detected so several table shapes work:
      1. HGNC-style named columns (``entrez_id`` + ``symbol``) — the common case.
      2. An entrez-labeled index with the symbol in the first column.
      3. Otherwise, the first two columns as (id, symbol).
    """
    if "entrez_id" in df.columns and "symbol" in df.columns:
        id_col, symbol_col = df["entrez_id"], df["symbol"]
    elif isinstance(df.index.name, str) and "entrez" in df.index.name.lower():
        id_col, symbol_col = df.index.to_series(), df.iloc[:, 0]
    elif len(df.columns) >= 2:
        id_col, symbol_col = df.iloc[:, 0], df.iloc[:, 1]
    else:
        return {}

    entries: dict[str, str] = {}
    for raw_id, raw_symbol in zip(id_col, symbol_col, strict=False):
        entrez = _normalize_entrez(raw_id)
        if entrez is None:
            continue
        symbol = str(raw_symbol).strip()
        if not symbol or symbol.lower() == "nan":
            continue
        entries[entrez] = symbol
    return entries


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
            # Keep the configured id so /health can distinguish "configured but
            # failed to load" (taiga_id set, no entries) from "never configured".
            return cls(taiga_id=taiga_id, entries={})

        entries = _entries_from_dataframe(df) if df is not None and not df.empty else {}

        logger.info("Loaded %d gene mappings from Taiga: %s", len(entries), taiga_id)
        return cls(taiga_id=taiga_id, entries=entries)

    @property
    def is_loaded(self) -> bool:
        return len(self.entries) > 0
