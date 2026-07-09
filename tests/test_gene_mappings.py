"""Unit tests for gene-mapping parsing (no network / no Taiga)."""

import numpy as np
import pandas as pd

from cheesemonger.services.gene_mappings import (
    _entries_from_dataframe,
    _normalize_entrez,
)


def test_normalize_entrez_variants():
    assert _normalize_entrez(1.0) == "1"
    assert _normalize_entrez(np.float64(7157.0)) == "7157"
    assert _normalize_entrez("503538") == "503538"
    assert _normalize_entrez("1.0") == "1"
    assert _normalize_entrez(float("nan")) is None
    assert _normalize_entrez(None) is None
    assert _normalize_entrez("") is None
    assert _normalize_entrez("nan") is None
    # Non-numeric ids pass through unchanged.
    assert _normalize_entrez("ABC1") == "ABC1"


def test_entries_from_hgnc_shaped_frame():
    # HGNC complete set: default int index, entrez_id as float64 with NaNs,
    # extra columns, symbol may be missing.
    df = pd.DataFrame(
        {
            "hgnc_id": ["HGNC:5", "HGNC:37133", "HGNC:X", "HGNC:Y"],
            "symbol": ["A1BG", "A1BG-AS1", "NOENTREZ", np.nan],
            "name": ["a", "b", "c", "d"],
            "entrez_id": [1.0, 503538.0, np.nan, 7.0],
        }
    )
    # Maps entrez_id -> symbol; rows without an entrez id or symbol are skipped.
    assert _entries_from_dataframe(df) == {"1": "A1BG", "503538": "A1BG-AS1"}


def test_entries_from_entrez_indexed_frame():
    df = pd.DataFrame({"symbol": ["A1BG", "TP53"]}, index=[1, 7157])
    df.index.name = "entrez_id"
    assert _entries_from_dataframe(df) == {"1": "A1BG", "7157": "TP53"}


def test_entries_fallback_first_two_columns():
    df = pd.DataFrame({"id": [1, 2], "sym": ["A", "B"], "other": ["x", "y"]})
    assert _entries_from_dataframe(df) == {"1": "A", "2": "B"}


def test_entries_from_empty_or_single_column():
    assert _entries_from_dataframe(pd.DataFrame({"only": [1, 2]})) == {}
