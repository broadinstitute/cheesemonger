from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cheesemonger.schemas.gene_mappings import GeneMappingOut
from cheesemonger.services.gene_mappings import GeneMappingService

from .deps import get_gene_mapping_service

router = APIRouter(tags=["gene_mappings"])


@router.get("/gene_mappings", response_model=GeneMappingOut)
def get_gene_mappings(
    svc: Annotated[GeneMappingService, Depends(get_gene_mapping_service)],
) -> GeneMappingOut:
    if not svc.is_loaded:
        raise HTTPException(
            status_code=404,
            detail="No gene mapping has been loaded (server misconfigured or Taiga ID not set)",
        )
    return GeneMappingOut(
        name="gene_mappings",
        taiga_id=svc.taiga_id,
        entries_count=len(svc.entries),
        entries=svc.entries,
    )
