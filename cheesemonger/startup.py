"""App factory — creates and configures the FastAPI application.

Called once from main.py. Registers all routers, creates DB tables,
and creates singleton services (stored on app.state).

TODO(security): Add authentication/authorization middleware.
TODO(security): Add rate limiting for expensive query patterns.
TODO(security): Add query resource limits (max blocks per query, memory cap).
"""

from importlib import metadata

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .api.blocks import router as blocks_router
from .api.datasets import router as datasets_router
from .api.gene_mappings import router as gene_mappings_router
from .api.health import router as health_router
from .api.query import router as query_router
from .config import Settings
from .schemas.common import InvalidName
from .services.gene_mappings import GeneMappingService
from .services.query import QueryService

_PACKAGE_NAME = "cheesemonger"

try:
    _VERSION = metadata.version(_PACKAGE_NAME)
except metadata.PackageNotFoundError:
    _VERSION = "0.0.0"


def create_app(settings: Settings) -> FastAPI:
    api_prefix = settings.api_prefix

    app = FastAPI(
        title=_PACKAGE_NAME,
        openapi_url=f"{api_prefix}/openapi.json",
        docs_url=f"{api_prefix}/docs",
        redoc_url=f"{api_prefix}/redoc",
        swagger_ui_oauth2_redirect_url=f"{api_prefix}/docs/oauth2-redirect",
        version=_VERSION,
    )

    def _invalid_name_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.add_exception_handler(InvalidName, _invalid_name_handler)

    # Create all tables on startup (use Alembic migrations in production)
    from .db import get_engine
    from .models.base import Base
    Base.metadata.create_all(bind=get_engine(settings.sqlalchemy_database_url))

    # --- Singleton services (stateful, shared across all requests) ---

    app.state.query_service = QueryService(
        thread_pool_size=settings.thread_pool_size,
    )

    if settings.taiga_gene_mapping_id:
        gene_mapping_svc = GeneMappingService.from_taiga(
            settings.taiga_gene_mapping_id,
            token_path=settings.taiga_token_path,
        )
    else:
        gene_mapping_svc = GeneMappingService.empty()
    app.state.gene_mapping_service = gene_mapping_svc

    # --- Routers ---

    if api_prefix:
        root_router = APIRouter(prefix=api_prefix)
    else:
        root_router = APIRouter()

    root_router.include_router(health_router)
    root_router.include_router(datasets_router)
    root_router.include_router(blocks_router)
    root_router.include_router(gene_mappings_router)
    root_router.include_router(query_router)

    app.include_router(root_router)

    return app
