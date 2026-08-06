"""End-to-end tests for POST /datasets/{dataset}/filter.

Mirrors test_query.py's setup: create a dataset via crud, write blocks as
xarray-exported Zarr, then exercise the filter endpoint. Block data is
deterministic so passing cells can be asserted exactly.
"""

from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.schemas.dataset import DatasetIn

TP = ["4", "7"]
PERT = ["103", "226", "672"]
GENE = ["103", "226", "672", "7157"]
DIMS = ["timepoint", "testedperturbation", "testedgeneexpression"]

SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": TP},
        {"name": "testedperturbation", "labels": PERT},
        {"name": "testedgeneexpression", "labels": GENE},
    ],
    "datatypes": [
        {"name": "ZScore", "dimensions": DIMS},
        {"name": "CorrelateResponse", "dimensions": DIMS},  # string-valued
        {"name": "nHits", "dimensions": DIMS},  # integer-valued
        {"name": "nCtrlCells", "dimensions": ["timepoint"]},  # reduced-rank
    ],
}

# Shape (2, 3, 4). tp=4 (idx0): [[0,1,2,3],[4,5,6,7],[8,9,10,11]];
# tp=7 (idx1): [[12..15],[16..19],[20..23]].
BASE = np.arange(24).reshape(2, 3, 4).astype("float32")
# CorrelateResponse holds a string id per cell = str(flat index).
CORR = np.array([str(i) for i in range(24)]).reshape(2, 3, 4)
# nHits is integer-valued (= flat index) to check dtype is preserved in the response.
NHITS = np.arange(24).reshape(2, 3, 4).astype("int64")


def _block(zscore: np.ndarray) -> xr.Dataset:
    coords = {"timepoint": TP, "testedperturbation": PERT, "testedgeneexpression": GENE}
    return xr.Dataset(
        {
            "ZScore": xr.DataArray(zscore, dims=DIMS, coords=coords),
            "CorrelateResponse": xr.DataArray(CORR, dims=DIMS, coords=coords),
            "nHits": xr.DataArray(NHITS, dims=DIMS, coords=coords),
            "nCtrlCells": xr.DataArray(
                np.array([100.0, 200.0], dtype="float32"),
                dims=["timepoint"], coords={"timepoint": TP},
            ),
        }
    )


def _setup(settings, db, blocks: dict[str, xr.Dataset]) -> None:
    ds_crud.create_dataset(db, DatasetIn(**SCHEMA))
    for name, ds in blocks.items():
        block_path = Path(settings.data_dir) / "pesca" / "blocks" / name
        block_path.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(str(block_path), mode="w")
        ds_crud.create_block(db, "pesca", name)
    db.commit()


def _filter(client, body: dict):
    return client.post("/datasets/pesca/filter", json=body)


# --- Happy paths -----------------------------------------------------------


def test_filter_gt_single_block(client, settings, db):
    """ZScore > 20 at a fixed screen+timepoint returns the passing cells + coords."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 20},
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 7},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # timepoint was fixed (collapsed); screen is a coordinate column (Philip's rec).
    assert body["dimensions"] == ["testedperturbation", "testedgeneexpression", "screen"]
    assert body["count"] == 3
    assert body["data"]["ZScore"] == [21.0, 22.0, 23.0]
    assert body["coords"]["testedperturbation"] == ["672", "672", "672"]
    assert body["coords"]["testedgeneexpression"] == ["226", "672", "7157"]
    assert body["coords"]["screen"] == ["SW620", "SW620", "SW620"]
    assert body["truncated"] is False


def test_filter_returns_extra_datatype(client, settings, db):
    """A co-located datatype is read out at the passing cells alongside the filtered one."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 20},
        "datatypes": ["CorrelateResponse"],
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 7},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["ZScore"] == [21.0, 22.0, 23.0]
    # str(flat index) for tp7,pert2,gene{1,2,3} = 21,22,23
    assert body["data"]["CorrelateResponse"] == ["21", "22", "23"]


def test_filter_in_string_datatype(client, settings, db):
    """`in` on a string datatype (e.g. a gene set) returns matching cells."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "CorrelateResponse", "op": "in", "value": ["5", "10", "23"]},
        "select": [{"dimension": "screen", "value": "SW620"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert body["dimensions"] == [*DIMS, "screen"]
    assert body["coords"]["timepoint"] == ["4", "4", "7"]
    assert body["coords"]["testedperturbation"] == ["226", "672", "672"]
    assert body["coords"]["testedgeneexpression"] == ["226", "672", "7157"]
    assert body["data"]["CorrelateResponse"] == ["5", "10", "23"]


def test_filter_spans_blocks_with_screen_column(client, settings, db):
    """Filtering across blocks tags each record with its block (Screen column)."""
    _setup(settings, db, {"SW620": _block(BASE), "HCT116": _block(BASE + 100)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 120},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Only HCT116 (BASE+100) has values > 120: 121, 122, 123.
    assert body["count"] == 3
    assert set(body["coords"]["screen"]) == {"HCT116"}
    assert body["data"]["ZScore"] == [121.0, 122.0, 123.0]
    assert "screen" in body["dimensions"]


def test_filter_empty_result_keeps_dimensions(client, settings, db):
    """A predicate nothing passes returns count 0 with the right dimension names."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 1000},
        "select": [{"dimension": "screen", "value": "SW620"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body["data"]["ZScore"] == []
    assert body["dimensions"] == [*DIMS, "screen"]


def test_filter_limit_truncates(client, settings, db):
    """A client limit caps rows and flags truncation."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "ge", "value": 0},  # all 24 cells
        "select": [{"dimension": "screen", "value": "SW620"}],
        "limit": 5,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 5
    assert body["truncated"] is True


def test_filter_limit_gathers_codatatype_at_capped_cells(client, settings, db):
    """Under a limit, a co-located datatype is gathered at exactly the capped
    passing cells (pointwise), aligning cell-for-cell with the filtered one."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "ge", "value": 0},  # every cell passes
        "datatypes": ["nHits"],
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "limit": 3,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert body["truncated"] is True
    # First 3 passing cells (row-major) at tp4 = flat idx 0,1,2.
    assert body["data"]["ZScore"] == [0.0, 1.0, 2.0]
    assert body["data"]["nHits"] == [0, 1, 2]  # co-datatype gathered at the SAME cells


def test_filter_preserves_int_dtype(client, settings, db):
    """An integer-valued datatype comes back as ints, not floats."""
    _setup(settings, db, {"SW620": _block(BASE)})

    r = _filter(client, {
        "filter": {"datatype": "nHits", "op": "in", "value": [21, 23]},
        "select": [{"dimension": "screen", "value": "SW620"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["nHits"] == [21, 23]
    assert all(isinstance(v, int) for v in body["data"]["nHits"])


def test_filter_list_select_keeps_dim_and_aligns_extra(client, settings, db):
    """A list selection subsets (keeps) a dim; a co-located datatype aligns to it."""
    _setup(settings, db, {"SW620": _block(BASE)})

    # Fix screen+timepoint, subset perturbation to two, filter ZScore > 20.
    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 20},
        "datatypes": ["nHits"],
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 7},
            {"dimension": "testedperturbation", "value": ["226", "672"]},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Only pert 672 has values > 20 at tp7 (21,22,23); pert 226 maxes at 19.
    assert body["dimensions"] == ["testedperturbation", "testedgeneexpression", "screen"]
    assert body["coords"]["testedperturbation"] == ["672", "672", "672"]
    assert body["data"]["ZScore"] == [21.0, 22.0, 23.0]
    # nHits (int) read at the SAME cells must equal the ZScore values here (both = flat idx).
    assert body["data"]["nHits"] == [21, 22, 23]


# --- Validation ------------------------------------------------------------


def test_filter_in_requires_list(client, settings, db):
    _setup(settings, db, {"SW620": _block(BASE)})
    r = _filter(client, {"filter": {"datatype": "ZScore", "op": "in", "value": 5}})
    assert r.status_code == 422, r.text


def test_filter_scalar_op_rejects_list(client, settings, db):
    _setup(settings, db, {"SW620": _block(BASE)})
    r = _filter(client, {"filter": {"datatype": "ZScore", "op": "gt", "value": [1, 2]}})
    assert r.status_code == 422, r.text


def test_filter_unknown_datatype(client, settings, db):
    _setup(settings, db, {"SW620": _block(BASE)})
    r = _filter(client, {"filter": {"datatype": "Nope", "op": "gt", "value": 1}})
    assert r.status_code == 400, r.text


def test_filter_extra_datatype_must_share_dims(client, settings, db):
    """A returned datatype with different dims than the filtered one is rejected."""
    _setup(settings, db, {"SW620": _block(BASE)})
    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 1},
        "datatypes": ["nCtrlCells"],  # dims [timepoint] != ZScore's dims
    })
    assert r.status_code == 422, r.text


def test_filter_block_key_list_selects_subset(client, settings, db):
    """A list on the block key filters just those blocks; the block key remains a
    coordinate column so records stay attributable to their screen."""
    _setup(settings, db, {
        "SW620": _block(BASE),           # max ZScore 23
        "HCT116": _block(BASE + 100),    # 100..123
        "A549": _block(BASE + 200),      # 200..223 — excluded by the subset
    })
    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 120},
        "select": [{"dimension": "screen", "value": ["SW620", "HCT116"]}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Only HCT116 has values > 120 within the subset; A549 (200s) is excluded.
    assert body["count"] == 3
    assert set(body["coords"]["screen"]) == {"HCT116"}
    assert body["data"]["ZScore"] == [121.0, 122.0, 123.0]
    assert "screen" in body["dimensions"]


def test_filter_block_key_list_unknown_block_404(client, settings, db):
    _setup(settings, db, {"SW620": _block(BASE), "HCT116": _block(BASE)})
    r = _filter(client, {
        "filter": {"datatype": "ZScore", "op": "gt", "value": 1},
        "select": [{"dimension": "screen", "value": ["SW620", "NOPE"]}],
    })
    assert r.status_code == 404, r.text
    assert "NOPE" in r.json()["detail"]


def test_filter_dataset_not_found(client, settings, db):
    r = client.post("/datasets/nope/filter",
                    json={"filter": {"datatype": "ZScore", "op": "gt", "value": 1}})
    assert r.status_code == 404, r.text
