from __future__ import annotations

from pydantic import BaseModel


class Dimension(BaseModel):
    name: str
    labels: list[int] | list[str]


class DatatypeSpec(BaseModel):
    name: str
    dimensions: list[str]
    dtype: str = "float32"


class ChunkDim(BaseModel):
    name: str
    size: int
