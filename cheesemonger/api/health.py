from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from cheesemonger.services.gene_mappings import GeneMappingService

from .deps import get_gene_mapping_service

router = APIRouter(tags=["health"])


class GeneMappingStatus(BaseModel):
    loaded: bool
    entries: int
    # The configured Taiga ID. Empty string means no mapping was configured;
    # a non-empty id with loaded=False means it was configured but failed to load.
    taiga_id: str


class HealthOut(BaseModel):
    status: str
    gene_mapping: GeneMappingStatus


@router.get("/health", response_model=HealthOut)
def health(
    gene_mapping: Annotated[GeneMappingService, Depends(get_gene_mapping_service)],
) -> HealthOut:
    # Liveness is always "ok" if the process can answer. The gene_mapping block
    # is a readiness signal: it lets you confirm the Taiga mapping actually
    # loaded at startup (it loads lazily and degrades silently if Taiga fails).
    return HealthOut(
        status="ok",
        gene_mapping=GeneMappingStatus(
            loaded=gene_mapping.is_loaded,
            entries=len(gene_mapping.entries),
            taiga_id=gene_mapping.taiga_id,
        ),
    )
