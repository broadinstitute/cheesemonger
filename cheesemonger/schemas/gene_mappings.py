from __future__ import annotations

from pydantic import BaseModel


class GeneMappingOut(BaseModel):
    name: str
    taiga_id: str
    entries_count: int
    entries: dict[str, str]
