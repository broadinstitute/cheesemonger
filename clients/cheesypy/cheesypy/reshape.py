"""Turn a Cheesemonger query response into pandas objects.

The response carries an ``index`` (one entry per free dimension, with labels)
and ``data`` (one array per datatype, shaped like the index). This maps that to
the most natural pandas type:

    free dims   datatypes   -> result
    ---------   ---------      ------
    0           1              scalar
    0           N              Series (indexed by datatype)
    1           1              Series (indexed by the free dim)
    1           N              DataFrame (rows = labels, cols = datatypes)
    2           1              DataFrame (rows x cols = the two free dims)
    2           N              dict[datatype -> DataFrame]

NaN/None in the data become NaN in the float result. Use the client's
``raw=True`` to get the plain response dict instead.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .exceptions import CheesemongerError


def response_to_pandas(resp: dict, datatypes: list[str], single: bool) -> Any:
    index = resp["index"]
    data = resp["data"]
    ndim = len(index)

    if ndim == 0:
        if single:
            return data[datatypes[0]]
        return pd.Series({dt: data[dt] for dt in datatypes})

    if ndim == 1:
        idx = pd.Index(index[0]["labels"], name=index[0]["dimension"])
        if single:
            return pd.Series(data[datatypes[0]], index=idx, name=datatypes[0])
        df = pd.DataFrame({dt: data[dt] for dt in datatypes}, index=idx)
        df.columns.name = "datatype"
        return df

    if ndim == 2:
        rows = pd.Index(index[0]["labels"], name=index[0]["dimension"])
        cols = pd.Index(index[1]["labels"], name=index[1]["dimension"])
        if single:
            return pd.DataFrame(data[datatypes[0]], index=rows, columns=cols)
        return {dt: pd.DataFrame(data[dt], index=rows, columns=cols) for dt in datatypes}

    raise CheesemongerError(
        f"Cannot reshape a {ndim}-dimensional result to pandas; pass raw=True "
        f"to get the response dict."
    )
