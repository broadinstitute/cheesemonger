"""The Cheesemonger HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd

from .exceptions import CheesemongerError, DatasetNotFound, QueryError
from .reshape import filter_to_pandas, response_to_pandas

# User-facing operator spellings mapped to the wire op codes.
_FILTER_OPS = {
    ">": "gt", "<": "lt", "=": "eq", "==": "eq", ">=": "ge", "<=": "le", "in": "in",
    "gt": "gt", "lt": "lt", "eq": "eq", "ge": "ge", "le": "le",
}


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
            # 413 = result too large (query size cap); surface it like other
            # query-level problems so callers catch it with QueryError.
            if resp.status_code in (400, 413, 422):
                raise QueryError(detail, status_code=resp.status_code)
            raise CheesemongerError(detail, status_code=resp.status_code)
        return resp.json()

    # --- metadata / admin ------------------------------------------------

    def list_datasets(self) -> pd.DataFrame:
        """All datasets on the server as a DataFrame (name, blocks, datatypes)."""
        return pd.DataFrame(self._request("GET", "/datasets")["datasets"])

    def metadata(self, dataset: str) -> dict:
        """Full metadata for a dataset (dimensions, labels, blocks, datatypes).

        Note: large dimensions (>100 labels) are truncated to a sample here; use
        ``dimension_labels()`` to get the full list of one dimension.
        """
        return self._request("GET", f"/datasets/{dataset}")

    def dimension_labels(
        self, dataset: str, dim: str, *, offset: int = 0, limit: int | None = None
    ) -> list:
        """All coordinate labels for one dimension (e.g. every timepoint / target /
        response), not truncated. Pass the block-key name (e.g. ``"screen"``) to
        list the loaded blocks. With ``gene_symbols=True``, gene labels are
        translated to symbols; non-gene labels (timepoints, screens) pass through.
        """
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        labels = self._request("GET", f"/datasets/{dataset}/dimensions/{dim}", params=params)[
            "labels"
        ]
        if self._gene_symbols:
            self._ensure_maps()
            assert self._id2sym is not None
            labels = [self._id2sym.get(str(x), x) for x in labels]
        return labels

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

    def _to_ids(self, value: Any) -> Any:
        """Translate a symbol, or a list of symbols, to entrez id(s)."""
        if isinstance(value, (list, tuple)):
            return [self._to_id(v) for v in value]
        return self._to_id(value)

    def _relabel_to_symbols(self, resp: dict) -> None:
        if not self._gene_symbols:
            return
        self._ensure_maps()
        assert self._id2sym is not None
        for level in resp["index"]:
            level["labels"] = [self._id2sym.get(str(lbl), lbl) for lbl in level["labels"]]

    def _relabel_filter(self, resp: dict) -> None:
        # Relabel gene ids to symbols in coordinate columns AND string-valued data
        # columns (e.g. CorrelateResponse), so the flagship `CorrelateResponse in
        # [symbols]` round-trips symbols in and symbols out. Non-gene labels pass
        # through unchanged. (Caveat: a numeric non-gene label that collides with
        # an entrez id — e.g. a small-integer Rank/Timepoint — is relabeled too;
        # this is the same limitation as query()'s index relabeling.)
        if not self._gene_symbols:
            return
        self._ensure_maps()
        assert self._id2sym is not None
        for dim, labels in resp["coords"].items():
            resp["coords"][dim] = [self._id2sym.get(str(lbl), lbl) for lbl in labels]
        for dt, vals in resp["data"].items():
            resp["data"][dt] = [
                self._id2sym.get(v, v) if isinstance(v, str) else v for v in vals
            ]

    # --- read ------------------------------------------------------------

    def query(
        self,
        dataset: str,
        datatype: str | list[str],
        *,
        select: dict[str, int | str | list[int | str]] | None = None,
        aggregate: dict | None = None,
        diagonal: tuple[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        """Run a query (POST /datasets/{dataset}/query).

        Args:
            datatype: One datatype name, or a list for a multi-datatype batch.
            select: ``{dimension: value}`` fixed selections. A value may be a
                single label (fixes the dim, dropping it from the result) or a
                list of labels (keeps the dim, in the given order). The block key
                (e.g. ``screen``) follows the same rule: a single value targets
                one block (dropping the block dim), a list selects that subset of
                blocks (keeping the block dim), and omitting it spans all blocks.
            aggregate: ``{"type": "mean"|"median"|"min"|"max"|"count"|"count_lt"|
                "count_gt"|"abs_gt", "over": dim, "threshold": x}``. ``threshold``
                is required for the ``count_lt``/``count_gt``/``abs_gt`` types.
            diagonal: ``(dim_a, dim_b)`` to extract the shared-label diagonal.
            raw: Return the response dict instead of pandas.
        """
        # The client keeps the convenient str-or-list surface; the server takes
        # a list ("datatypes"), so normalize here.
        single = isinstance(datatype, str)
        datatypes = [datatype] if single else list(datatype)
        body: dict[str, Any] = {"datatypes": datatypes}
        # In gene_symbols mode, track values we couldn't translate (sent as-is)
        # so we can give a clear hint if the server rejects one as an unknown label.
        untranslated: list[tuple[str, int | str]] = []
        if select:
            sel: list[dict[str, Any]] = []
            for dim, val in select.items():
                sel.append({"dimension": dim, "value": self._to_ids(val)})
                if self._gene_symbols and self._sym2id is not None:
                    for v in val if isinstance(val, (list, tuple)) else [val]:
                        if str(v) not in self._sym2id:
                            untranslated.append((dim, v))
            body["select"] = sel
        if aggregate:
            body["aggregate"] = aggregate
        if diagonal:
            body["diagonal"] = list(diagonal)

        try:
            resp = self._request("POST", f"/datasets/{dataset}/query", json=body)
        except QueryError as e:
            # If the server couldn't find a value we passed through untranslated,
            # it was likely meant as a gene symbol but isn't one (e.g. a UniProt
            # accession or a typo). Only values that appear in the server's error
            # are flagged, so legitimate non-gene labels (timepoints, screen) that
            # also pass through untranslated aren't mentioned.
            bad = [f"{dim}={val!r}" for dim, val in untranslated if str(val) in str(e)]
            if bad:
                raise QueryError(
                    f"{e} — {', '.join(bad)} not recognized as a gene symbol, so it was "
                    f"sent as-is. Look up valid symbols with cm.gene_mappings().",
                    status_code=e.status_code,
                ) from e
            raise
        if raw:
            return resp

        self._relabel_to_symbols(resp)
        return response_to_pandas(resp, datatypes, single)

    def series(
        self,
        dataset: str,
        datatype: str | list[str],
        *,
        raw: bool = False,
        **select: int | str | list[int | str],
    ) -> Any:
        """Series query: fix dimensions via keyword args, read the rest.

        A keyword value may be a single label or a list of labels; a list keeps
        that dimension in the result (in the given order)::

            cm.series("perturb-scuba", "ZScore", screen="PS-SC-1",
                      Timepoint="D4", Target="23293")
            cm.series("perturb-scuba", "ZScore", screen="PS-SC-1",
                      Timepoint="D4", Target=["SMG6", "MTPAP"])
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
        **select: int | str | list[int | str],
    ) -> Any:
        """Aggregate over a dimension.

        ``how`` is one of ``mean``, ``median``, ``min``, ``max``, ``count``,
        ``count_lt``, ``count_gt``, ``abs_gt``. ``threshold`` is required for the
        ``count_lt``/``count_gt``/``abs_gt`` variants. Use ``over=<last_dimension>``
        (e.g. ``over="screen"``) for cross-screen aggregation.
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

    def filter(
        self,
        dataset: str,
        datatype: str,
        op: str,
        value: float | int | str | list[float | int | str],
        *,
        datatypes: list[str] | None = None,
        select: dict[str, int | str | list[int | str]] | None = None,
        limit: int | None = None,
        raw: bool = False,
    ) -> Any:
        """Return the cells of ``datatype`` passing a predicate, with coords.

        Runs the comparison server-side (POST /datasets/{dataset}/filter) and
        returns a tidy DataFrame: one row per passing cell, columns are the
        coordinate dimensions (including the block key) then the datatypes.

        Args:
            op: one of ``>``, ``<``, ``=``, ``>=``, ``<=``, ``in`` (or the wire
                codes ``gt/lt/eq/ge/le/in``).
            value: threshold for the ordering/equality ops; a list for ``in``.
                Gene symbols are translated to entrez ids (like ``select``).
            datatypes: extra co-located datatypes to read at the passing cells
                (must share the filtered datatype's dimensions). The filtered
                datatype is always included.
            select: fixed selections applied before filtering (see ``query``).
            limit: cap on returned rows (the server also enforces a hard cap).

        Example:
            >>> cm.filter("ps100_response_corr", "Correlation", ">", 0.75,
            ...           datatypes=["CorrelateResponse"])
        """
        op_code = _FILTER_OPS.get(op)
        if op_code is None:
            raise QueryError(
                f"Unknown filter op {op!r}; use one of >, <, =, >=, <=, in."
            )

        # Track symbols we couldn't translate (sent as-is) so a server rejection
        # can be explained — same courtesy as query().
        untranslated: list[tuple[str, int | str]] = []

        def _note_untranslated(dim: str, val: Any) -> None:
            if self._gene_symbols and self._sym2id is not None:
                for v in val if isinstance(val, (list, tuple)) else [val]:
                    if str(v) not in self._sym2id:
                        untranslated.append((dim, v))

        # Translate gene symbol(s) to ids for string/list values; numbers (a
        # numeric threshold) pass through untouched.
        if isinstance(value, (str, list, tuple)):
            wire_value = self._to_ids(value)
            _note_untranslated(datatype, value)
        else:
            wire_value = value

        body: dict[str, Any] = {
            "filter": {"datatype": datatype, "op": op_code, "value": wire_value},
            "datatypes": list(datatypes or []),
        }
        if select:
            sel: list[dict[str, Any]] = []
            for dim, val in select.items():
                sel.append({"dimension": dim, "value": self._to_ids(val)})
                _note_untranslated(dim, val)
            body["select"] = sel
        if limit is not None:
            body["limit"] = limit

        try:
            resp = self._request("POST", f"/datasets/{dataset}/filter", json=body)
        except QueryError as e:
            bad = [f"{dim}={val!r}" for dim, val in untranslated if str(val) in str(e)]
            if bad:
                raise QueryError(
                    f"{e} — {', '.join(bad)} not recognized as a gene symbol, so it was "
                    f"sent as-is. Look up valid symbols with cm.gene_mappings().",
                    status_code=e.status_code,
                ) from e
            raise
        if raw:
            return resp
        self._relabel_filter(resp)
        return filter_to_pandas(resp)
