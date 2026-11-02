"""App factory — creates and configures the FastAPI application.

Called once from main.py. Registers all routers, creates DB tables,
and creates singleton services (stored on app.state).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .api.datasets import router as datasets_router
from .api.filter import router as filter_router
from .api.gene_mappings import router as gene_mappings_router
from .api.health import router as health_router
from .api.query import router as query_router
from .config import Settings
from .schemas.common import InvalidName
from .services.gene_mappings import GeneMappingService
from .services.query import QueryService

_PACKAGE_NAME = "cheesemonger"

logger = logging.getLogger(__name__)

try:
    _VERSION = metadata.version(_PACKAGE_NAME)
except metadata.PackageNotFoundError:
    _VERSION = "0.0.0"

# Rendered at the top of Swagger UI (/docs) and ReDoc (/redoc).
_DESCRIPTION = """
Read-only API for multi-dimensional perturb-seq data stored as xarray/Zarr.

**Data model.** A *dataset* has named *dimensions* (e.g. `Timepoint`, `Target`,
`Response`) and *datatypes* (measured quantities such as `ZScore`, `L2FC`,
`FDR`). Its *last dimension* (the block key, usually `Screen`) splits it into
*blocks*, one per screen. In requests the block key behaves like any other
dimension. Its name is set per dataset and is case-sensitive: read it from
`last_dimension` in `GET /datasets/{dataset}`.

**Selecting.** A `select` entry with a single label fixes that dimension and
drops it from the result; a list of labels keeps it, in the given order; an
omitted dimension is returned in full (for the block key: all blocks).

**Labels.** Gene dimensions are labelled by entrez ID (as strings). Use
`GET /gene_mappings` to translate to and from gene symbols.

**Loading.** Datasets and blocks are created and deleted with the admin CLI
(`python -m cheesemonger load | delete-block | delete-dataset`), never over HTTP.

The Python client [cheesypy](https://github.com/broadinstitute/cheesemonger/tree/main/clients/cheesypy)
wraps these endpoints and returns pandas objects.
"""

_OPENAPI_TAGS = [
    {"name": "datasets", "description": "Discover datasets, their schema, and coordinate labels."},
    {
        "name": "query",
        "description": "Read data: series, aggregation, diagonal (`/query`) "
        "and value filtering (`/filter`).",
    },
    {"name": "gene_mappings", "description": "Entrez ID ↔ gene symbol mapping."},
    {"name": "health", "description": "Liveness and readiness."},
]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup logic runs before `yield`; shutdown logic runs after.
    # Services are created in create_app() and stored on app.state, so
    # they're available here by the time the app starts serving.
    yield
    app.state.query_service.shutdown()


def create_app(settings: Settings) -> FastAPI:
    api_prefix = settings.api_prefix

    app = FastAPI(
        title=_PACKAGE_NAME,
        description=_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        openapi_url=f"{api_prefix}/openapi.json",
        docs_url=f"{api_prefix}/docs",
        redoc_url=f"{api_prefix}/redoc",
        swagger_ui_oauth2_redirect_url=f"{api_prefix}/docs/oauth2-redirect",
        version=_VERSION,
        lifespan=_lifespan,
    )

    def _invalid_name_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.add_exception_handler(InvalidName, _invalid_name_handler)

    def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Return the failure's type and message instead of a bare 500 body.

        Without this, FastAPI answers any unexpected exception with the opaque
        text "Internal Server Error", which tells a client nothing — not even
        whether the fault is in the request or in the data on disk. This is an
        internal, authenticated API, so echoing the exception is worth far more
        than hiding it. The traceback stays server-side (logged with the request
        path); only the one-line summary goes over the wire.
        """
        logger.exception(
            "Unhandled error handling %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal error: {type(exc).__name__}: {exc}"},
        )

    # Starlette sends this response and then re-raises, so uvicorn still logs
    # the failure as a server error.
    app.add_exception_handler(Exception, _unhandled_error_handler)

    # Create all tables on startup (single source of truth in db.init_db).
    from .db import init_db
    init_db(settings.sqlalchemy_database_url)

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
    root_router.include_router(gene_mappings_router)
    root_router.include_router(query_router)
    root_router.include_router(filter_router)

    app.include_router(root_router)

    return app
