from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Selection(BaseModel):
    dimension: str
    # A scalar fixes the dimension (collapsing it out of the result). A list
    # selects several labels and keeps the dimension in the result, in the given
    # order (like pandas fancy indexing). Lists are not allowed on the block key.
    #
    # Ints are accepted on the wire (timepoint=4, rank=0) but canonicalized to
    # str here, coordinate labels are stored as strings so the engine only
    # ever branches on scalar-vs-list, never on the label's encoding.
    value: str | list[str]

    @field_validator("value", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, (int, str)):
            return str(v)
        return v  # anything else falls through to be rejected by the type


# Aggregation kinds. The threshold-based counts need `threshold`; the rest don't.
AggregationType = Literal[
    "mean", "median", "min", "max", "count", "count_lt", "count_gt", "abs_gt"
]
THRESHOLD_AGGREGATIONS: frozenset[str] = frozenset({"count_lt", "count_gt", "abs_gt"})


class AggregateSpec(BaseModel):
    type: AggregationType
    over: str
    # Required for count_lt / count_gt / abs_gt; ignored by the others.
    threshold: float | None = None


class QueryIn(BaseModel):
    # Always a list, even for one datatype. Reading several quantities at the
    # same coordinates in one request (e.g. L2FC + FDR for a volcano plot) opens
    # each block's store once and shares one response index. The datatypes must
    # share dimensions which is validated in the router.
    datatypes: list[str] = Field(min_length=1)
    select: list[Selection] = []
    aggregate: AggregateSpec | None = None
    diagonal: tuple[str, str] | None = None


class IndexLevel(BaseModel):
    dimension: str
    labels: Sequence[str]


class QueryOut(BaseModel):
    blocks: list[str]
    aggregation: str | None = None
    shape: list[int]
    index: list[IndexLevel]
    data: dict[str, Any]
