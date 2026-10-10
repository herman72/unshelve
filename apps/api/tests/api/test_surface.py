"""The shape of the API: which operations exist, what they are called, how they are grouped.

The web app is generated from this surface. Changing it is a decision, so the expected list
below is edited by hand in the same commit as the route.
"""

from tests.api.support import api_operations, build_app

# (method, path, operation id). The operation id is the handler's name, and it becomes the
# name of the call in generated clients.
EXPECTED_SURFACE = {
    # Probes
    ("GET", "/api/v1/healthz", "healthz"),
    ("GET", "/api/v1/readyz", "readyz"),
    # Auth
    ("POST", "/api/v1/auth/magic/consume", "consume_magic_link"),
    ("POST", "/api/v1/auth/logout", "logout"),
    ("GET", "/api/v1/me", "get_me"),
    # Items
    ("GET", "/api/v1/items", "list_items"),
    ("POST", "/api/v1/items", "create_item"),
    ("GET", "/api/v1/items/{item_id}", "get_item"),
    ("PATCH", "/api/v1/items/{item_id}", "update_item"),
    ("POST", "/api/v1/items/{item_id}/transition", "transition_item"),
    ("GET", "/api/v1/items/{item_id}/public-preview", "get_public_preview"),
    ("PUT", "/api/v1/items/{item_id}/visibility", "set_item_visibility"),
    ("POST", "/api/v1/items/{item_id}/reprocess", "reprocess_item"),
    ("GET", "/api/v1/items/{item_id}/events", "list_item_events"),
    ("GET", "/api/v1/items/{item_id}/similar", "list_similar_items"),
    ("PUT", "/api/v1/items/{item_id}/goals/{goal_id}/label", "set_goal_label"),
    # Goals
    ("GET", "/api/v1/goals", "list_goals"),
    ("POST", "/api/v1/goals", "create_goal"),
    ("PATCH", "/api/v1/goals/{goal_id}", "update_goal"),
    ("POST", "/api/v1/goals/{goal_id}/focus", "focus_goal"),
    # Search
    ("GET", "/api/v1/search", "search_items"),
    ("POST", "/api/v1/search/overview", "create_overview"),
    # Resurface
    ("GET", "/api/v1/queue", "get_queue"),
    ("GET", "/api/v1/digests", "list_digests"),
    ("POST", "/api/v1/digests/preview", "preview_digest"),
    # Admin
    ("GET", "/api/v1/tags", "list_tags"),
    ("GET", "/api/v1/stats", "get_stats"),
    ("GET", "/api/v1/admin/jobs", "list_jobs"),
    ("POST", "/api/v1/admin/jobs/{job_id}/retry", "retry_job"),
}


def test_the_api_surface_is_the_expected_one() -> None:
    surface = {(op.method, op.path, op.operation_id) for op in api_operations(build_app())}

    assert surface == EXPECTED_SURFACE


def test_operation_ids_are_unique() -> None:
    operation_ids = [op.operation_id for op in api_operations(build_app())]

    assert len(operation_ids) == len(set(operation_ids))


def test_every_route_has_exactly_one_tag() -> None:
    for op in api_operations(build_app()):
        assert len(op.tags) == 1, f"{op.operation_id} has tags {op.tags}"


def test_every_route_is_part_of_the_published_contract() -> None:
    hidden = [op.operation_id for op in api_operations(build_app()) if not op.in_schema]

    assert hidden == []
