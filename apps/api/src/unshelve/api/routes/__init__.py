"""The routes of the HTTP API, one module per area.

Every router here is mounted under ``/api/v1`` by ``create_app``. Unless a route says
otherwise, it needs a signed-in user: see ``unshelve.api.deps.current_user``.
"""

from fastapi import APIRouter

from unshelve.api.routes import auth, goals, items, search

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(items.router)
api_router.include_router(goals.router)
api_router.include_router(search.router)
