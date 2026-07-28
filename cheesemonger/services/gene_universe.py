"""Build the gene *gene_universe* — the validation superset of allowed coordinate
labels for a gene dimension (HGNC entrez IDs + custom tokens like ``Cas9``).

Two sources, mirroring the gene-mapping service:

- **Taiga**: pull a pinned HGNC gene table and take its ``entrez_id`` column,
  normalized to plain string integers (reproducible via the pinned version).
- **Manifest**: a local file (one label per line, or a JSON list) — for tests
  and air-gapped loads where Taiga is unavailable.

Extra tokens (e.g. ``Cas9``) are appended to either source.

``normalize_label`` is the single normalization used for BOTH the gene_universe and
each block's coordinate labels, so the subset check compares apples to apples
(e.g. a Taiga ``entrez_id`` float ``9992.0`` and a stored coord ``"9992"`` both
normalize to ``"9992"``).
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_label(value: object) -> str | None:
    """Coerce a raw id cell to a clean string label, or ``None`` to skip it.

    Mirrors ``gene_mappings._normalize_entrez``: floats/ints like ``9992`` or
    ``9992.0`` become ``"9992"``; non-numeric tokens (``"Cas9"``) pass through;
    empty/NaN become ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        as_float = float(text)
    except ValueError:
        return text  # non-numeric token (e.g. "Cas9")
    if math.isnan(as_float):
        return None
    return str(int(as_float)) if as_float.is_integer() else str(as_float)


def _dedup(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lbl in labels:
        if lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    return out


def _labels_from_manifest(path: str) -> list[str]:
    raw = Path(path).read_text()
    text = raw.strip()
    items = json.loads(text) if text.startswith("[") else raw.splitlines()
    return _dedup([n for n in (normalize_label(i) for i in items) if n is not None])


def _labels_from_taiga(taiga_id: str, token_path: str = "") -> list[str]:
    if token_path:
        os.environ["TAIGA_TOKEN_DIR"] = os.path.dirname(os.path.abspath(token_path))
    from taigapy import create_taiga_client_v3

    tc = create_taiga_client_v3()
    df = tc.get(taiga_id)
    if df is None or df.empty:
        return []
    col = df["entrez_id"] if "entrez_id" in df.columns else df.iloc[:, 0]
    return _dedup([n for n in (normalize_label(v) for v in col) if n is not None])


def build_gene_universe(
    *,
    taiga_id: str = "",
    manifest_path: str = "",
    extras: list[str] | None = None,
    token_path: str = "",
) -> list[str]:
    """Build one gene_universe label list from Taiga or a manifest, plus extras.

    Exactly one of ``taiga_id`` / ``manifest_path`` must be given. Extra tokens
    (e.g. ``"Cas9"``) are appended if not already present.
    """
    if manifest_path:
        labels = _labels_from_manifest(manifest_path)
    elif taiga_id:
        labels = _labels_from_taiga(taiga_id, token_path)
    else:
        raise ValueError("build_gene_universe requires taiga_id or manifest_path")

    extra_norm = [n for n in (normalize_label(e) for e in (extras or [])) if n is not None]
    return _dedup(labels + extra_norm)
