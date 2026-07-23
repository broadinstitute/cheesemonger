from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field


class Selection(BaseModel):
    dimension: str
    value: int | str


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
