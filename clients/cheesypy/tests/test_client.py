"""Client tests using httpx.MockTransport — no real server, no server import."""

import json

import httpx
import pandas as pd
import pytest

from cheesypy import Cheesemonger, DatasetNotFound, QueryError


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://test")
    return Cheesemonger("http://test", _client=http, **kwargs)


def test_series_builds_request_and_returns_series():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "blocks": ["PS-SC-1"], "aggregation": None, "shape": [3],
            "index": [{"dimension": "Response", "labels": ["10", "100", "200"]}],
            "data": {"ZScore": [0.1, 0.2, 0.3]},
        })

    cm = make_client(handler)
    out = cm.series("perturb-scuba", "ZScore", screen="PS-SC-1", Timepoint="D4", Target="23293")

    assert captured["path"] == "/datasets/perturb-scuba/query"
    assert captured["body"]["datatypes"] == ["ZScore"]
    assert {"dimension": "screen", "value": "PS-SC-1"} in captured["body"]["select"]
    assert {"dimension": "Target", "value": "23293"} in captured["body"]["select"]
    assert isinstance(out, pd.Series)
    assert out.tolist() == [0.1, 0.2, 0.3]


def test_multi_datatype_returns_dataframe():
    def handler(request):
        return httpx.Response(200, json={
            "blocks": ["PS-SC-1"], "aggregation": None, "shape": [2],
            "index": [{"dimension": "Response", "labels": ["a", "b"]}],
            "data": {"ZScore": [1.0, 2.0], "FDR": [0.01, 0.5]},
        })

    cm = make_client(handler)
    out = cm.series("ds", ["ZScore", "FDR"], screen="S1")
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == ["ZScore", "FDR"]


def test_aggregate_sends_spec():
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured["body"] = body
        dts = body["datatypes"]
        return httpx.Response(200, json={
            "blocks": ["S1"], "aggregation": "mean", "shape": [2],
            "index": [{"dimension": "Response", "labels": ["a", "b"]}],
            "data": {d: [1.0, 2.0] for d in dts},
        })

    cm = make_client(handler)
    cm.aggregate("ds", "ZScore", over="Target", how="mean", screen="S1", Timepoint="D4")
    assert captured["body"]["aggregate"] == {"type": "mean", "over": "Target"}

    cm.aggregate("ds", "FDR", over="Response", how="count_lt", threshold=0.1, screen="S1")
    assert captured["body"]["aggregate"] == {
        "type": "count_lt", "over": "Response", "threshold": 0.1,
    }


def test_diagonal_sends_dims():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "blocks": ["S1"], "aggregation": None, "shape": [2],
            "index": [{"dimension": "label", "labels": ["g1", "g2"]}],
            "data": {"L2FC": [0.0, 1.0]},
        })

    cm = make_client(handler)
    cm.diagonal("ds", "L2FC", dims=("Target", "Response"), screen="S1")
    assert captured["body"]["diagonal"] == ["Target", "Response"]


def test_raw_returns_dict():
    payload = {
        "blocks": ["S1"], "aggregation": None, "shape": [1],
        "index": [{"dimension": "Response", "labels": ["a"]}], "data": {"ZScore": [1.0]},
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    cm = make_client(handler)
    out = cm.series("ds", "ZScore", raw=True, screen="S1")
    assert out == payload


def test_error_mapping():
    def handler(request):
        if request.url.path == "/datasets/missing":
            return httpx.Response(404, json={"detail": "Dataset does not exist"})
        return httpx.Response(422, json={"detail": "Unknown datatype: Nope"})

    cm = make_client(handler)
    with pytest.raises(DatasetNotFound, match="does not exist"):
        cm.metadata("missing")
    with pytest.raises(QueryError, match="Unknown datatype"):
        cm.query("ds", "Nope", select={"screen": "S1"})


def test_gene_symbols_translation():
    captured = {}

    def handler(request):
        if request.url.path == "/gene_mappings":
            return httpx.Response(200, json={
                "name": "gene_mappings", "taiga_id": "x", "entries_count": 2,
                "entries": {"4193": "MDM2", "7157": "TP53"},
            })
        captured["body"] = json.loads(request.content)
        # Response uses entrez ids; client should relabel to symbols.
        return httpx.Response(200, json={
            "blocks": ["S1"], "aggregation": None, "shape": [2],
            "index": [{"dimension": "Response", "labels": ["7157", "999"]}],
            "data": {"ZScore": [0.5, 0.6]},
        })

    cm = make_client(handler, gene_symbols=True)
    out = cm.series("ds", "ZScore", screen="S1", Target="MDM2")

    # Query value translated symbol -> entrez id.
    assert {"dimension": "Target", "value": "4193"} in captured["body"]["select"]
    # Result index relabeled entrez id -> symbol (unknown id passes through).
    assert list(out.index) == ["TP53", "999"]


def test_unknown_symbol_gets_helpful_hint():
    """A value that isn't a known symbol (sent as-is) yields a clear hint,
    while legitimate non-gene labels (timepoints, screen) are not flagged."""
    def handler(request):
        if request.url.path == "/gene_mappings":
            return httpx.Response(200, json={
                "name": "gene_mappings", "taiga_id": "x", "entries_count": 1,
                "entries": {"23293": "SMG6"},
            })
        # Server rejects the untranslated Target value.
        return httpx.Response(422, json={
            "detail": "Selection value(s) not found in dataset: Target='Q9BXS5'"
        })

    cm = make_client(handler, gene_symbols=True)
    with pytest.raises(QueryError) as exc:
        cm.series("perturb-scuba", "ZScore", screen="PS-SC-1", Timepoint="D4", Target="Q9BXS5")

    msg = str(exc.value)
    assert "Q9BXS5" in msg
    assert "not recognized as a gene symbol" in msg
    # Non-gene passthrough labels must NOT be flagged as bad symbols.
    assert "D4" not in msg.split("not recognized")[1]
    assert "PS-SC-1" not in msg.split("not recognized")[1]


def test_known_symbol_is_translated_before_send():
    captured = {}

    def handler(request):
        if request.url.path == "/gene_mappings":
            return httpx.Response(200, json={
                "name": "gene_mappings", "taiga_id": "x", "entries_count": 1,
                "entries": {"23293": "SMG6"},
            })
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "blocks": ["PS-SC-1"], "aggregation": None, "shape": [1],
            "index": [{"dimension": "Response", "labels": ["7157"]}],
            "data": {"ZScore": [1.0]},
        })

    cm = make_client(handler, gene_symbols=True)
    cm.series("perturb-scuba", "ZScore", screen="PS-SC-1", Timepoint="D4", Target="SMG6")
    assert {"dimension": "Target", "value": "23293"} in captured["body"]["select"]


def test_dimension_labels_plain():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json={
            "name": "Timepoint", "size": 2, "labels": ["D4", "D7"],
        })

    cm = make_client(handler)
    out = cm.dimension_labels("perturb-scuba", "Timepoint")
    assert captured["path"] == "/datasets/perturb-scuba/dimensions/Timepoint"
    assert out == ["D4", "D7"]


def test_dimension_labels_translates_to_symbols():
    def handler(request):
        if request.url.path == "/gene_mappings":
            return httpx.Response(200, json={
                "name": "gene_mappings", "taiga_id": "x", "entries_count": 2,
                "entries": {"23293": "SMG6", "55149": "MTPAP"},
            })
        return httpx.Response(200, json={
            "name": "Target", "size": 2, "labels": ["23293", "55149"],
        })

    cm = make_client(handler, gene_symbols=True)
    assert cm.dimension_labels("perturb-scuba", "Target") == ["SMG6", "MTPAP"]


def test_list_datasets_returns_dataframe():
    def handler(request):
        return httpx.Response(200, json={"datasets": [
            {"name": "perturb-scuba", "blocks": 2, "datatypes": 14},
        ]})

    cm = make_client(handler)
    df = cm.list_datasets()
    assert isinstance(df, pd.DataFrame)
    assert df.loc[0, "name"] == "perturb-scuba"
