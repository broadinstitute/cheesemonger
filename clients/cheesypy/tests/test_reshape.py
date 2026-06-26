import pandas as pd
import pytest

from cheesypy.exceptions import CheesemongerError
from cheesypy.reshape import response_to_pandas


def _resp(index, data, shape):
    return {"blocks": ["B"], "aggregation": None, "shape": shape, "index": index, "data": data}


def test_scalar_single():
    resp = _resp([], {"ZScore": -1.5}, [])
    assert response_to_pandas(resp, ["ZScore"], single=True) == -1.5


def test_scalar_multi():
    resp = _resp([], {"A": 1.0, "B": 2.0}, [])
    out = response_to_pandas(resp, ["A", "B"], single=False)
    assert isinstance(out, pd.Series)
    assert out["A"] == 1.0 and out["B"] == 2.0


def test_series_single():
    resp = _resp([{"dimension": "Response", "labels": ["a", "b", "c"]}],
                 {"ZScore": [1.0, 2.0, 3.0]}, [3])
    out = response_to_pandas(resp, ["ZScore"], single=True)
    assert isinstance(out, pd.Series)
    assert out.name == "ZScore"
    assert out.index.name == "Response"
    assert list(out.index) == ["a", "b", "c"]
    assert out.tolist() == [1.0, 2.0, 3.0]


def test_dataframe_multi_datatype():
    resp = _resp([{"dimension": "Response", "labels": ["a", "b"]}],
                 {"ZScore": [1.0, 2.0], "L2FC": [0.1, 0.2]}, [2])
    out = response_to_pandas(resp, ["ZScore", "L2FC"], single=False)
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == ["ZScore", "L2FC"]
    assert out.index.name == "Response"
    assert out.loc["b", "L2FC"] == 0.2


def test_dataframe_two_dims_single():
    resp = _resp(
        [{"dimension": "screen", "labels": ["S1", "S2"]},
         {"dimension": "Response", "labels": ["a", "b"]}],
        {"ZScore": [[1.0, 2.0], [3.0, 4.0]]}, [2, 2],
    )
    out = response_to_pandas(resp, ["ZScore"], single=True)
    assert isinstance(out, pd.DataFrame)
    assert out.loc["S2", "b"] == 4.0
    assert out.index.name == "screen"
    assert out.columns.name == "Response"


def test_two_dims_multi_datatype_returns_dict():
    resp = _resp(
        [{"dimension": "screen", "labels": ["S1"]},
         {"dimension": "Response", "labels": ["a", "b"]}],
        {"ZScore": [[1.0, 2.0]], "L2FC": [[3.0, 4.0]]}, [1, 2],
    )
    out = response_to_pandas(resp, ["ZScore", "L2FC"], single=False)
    assert set(out) == {"ZScore", "L2FC"}
    assert out["L2FC"].loc["S1", "b"] == 4.0


def test_none_becomes_nan():
    resp = _resp([{"dimension": "Response", "labels": ["a", "b", "c"]}],
                 {"ZScore": [1.0, None, 3.0]}, [3])
    out = response_to_pandas(resp, ["ZScore"], single=True)
    assert pd.isna(out.iloc[1])


def test_three_dims_raises():
    resp = _resp(
        [{"dimension": "x", "labels": ["1"]},
         {"dimension": "y", "labels": ["1"]},
         {"dimension": "z", "labels": ["1"]}],
        {"ZScore": [[[1.0]]]}, [1, 1, 1],
    )
    with pytest.raises(CheesemongerError, match="3-dimensional"):
        response_to_pandas(resp, ["ZScore"], single=True)
