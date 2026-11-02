from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from cheesemonger.services.gene_mappings import GeneMappingService

from .deps import get_gene_mapping_service

router = APIRouter(tags=["health"])


class GeneMappingStatus(BaseModel):
    loaded: bool = Field(description="True if the mapping has at least one entry.")
    entries: int = Field(description="Number of entrez ID → symbol entries.")
    taiga_id: str = Field(
        description=(
            "Configured Taiga ID. Empty means no mapping is configured; set but "
            "`loaded: false` means it failed to load."
        )
    )


class HealthOut(BaseModel):
    status: str = Field(description='Always `"ok"` when the server is up.')
    gene_mapping: GeneMappingStatus


@router.get("/health", response_model=HealthOut, summary="Health check")
def health(
    gene_mapping: Annotated[GeneMappingService, Depends(get_gene_mapping_service)],
) -> HealthOut:
    """Liveness and readiness check.

    Always returns `status: "ok"` if the process is up. The `gene_mapping` block
    says whether the Taiga mapping loaded at startup. The server still starts if
    Taiga is unavailable; `GET /gene_mappings` then returns `404`:

    - `loaded: false, taiga_id: ""` — no mapping configured.
    - `loaded: false, taiga_id: "…"` — configured but failed to load (check logs / token).
    - `loaded: true` — mapping is serving `entries` genes.
    """
    return HealthOut(
        status="ok",
        gene_mapping=GeneMappingStatus(
            loaded=gene_mapping.is_loaded,
            entries=len(gene_mapping.entries),
            taiga_id=gene_mapping.taiga_id,
        ),
    )
