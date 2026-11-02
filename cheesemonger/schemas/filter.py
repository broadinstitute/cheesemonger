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
    datatype: str = Field(description="Datatype to test.", examples=["Correlation"])
    op: FilterOp = Field(
        description="`gt` (>), `lt` (<), `eq` (=), `ge` (>=), `le` (<=), or `in` (membership).",
        examples=["gt"],
    )
    # The union is irreducible because the right operand's type depends on the
    # datatype being filtered — resolved in the engine.
    value: float | str | list[float | str] = Field(
        description=(
            "A single value for `gt`/`lt`/`eq`/`ge`/`le`; a list for `in`. Numbers compare "
            "numerically; strings match string-valued datatypes (e.g. "
            "`CorrelateResponse` `in` a list of entrez IDs)."
        ),
        examples=[0.75],
    )

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
    filter: Filter = Field(description="The predicate a cell must pass to be returned.")
    datatypes: list[str] = Field(
        default_factory=list,
        description=(
            "Extra datatypes to read at each passing cell (e.g. `CorrelateResponse` "
            "alongside `Correlation`). They must have the same dimensions as the "
            "filtered datatype, which is always returned first."
        ),
        examples=[["CorrelateResponse"]],
    )
    select: list[Selection] = Field(
        default=[],
        description=(
            "Applied before filtering, as in `/query`. Omit the block key to search "
            "every block."
        ),
    )
    # The server also enforces a hard cap (MAX_FILTER_ROWS) so an unselective
    # predicate can't return the cube.
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Maximum rows to return. The server caps results at 100,000 rows.",
    )


class FilterOut(BaseModel):
    dimensions: list[str] = Field(
        description="Coordinate columns of the result, with the block key last."
    )
    coords: dict[str, list[str]] = Field(
        description="One list of labels per dimension. Row *i* across all lists is one cell."
    )
    # int before float so count-typed datatypes keep integer values (smart-union
    # would otherwise coerce them to float).
    data: dict[str, list[str | int | float | None]] = Field(
        description="One list of values per returned datatype, parallel to `coords`. NaN is `null`."
    )
    count: int = Field(
        description=(
            "Rows returned (the length of every list). When `truncated` is true this "
            "is the cap, not the total number of matching cells."
        )
    )
    truncated: bool = Field(
        default=False, description="True if `limit` or the server cap cut the result short."
    )
