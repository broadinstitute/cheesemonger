"""Shared FastAPI dependencies for all routers.

DB sessions are per-request (via get_db). Stateful services that hold
thread pools or caches (QueryService, GeneMappingService) are singletons
created once at startup and stored on app.state.
"""

from __future__ import annotations

from fastapi import Request

from cheesemonger.services.gene_mappings import GeneMappingService
from cheesemonger.services.query import QueryService


def get_query_service(request: Request) -> QueryService:
    return request.app.state.query_service


def get_gene_mapping_service(request: Request) -> GeneMappingService:
    return request.app.state.gene_mapping_service
