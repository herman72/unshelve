"""Only the fetcher opens an address from outside (ADR 0010).

A module that imports an HTTP client could open a saved link without the guard. This test
reads the imports of each module of the package, and allows an HTTP client only in
``unshelve.net``. Libraries that talk to one fixed service (aiogram for Telegram, the SDK of
Anthropic) are not HTTP clients for this rule: they open no saved link.
"""

import ast
from pathlib import Path

import unshelve

HTTP_CLIENTS = (
    "aiohttp",
    "http.client",
    "httpcore",
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
)
# trafilatura reads an article from bytes, and it can also download a page by itself, without
# the guard. The extractors give it the bytes of the fetcher.
DOWNLOADS = ("fetch_url", "fetch_response")
PACKAGE = Path(unshelve.__file__).parent


def _ways_out(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _is_client(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            if _is_client(node.module) or node.module.startswith("trafilatura.downloads"):
                found.append(node.module)
            elif node.module == "trafilatura":
                found += [f"trafilatura.{a.name}" for a in node.names if a.name in DOWNLOADS]
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "trafilatura"
            and node.attr in DOWNLOADS
        ):
            found.append(f"trafilatura.{node.attr}")
    return found


def _is_client(module: str) -> bool:
    return any(module == client or module.startswith(f"{client}.") for client in HTTP_CLIENTS)


def test_only_the_net_package_opens_a_connection_to_the_web() -> None:
    found = {
        path.relative_to(PACKAGE).as_posix(): ways
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.relative_to(PACKAGE).parts[0] != "net"
        and (ways := _ways_out(path.read_text(encoding="utf-8")))
    }

    assert found == {}


def test_the_check_sees_each_way_out() -> None:
    source = """
import httpx
import urllib.request
from aiohttp import ClientSession
from trafilatura import extract, fetch_url
import trafilatura
trafilatura.fetch_response("https://example.com/")
"""
    assert _ways_out(source) == [
        "httpx",
        "urllib.request",
        "aiohttp",
        "trafilatura.fetch_url",
        "trafilatura.fetch_response",
    ]
