"""The OpenAPI document is the contract with the web app. These tests keep it honest."""

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from tests.api.support import PUBLIC_OPERATIONS, build_app
from unshelve.cli import app as cli

COMMITTED = Path(__file__).resolve().parents[2] / "openapi.json"
HTTP_METHODS = ("get", "put", "post", "delete", "patch")


def operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(method, path, operation) for every operation in the document."""
    return [
        (method, path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def test_the_committed_document_is_what_the_application_generates(tmp_path: Path) -> None:
    # The same command that wrote the committed file, so the serialisation cannot differ.
    generated = tmp_path / "openapi.json"

    result = CliRunner().invoke(cli, ["openapi", "--output", str(generated)])

    assert result.exit_code == 0, result.output
    assert COMMITTED.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8"), (
        "openapi.json is stale: run `make openapi` and commit the result"
    )


def test_every_documented_error_is_a_problem() -> None:
    problem = {"$ref": "#/components/schemas/Problem"}

    for method, path, operation in operations(build_app().openapi()):
        for status, response in operation["responses"].items():
            if int(status) >= 400:
                schema = response["content"]["application/json"]["schema"]
                assert schema == problem, f"{method.upper()} {path} documents {status} as {schema}"


def test_the_framework_default_for_validation_errors_is_not_documented() -> None:
    # FastAPI adds HTTPValidationError to a route with parameters unless the route lists 422.
    # This API answers 422 with a problem, so the default would describe the wrong body.
    schemas = build_app().openapi()["components"]["schemas"]

    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_no_operation_documents_a_501() -> None:
    # 501 is temporary: it goes away with the stub, so it is not part of the contract.
    for method, path, operation in operations(build_app().openapi()):
        assert "501" not in operation["responses"], f"{method.upper()} {path}"


def test_only_the_public_operations_lack_a_session_requirement() -> None:
    without_session = {
        operation["operationId"]
        for _, _, operation in operations(build_app().openapi())
        if not operation.get("security")
    }

    assert without_session == PUBLIC_OPERATIONS
