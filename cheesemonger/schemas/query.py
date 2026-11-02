from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Selection(BaseModel):
    dimension: str = Field(
        description="Dimension name, including the block key (e.g. `Screen`). Case-sensitive.",
        examples=["Timepoint"],
    )
    # Ints are accepted on the wire (timepoint=4, rank=0) but canonicalized to
    # str here, coordinate labels are stored as strings so the engine only
    # ever branches on scalar-vs-list, never on the label's encoding.
    value: str | list[str] = Field(
        description=(
            "A single label fixes the dimension and drops it from the result. A list "
            "of labels keeps the dimension, restricted to those labels in the given "
            "order; on the block key it selects that subset of blocks. Integers are "
            "accepted and treated as strings."
        ),
        examples=["D4"],
    )

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
    type: AggregationType = Field(
        description=(
            "`mean`, `median`, `min`, `max`: that statistic, ignoring NaN. "
            "`count`: number of non-NaN cells. `count_lt` / `count_gt`: cells below / "
            "above `threshold`. `abs_gt`: cells with absolute value above `threshold`."
        ),
    )
    over: str = Field(
        description=(
            "Dimension to reduce. Use the block key (e.g. `Screen`) to aggregate "
            "across blocks. It must not appear in `select`, except the block key "
            "selected as a list (to aggregate across just those blocks)."
        ),
        examples=["Target"],
    )
    threshold: float | None = Field(
        default=None,
        description="Required for `count_lt`, `count_gt` and `abs_gt`; ignored otherwise.",
    )
    # E.g. fill_value=0 averages a gene over ALL screens instead of only the ones
    # that measured it.
    fill_value: float | None = Field(
        default=None,
        description=(
            "If set, missing (NaN) cells count as this value before reducing, e.g. `0` "
            "to average over all blocks rather than only those that measured a label. "
            "Default `null` ignores missing cells."
        ),
    )


class QueryIn(BaseModel):
    # Always a list, even for one datatype. Reading several quantities at the
    # same coordinates in one request (e.g. L2FC + FDR for a volcano plot) opens
    # each block's store once and shares one response index. The datatypes must
    # share dimensions which is validated in the router.
    datatypes: list[str] = Field(
        min_length=1,
        description=(
            "One or more datatypes to read at the same coordinates. Several datatypes "
            "must have the same dimensions; each comes back under `data`."
        ),
        examples=[["L2FC", "FDR"]],
    )
    select: list[Selection] = Field(
        default=[],
        description="Dimensions to fix or subset. Omitted dimensions are returned in full.",
    )
    aggregate: AggregateSpec | None = Field(
        default=None, description="Reduce one dimension. Cannot be combined with `diagonal`."
    )
    diagonal: tuple[str, str] | None = Field(
        default=None,
        description=(
            "Two dimensions with shared labels (e.g. `[\"Target\", \"Response\"]`). "
            "Returns the cells where their labels are equal, as one dimension."
        ),
    )


class IndexLevel(BaseModel):
    dimension: str = Field(description="Name of a free (non-fixed) dimension.")
    labels: Sequence[str] = Field(description="Labels along this axis of `data`, in order.")


class QueryOut(BaseModel):
    blocks: list[str] = Field(description="Blocks that were read.")
    aggregation: str | None = Field(
        default=None, description="The aggregation `type` applied, if any (e.g. `mean`)."
    )
    shape: list[int] = Field(description="Shape of each array in `data`; one entry per `index`.")
    index: list[IndexLevel] = Field(
        description="One entry per axis of `data`, in axis order. Empty for a scalar result."
    )
    data: dict[str, Any] = Field(
        description=(
            "One nested array (or scalar) per requested datatype, shaped like `index`. "
            "Missing values are `null`."
        )
    )
