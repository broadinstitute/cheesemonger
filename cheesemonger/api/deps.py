"""Shared FastAPI dependencies for all routers.

Each dependency is a function that can be used with Depends() to inject
services into route handlers. This keeps routers thin — they validate
HTTP concerns, then delegate to the appropriate service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from cheesemonger.config import Settings, get_settings
from cheesemonger.services.dataset import DatasetService
from cheesemonger.services.gene_mappings import GeneMappingService
from cheesemonger.services.query import QueryService


def get_dataset_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatasetService:
    return DatasetService(settings.data_dir)


def get_query_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> QueryService:
    return QueryService(thread_pool_size=settings.thread_pool_size)


def get_gene_mapping_service(request: Request) -> GeneMappingService:
    """Retrieve the gene mapping service from app state.

    The service is created once at startup (in startup.py) and stored
    on app.state so it's shared across all requests.
    """
    return request.app.state.gene_mapping_service
