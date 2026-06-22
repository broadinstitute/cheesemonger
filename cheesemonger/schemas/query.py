from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel


class Selection(BaseModel):
    dimension: str
    value: int | str


class AggregateSpec(BaseModel):
    type: Literal["mean", "count_lt"]
    over: str
    threshold: float | None = None


class QueryIn(BaseModel):
    datatype: str | list[str]
    select: list[Selection] = []
    aggregate: AggregateSpec | None = None
    diagonal: tuple[str, str] | None = None


class IndexLevel(BaseModel):
    dimension: str
    labels: Sequence[int | str]


class QueryOut(BaseModel):
    blocks: list[str]
    aggregation: str | None = None
    shape: list[int]
    index: list[IndexLevel]
    data: dict[str, Any]
