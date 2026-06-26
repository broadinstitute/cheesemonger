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
    assert captured["body"]["datatype"] == "ZScore"
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
        dt = body["datatype"]
        dts = dt if isinstance(dt, list) else [dt]
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


def test_load_posts_to_blocks():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "dataset": "perturb-scuba", "block": "PS-SC-1",
            "path": "/data/perturb-scuba/blocks/PS-SC-1",
            "dimensions": {"Timepoint": 2, "Target": 2, "Response": 14588},
            "datatypes": ["ZScore", "L2FC"],
        })

    cm = make_client(handler)
    out = cm.load("perturb-scuba", "PS-SC-1", "gs://bucket/PS-SC-1.zarr", create_dataset=True)

    assert captured["path"] == "/datasets/perturb-scuba/blocks"
    assert captured["body"]["source"] == "gs://bucket/PS-SC-1.zarr"
    assert captured["body"]["block"] == "PS-SC-1"
    assert captured["body"]["create_dataset"] is True
    assert out["block"] == "PS-SC-1"


def test_list_datasets_returns_dataframe():
    def handler(request):
        return httpx.Response(200, json={"datasets": [
            {"name": "perturb-scuba", "blocks": 2, "datatypes": 14},
        ]})

    cm = make_client(handler)
    df = cm.list_datasets()
    assert isinstance(df, pd.DataFrame)
    assert df.loc[0, "name"] == "perturb-scuba"
