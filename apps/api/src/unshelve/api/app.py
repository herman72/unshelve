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
from unshelve.contracts.protocols import CitingLLM, Embedder
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.log import configure_logging
from unshelve.services import build_embedder, build_model, close_embedder

API_PREFIX = "/api/v1"


def _operation_id(route: APIRoute) -> str:
    # The handler name becomes the operation id, so generated clients get readable names.
    return route.name


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    llm: CitingLLM | None = None,
) -> FastAPI:
    """The API. ``embedder`` and ``llm`` are for the tests; the app builds its own from the
    settings."""
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        # Search makes the vector of each query with the embedder of the stored vectors.
        app.state.embedder = embedder or build_embedder(settings)
        # An overview asks the model through the ledger: the cache, the costs and the budget.
        model = build_model(settings, app.state.session_factory) if llm is None else None
        app.state.llm = llm or model
        yield
        if model is not None:
            await model.aclose()
        if embedder is None:
            await close_embedder(app.state.embedder)
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
