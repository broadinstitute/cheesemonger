from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cheesemonger.schemas.gene_mappings import GeneMappingOut
from cheesemonger.services.gene_mappings import GeneMappingService

from .deps import get_gene_mapping_service

router = APIRouter(tags=["gene_mappings"])


@router.get(
    "/gene_mappings",
    response_model=GeneMappingOut,
    summary="Get the entrez ID ↔ gene symbol mapping",
    responses={404: {"description": "No gene mapping is loaded on this server"}},
)
def get_gene_mappings(
    svc: Annotated[GeneMappingService, Depends(get_gene_mapping_service)],
) -> GeneMappingOut:
    """The mapping from entrez ID (the label used for genes everywhere in this
    API) to gene symbol, loaded from Taiga when the server starts. Use it to
    translate symbols to IDs for `select` and IDs back to symbols in results.
    It does not change while the server runs, so fetch it once and cache it.

    Returns `404` if no Taiga ID is configured or the mapping failed to load
    (see `GET /health`).
    """
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
