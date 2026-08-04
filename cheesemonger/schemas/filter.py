"""Schemas for value-filtering queries (POST /datasets/{dataset}/filter).

Filtering returns the individual cells of a datatype that pass a predicate
(``Correlation > 0.75``), together with their coordinate labels — a tidy/long
result, one record per passing cell. It runs the comparison inside xarray on
the server, which is far cheaper than shipping the whole hypercube to the
client and filtering there.

The block key (last dimension, e.g. ``Screen``) is treated as just another
coordinate column in the result, so results can span blocks and every record
still says which block it came from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .query import Selection

# Maps the user-facing operators >, <, =, >=, <=, in.
FilterOp = Literal["gt", "lt", "eq", "ge", "le", "in"]


class Filter(BaseModel):
    datatype: str
    op: FilterOp
    # A scalar for gt/lt/eq/ge/le; a list for "in". Numbers compare numerically;
    # strings compare against string-valued datatypes (e.g. CorrelateResponse in
    # [gene set]). The union is irreducible because the right operand's type
    # depends on the datatype being filtered — resolved in the engine.
    value: float | str | list[float | str]

    @model_validator(mode="after")
    def _check_cardinality(self) -> Filter:
        # "in" is the only many-valued operator; the ordering/equality operators
        # take exactly one value. Enforce the coupling here so the engine never
        # has to branch on it.
        if self.op == "in":
            if not isinstance(self.value, list):
                raise ValueError("op 'in' requires a list value")
        elif isinstance(self.value, list):
            raise ValueError(f"op '{self.op}' requires a single value, not a list")
        return self


class FilterIn(BaseModel):
    filter: Filter
    # Datatypes whose values to return at each passing cell. The filtered
    # datatype is always included (first); list co-located datatypes here to
    # read them out alongside (e.g. Correlation + CorrelateResponse). All must
    # share the filtered datatype's dimensions.
    datatypes: list[str] = Field(default_factory=list)
    # Fixed selections applied before filtering (same model as query): a scalar
    # fixes/collapses a dimension, a list subsets it. A single value on the
    # block key targets one block; omit it to span all.
    select: list[Selection] = []
    # Optional client cap on returned rows. The server also enforces a hard cap
    # (see MAX_FILTER_ROWS) so an unselective predicate can't return the cube.
    limit: int | None = Field(default=None, ge=1)


class FilterOut(BaseModel):
    # The coordinate dimensions of the result, including the block key. `coords`
    # has one list per dimension; `data` one list per returned datatype. All
    # lists are parallel (row i is one passing cell). `truncated` is True when a
    # cap cut the result short.
    dimensions: list[str]
    coords: dict[str, list[str]]
    # int before float so count-typed datatypes keep integer values (smart-union
    # would otherwise coerce them to float); str for string datatypes; None for NaN.
    data: dict[str, list[str | int | float | None]]
    # Number of rows actually returned (== the length of every column). When
    # `truncated` is True this is the cap, not the true number of matching cells.
    count: int
    truncated: bool = False
