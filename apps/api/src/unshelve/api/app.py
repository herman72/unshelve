"""Application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute

from unshelve import __version__
from unshelve.api import ops
from unshelve.api.errors import install_error_handlers
from unshelve.api.routes import api_router
from unshelve.config import Settings, get_settings
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.log import configure_logging

API_PREFIX = "/api/v1"


def _operation_id(route: APIRoute) -> str:
    # The handler name becomes the operation id, so generated clients get readable names.
    return route.name


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Unshelve",
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        generate_unique_id_function=_operation_id,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(ops.router, prefix=API_PREFIX)
    app.include_router(api_router, prefix=API_PREFIX)
    return app
