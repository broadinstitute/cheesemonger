from __future__ import annotations

from pydantic import BaseModel, Field


class GeneMappingOut(BaseModel):
    name: str
    taiga_id: str = Field(description="Taiga dataset the mapping was loaded from.")
    entries_count: int = Field(description="Number of entries in `entries`.")
    entries: dict[str, str] = Field(
        description="Entrez ID → gene symbol, e.g. `{\"7157\": \"TP53\"}`."
    )
