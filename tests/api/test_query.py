"""End-to-end query-engine tests.

Each test creates a dataset via the crud layer (the API is read-only), writes
one or more blocks as xarray-exported Zarr stores directly into the dataset's
blocks/ directory, registers them in the DB, then exercises
POST /datasets/{ds}/query.

Block data is deterministic (arange-based) so aggregation results can be
asserted exactly.
"""

from pathlib import Path

import numpy as np
import xarray as xr

from cheesemonger.crud import dataset as ds_crud
from cheesemonger.schemas.dataset import DatasetIn

TP = [4, 7]
PERT = ["103", "226", "672"]
GENE = ["103", "226", "672", "7157"]

SCHEMA = {
    "name": "pesca",
    "last_dimension": "screen",
    "dimensions": [
        {"name": "timepoint", "labels": TP},
        {"name": "testedperturbation", "labels": PERT},
        {"name": "testedgeneexpression", "labels": GENE},
    ],
    "datatypes": [
        {
            "name": "ZScore",
            "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"],
        },
        {
            "name": "L2FC",
            "dimensions": ["timepoint", "testedperturbation", "testedgeneexpression"],
        },
        {"name": "nCtrlCells", "dimensions": ["timepoint"]},
    ],
}

# Shape (2, 3, 4) — timepoint x perturbation x geneexpression.
# At timepoint=4 (index 0): rows are [[0,1,2,3],[4,5,6,7],[8,9,10,11]].
BASE = np.arange(24).reshape(2, 3, 4).astype("float32")


def _block(zscore: np.ndarray, l2fc: np.ndarray) -> xr.Dataset:
    dims = ["timepoint", "testedperturbation", "testedgeneexpression"]
    coords = {"timepoint": TP, "testedperturbation": PERT, "testedgeneexpression": GENE}
    return xr.Dataset(
        {
            "ZScore": xr.DataArray(zscore, dims=dims, coords=coords),
            "L2FC": xr.DataArray(l2fc, dims=dims, coords=coords),
            "nCtrlCells": xr.DataArray(
                np.array([100.0, 200.0], dtype="float32"),
                dims=["timepoint"],
                coords={"timepoint": TP},
            ),
        }
    )


def _setup(client, settings, db, blocks: dict[str, xr.Dataset]) -> None:
    # Datasets are created via the loader/crud, not the API (which is read-only).
    ds_crud.create_dataset(db, DatasetIn(**SCHEMA))
    for name, ds in blocks.items():
        block_path = Path(settings.data_dir) / "pesca" / "blocks" / name
        block_path.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(str(block_path), mode="w")
        ds_crud.create_block(db, "pesca", name)
    db.commit()


def _query(client, body: dict):
    return client.post("/datasets/pesca/query", json=body)


# --- Happy paths -----------------------------------------------------------


def test_series_query(client, settings, db):
    """Fix screen+timepoint+perturbation; get the gene-expression vector."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
            {"dimension": "testedperturbation", "value": "103"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocks"] == ["SW620"]
    assert body["shape"] == [4]
    assert [lvl["dimension"] for lvl in body["index"]] == ["testedgeneexpression"]
    assert body["data"]["ZScore"] == [0.0, 1.0, 2.0, 3.0]


def test_within_block_mean(client, settings, db):
    """Mean over perturbation at a fixed timepoint -> one value per gene."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "mean", "over": "testedperturbation"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shape"] == [4]
    assert [lvl["dimension"] for lvl in body["index"]] == ["testedgeneexpression"]
    assert body["data"]["ZScore"] == [4.0, 5.0, 6.0, 7.0]


def test_count_lt(client, settings, db):
    """count_lt over gene expression -> one count per perturbation."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "count_lt", "over": "testedgeneexpression", "threshold": 5},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shape"] == [3]
    assert [lvl["dimension"] for lvl in body["index"]] == ["testedperturbation"]
    assert body["data"]["ZScore"] == [4, 1, 0]


def test_within_block_count_gt(client, settings, db):
    """count_gt over gene expression -> one count per perturbation."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "count_gt", "over": "testedgeneexpression", "threshold": 5},
    })
    assert r.status_code == 200, r.text
    # tp=4 rows [0,1,2,3],[4,5,6,7],[8,9,10,11]; count(>5) -> [0, 2, 4]
    assert r.json()["data"]["ZScore"] == [0, 2, 4]


def test_within_block_abs_gt(client, settings, db):
    """abs_gt counts large-magnitude values in either direction."""
    _setup(client, settings, db, {"SW620": _block(BASE - 6, BASE - 6)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "abs_gt", "over": "testedgeneexpression", "threshold": 3},
    })
    assert r.status_code == 200, r.text
    # tp=4 rows [-6,-5,-4,-3],[-2,-1,0,1],[2,3,4,5]; count(|x|>3) -> [3, 0, 2]
    assert r.json()["data"]["ZScore"] == [3, 0, 2]


def test_within_block_min_max_median(client, settings, db):
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})
    sel = [
        {"dimension": "screen", "value": "SW620"},
        {"dimension": "timepoint", "value": 4},
    ]
    cases = {
        "min": [0.0, 4.0, 8.0],
        "max": [3.0, 7.0, 11.0],
        "median": [1.5, 5.5, 9.5],
    }
    for how, expected in cases.items():
        r = _query(client, {
            "datatype": "ZScore", "select": sel,
            "aggregate": {"type": how, "over": "testedgeneexpression"},
        })
        assert r.status_code == 200, r.text
        assert r.json()["data"]["ZScore"] == expected, how


def test_within_block_count_excludes_nan(client, settings, db):
    arr = BASE.copy()
    arr[0, 0, 0] = np.nan  # tp=4, first perturbation, first gene
    _setup(client, settings, db, {"SW620": _block(arr, arr)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "count", "over": "testedgeneexpression"},
    })
    assert r.status_code == 200, r.text
    # First perturbation lost one value to NaN; the rest are full (4).
    assert r.json()["data"]["ZScore"] == [3, 4, 4]


def test_cross_block_max(client, settings, db):
    """max over screen -> element-wise max across blocks."""
    _setup(client, settings, db, {
        "SW620": _block(BASE, BASE),
        "HT29": _block(BASE + 100, BASE + 100),
    })
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "timepoint", "value": 4},
            {"dimension": "testedperturbation", "value": "103"},
        ],
        "aggregate": {"type": "max", "over": "screen"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aggregation"] == "max"
    assert body["data"]["ZScore"] == [100.0, 101.0, 102.0, 103.0]


def test_count_gt_requires_threshold(client, settings, db):
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "aggregate": {"type": "count_gt", "over": "testedgeneexpression"},
    })
    assert r.status_code == 422, r.text
    assert "threshold" in r.json()["detail"]


def test_cross_block_mean(client, settings, db):
    """Omit screen, aggregate over screen -> element-wise mean across blocks."""
    _setup(client, settings, db, {
        "SW620": _block(BASE, BASE),
        "HT29": _block(BASE + 100, BASE + 100),
    })

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "timepoint", "value": 4},
            {"dimension": "testedperturbation", "value": "103"},
        ],
        "aggregate": {"type": "mean", "over": "screen"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["blocks"]) == ["HT29", "SW620"]
    assert body["aggregation"] == "mean"
    assert body["shape"] == [4]
    assert body["data"]["ZScore"] == [50.0, 51.0, 52.0, 53.0]


def test_multi_block_no_aggregation(client, settings, db):
    """Multiple blocks, no cross-block agg -> screen appears in the index."""
    _setup(client, settings, db, {
        "SW620": _block(BASE, BASE),
        "HT29": _block(BASE + 100, BASE + 100),
    })

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "timepoint", "value": 4},
            {"dimension": "testedperturbation", "value": "103"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["index"][0]["dimension"] == "screen"
    assert body["shape"] == [2, 4]
    screens = body["index"][0]["labels"]
    rows = dict(zip(screens, body["data"]["ZScore"], strict=True))
    assert rows["SW620"] == [0.0, 1.0, 2.0, 3.0]
    assert rows["HT29"] == [100.0, 101.0, 102.0, 103.0]


def test_diagonal(client, settings, db):
    """Diagonal over (perturbation, geneexpression) at a fixed timepoint."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "L2FC",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "diagonal": ["testedperturbation", "testedgeneexpression"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [lvl["dimension"] for lvl in body["index"]] == ["label"]
    assert body["index"][0]["labels"] == ["103", "226", "672"]
    assert body["data"]["L2FC"] == [0.0, 5.0, 10.0]


def test_reduced_rank_datatype(client, settings, db):
    """A datatype that spans only timepoint returns a scalar when fixed."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "nCtrlCells",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 7},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shape"] == []
    assert body["data"]["nCtrlCells"] == 200.0


def test_reduced_rank_ignores_inapplicable_selection(client, settings, db):
    """Fixing a dim a reduced-rank datatype lacks is a no-op, not an error.

    This is the unbroadcasted-store case: nCtrlCells spans only [timepoint], so
    fixing testedperturbation (which it doesn't have) is simply ignored.
    """
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "nCtrlCells",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 7},
            {"dimension": "testedperturbation", "value": "226"},  # not a nCtrlCells dim
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shape"] == []
    assert body["data"]["nCtrlCells"] == 200.0


# --- Validation (the bugs that motivated these fixes) ----------------------


def test_datatype_list_rejected(client, settings, db):
    """Multiple datatypes per query are no longer supported; a list is a 422."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": ["ZScore", "nCtrlCells"],
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
    })
    assert r.status_code == 422, r.text


def test_aggregate_over_dim_not_in_datatype_rejected(client, settings, db):
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "nCtrlCells",
        "select": [{"dimension": "screen", "value": "SW620"}],
        "aggregate": {"type": "mean", "over": "testedperturbation"},
    })
    assert r.status_code == 422, r.text


def test_diagonal_with_aggregate_rejected(client, settings, db):
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})

    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "timepoint", "value": 4},
        ],
        "diagonal": ["testedperturbation", "testedgeneexpression"],
        "aggregate": {"type": "mean", "over": "testedperturbation"},
    })
    assert r.status_code == 422, r.text


def test_unknown_selection_value_names_the_value(client, settings, db):
    """A bad label produces a clear error naming the offending dim=value."""
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [
            {"dimension": "screen", "value": "SW620"},
            {"dimension": "testedperturbation", "value": "NOSUCHGENE"},
        ],
    })
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "testedperturbation" in detail and "NOSUCHGENE" in detail


def test_unknown_block_is_404(client, settings, db):
    _setup(client, settings, db, {"SW620": _block(BASE, BASE)})
    r = _query(client, {
        "datatype": "ZScore",
        "select": [{"dimension": "screen", "value": "NOPE"}],
    })
    assert r.status_code == 404, r.text
