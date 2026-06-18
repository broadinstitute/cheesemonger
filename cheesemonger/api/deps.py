"""Shared FastAPI dependencies for all routers.

All services are singletons created once in create_app() and stored on
app.state. Dependencies here simply retrieve them from the request.
"""

from __future__ import annotations

from fastapi import Request

from cheesemonger.services.dataset import DatasetService
from cheesemonger.services.gene_mappings import GeneMappingService
from cheesemonger.services.query import QueryService


def get_dataset_service(request: Request) -> DatasetService:
    return request.app.state.dataset_service


def get_query_service(request: Request) -> QueryService:
    return request.app.state.query_service


def get_gene_mapping_service(request: Request) -> GeneMappingService:
    return request.app.state.gene_mapping_service
