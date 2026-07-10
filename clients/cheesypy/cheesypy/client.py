"""The Cheesemonger HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd

from .exceptions import CheesemongerError, DatasetNotFound, QueryError
from .reshape import response_to_pandas


class Cheesemonger:
    """Client for a Cheesemonger server.

    The server API is read-only: datasets and blocks are created, loaded, and
    deleted with the cheesemonger CLI on the server, not through this client.

    Example:
        >>> cm = Cheesemonger("https://cheesemonger.internal")
        >>> cm.series("perturb-scuba", ["ZScore", "L2FC", "FDR"],
        ...           screen="PS-SC-1", Timepoint="D4", Target="23293")

    Read methods return pandas objects by default (see reshape.py); pass
    ``raw=True`` to get the plain response dict.

    With ``gene_symbols=True`` the client fetches the server's entrez<->symbol
    mapping once, lets you query by gene symbol, and relabels result indexes
    back to symbols. Labels not in the mapping (e.g. timepoints) pass through.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        gene_symbols: bool = False,
        _client: httpx.Client | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = _client or httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )
        self._gene_symbols = gene_symbols
        self._sym2id: dict[str, str] | None = None
        self._id2sym: dict[str, str] | None = None

    # --- lifecycle -------------------------------------------------------

    def __enter__(self) -> Cheesemonger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- low-level -------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            if resp.status_code == 404:
                raise DatasetNotFound(detail, status_code=404)
            if resp.status_code in (400, 422):
                raise QueryError(detail, status_code=resp.status_code)
            raise CheesemongerError(detail, status_code=resp.status_code)
        return resp.json()

    # --- metadata / admin ------------------------------------------------

    def list_datasets(self) -> pd.DataFrame:
        """All datasets on the server as a DataFrame (name, blocks, datatypes)."""
        return pd.DataFrame(self._request("GET", "/datasets")["datasets"])

    def metadata(self, dataset: str) -> dict:
        """Full metadata for a dataset (dimensions, labels, blocks, datatypes)."""
        return self._request("GET", f"/datasets/{dataset}")

    def gene_mappings(self) -> dict:
        """Raw entrez<->symbol mapping payload from the server."""
        return self._request("GET", "/gene_mappings")

    # --- gene symbol translation ----------------------------------------

    def _ensure_maps(self) -> None:
        if self._sym2id is not None:
            return
        entries = self.gene_mappings().get("entries", {})  # {entrez_id: symbol}
        self._id2sym = dict(entries)
        self._sym2id = {sym: gid for gid, sym in entries.items()}

    def _to_id(self, value: int | str) -> int | str:
        if not self._gene_symbols:
            return value
        self._ensure_maps()
        assert self._sym2id is not None
        return self._sym2id.get(str(value), value)

    def _relabel_to_symbols(self, resp: dict) -> None:
        if not self._gene_symbols:
            return
        self._ensure_maps()
        assert self._id2sym is not None
        for level in resp["index"]:
            level["labels"] = [self._id2sym.get(str(lbl), lbl) for lbl in level["labels"]]

    # --- read ------------------------------------------------------------

    def query(
        self,
        dataset: str,
        datatype: str | list[str],
        *,
        select: dict[str, int | str] | None = None,
        aggregate: dict | None = None,
        diagonal: tuple[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        """Run a query (POST /datasets/{dataset}/query).

        Args:
            datatype: One datatype name, or a list for a multi-datatype batch.
            select: ``{dimension: value}`` fixed selections. Include the block
                key (e.g. ``screen``) here to target one block; omit it to span
                all blocks.
            aggregate: ``{"type": "mean"|"count_lt", "over": dim, "threshold": x}``.
            diagonal: ``(dim_a, dim_b)`` to extract the shared-label diagonal.
            raw: Return the response dict instead of pandas.
        """
        body: dict[str, Any] = {"datatype": datatype}
        if select:
            body["select"] = [
                {"dimension": dim, "value": self._to_id(val)} for dim, val in select.items()
            ]
        if aggregate:
            body["aggregate"] = aggregate
        if diagonal:
            body["diagonal"] = list(diagonal)

        resp = self._request("POST", f"/datasets/{dataset}/query", json=body)
        if raw:
            return resp

        self._relabel_to_symbols(resp)
        single = isinstance(datatype, str)
        datatypes = [datatype] if single else list(datatype)
        return response_to_pandas(resp, datatypes, single)

    def series(
        self, dataset: str, datatype: str | list[str], *, raw: bool = False, **select: int | str
    ) -> Any:
        """Series query: fix dimensions via keyword args, read the rest.

        ``cm.series("perturb-scuba", "ZScore", screen="PS-SC-1",
        Timepoint="D4", Target="23293")``
        """
        return self.query(dataset, datatype, select=select or None, raw=raw)

    def aggregate(
        self,
        dataset: str,
        datatype: str | list[str],
        *,
        over: str,
        how: str = "mean",
        threshold: float | None = None,
        raw: bool = False,
        **select: int | str,
    ) -> Any:
        """Aggregate over a dimension (``how`` = ``"mean"`` or ``"count_lt"``).

        Use ``over=<last_dimension>`` (e.g. ``over="screen"``) for cross-screen
        aggregation.
        """
        spec: dict[str, Any] = {"type": how, "over": over}
        if threshold is not None:
            spec["threshold"] = threshold
        return self.query(dataset, datatype, select=select or None, aggregate=spec, raw=raw)

    def diagonal(
        self,
        dataset: str,
        datatype: str,
        *,
        dims: tuple[str, str],
        raw: bool = False,
        **select: int | str,
    ) -> Any:
        """Diagonal query: values where the two ``dims`` share a coordinate label."""
        return self.query(dataset, datatype, select=select or None, diagonal=dims, raw=raw)
