"""The core of the watcher is pure: it loads no driver of a service, reads no clock, and does
no I/O. It uses only the modules and the builtins on a list. Only state.py may read and write a
file, and log (docs/learn/55)."""

import ast
import builtins
import dataclasses
import importlib
import pkgutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

import unshelve

WATCH = Path(unshelve.__file__).parent / "watch"

# The modules that a check must not load: the database and its driver, Telegram, the network,
# the model.
DRIVERS = {"sqlalchemy", "psycopg", "pgvector", "aiogram", "httpx", "anthropic"}
# A dynamic import can load any module, and the scan cannot see which: the core has none.
DYNAMIC_IMPORT = {"importlib"}
# The builtins that can load any module: __import__, the builtins that run code from a text,
# and the module of the builtins, which holds each of them.
LOADERS = {"__import__", "exec", "eval", "compile", "__builtins__"}
# The core needs no part of the module time: its functions read a clock or sleep. The core gets
# the time as an argument.
CLOCK_MODULES = {"time"}
# These functions of datetime read a clock, by any name that an import gives them.
CLOCKS = {
    "datetime.datetime.now",
    "datetime.datetime.today",
    "datetime.datetime.utcnow",
    "datetime.date.today",
}
# The same functions as values: another module can hold them, as pydantic.types holds datetime.
CLOCK_VALUES = [pkgutil.resolve_name(name) for name in sorted(CLOCKS)]
# The methods of Path that read or write the disk.
PATH_IO = {
    "open",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "stat",
    "lstat",
    "exists",
    "is_file",
    "is_dir",
    "iterdir",
    "glob",
    "rglob",
    "readlink",
    "touch",
    "unlink",
    "mkdir",
    "rmdir",
    "chmod",
    "symlink_to",
    "hardlink_to",
}
# Path.replace(target) and Path.rename(target) take one argument, also by its name. str.replace
# takes two or three.
PATH_MOVES = {"replace", "rename"}
# The builtins that the core may use. Each other builtin can do I/O or run code from a text, for
# example print, open, input, help, breakpoint and exec. Others reach them: vars, globals and the
# module __builtins__. A list of the builtins that the core must not use always misses one. A
# name in the core that is the same as such a builtin counts too, so the core does not use it.
PURE_BUILTINS = {
    "ValueError",
    "TypeError",
    "KeyError",
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "divmod",
    "enumerate",
    "float",
    "frozenset",
    "getattr",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "object",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
}
# A name such as __name__ or __doc__ is a global of each module, not the builtin.
BUILTINS = {name for name in dir(builtins) if not name.startswith("__")} | LOADERS
OTHER_BUILTINS = BUILTINS - PURE_BUILTINS
# The modules that the core may import, and each module under them. A list of the modules that
# it must not import always misses one: warnings, pprint, builtins, io and posix can each write
# a text to stderr, and uuid and asyncio can read a clock. A later part adds a module here.
IMPORTS = {
    "collections.abc",
    "dataclasses",
    "decimal",
    "re",
    "typing",
    "unicodedata",
    "urllib.parse",
    "pydantic",
    "unshelve.watch",
}
# The one module that reads and writes the state file, and logs. It may import more.
DOES_IO = {"state.py"}

# A separate Python: import each module of the core, then list each module that is loaded.
PROBE = """
import importlib
import sys
for name in sys.argv[1:]:
    importlib.import_module(name)
print("\\n".join(sorted(sys.modules)))
"""


def _files() -> list[Path]:
    return sorted(WATCH.glob("*.py"))


def _source(file: Path) -> str:
    return file.read_text(encoding="utf-8")


def _modules() -> list[str]:
    """The name of each module of the core, without the package."""
    return [f"unshelve.watch.{file.stem}" for file in _files() if file.stem != "__init__"]


def _under(module: str, names: set[str]) -> bool:
    """True if ``module`` is one of ``names``, or a module under one of them."""
    return any(module == name or module.startswith(f"{name}.") for name in names)


def _imported(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Each module that an import names: ``from urllib import request`` names urllib.request."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def _bound(tree: ast.AST) -> dict[str, str]:
    """The full name of each name that an import binds: ``import time as clock`` binds clock to
    time, and ``from datetime import date`` binds date to datetime.date."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                names[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _full_name(node: ast.expr, names: dict[str, str]) -> str | None:
    """``datetime.datetime.now`` for ``dt.now`` after ``from datetime import datetime as dt``."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _full_name(node.value, names)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _value(name: str | None) -> object:
    """The value of a full name, such as the module sys for ``typing.sys``: typing imports sys.
    None if the name is not under a module on the list, so the test loads no other module."""
    if name is None or not _under(name, IMPORTS):
        return None
    try:
        return pkgutil.resolve_name(name)
    except (ImportError, AttributeError):
        return None


def _where(node: ast.AST) -> str:
    return f"{getattr(node, 'lineno', 0)}: {ast.unparse(node)}"


def _getattr_by_text(node: ast.AST) -> bool:
    """A call of getattr with a constant name, such as ``getattr(datetime.datetime, "now")``.
    The scans look for an attribute by its name, so they cannot see this one."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    )


def _clocks(source: str) -> list[str]:
    """Each place in ``source`` that can read a clock."""
    tree = ast.parse(source)
    names = _bound(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if any(_under(module, CLOCK_MODULES) for module in _imported(node)):
                found.append(_where(node))
        elif _getattr_by_text(node) or (
            isinstance(node, ast.Attribute) and _is_clock(_full_name(node, names))
        ):
            found.append(_where(node))
    return found


def _is_clock(name: str | None) -> bool:
    """True if ``name`` is a clock, also by a path through a module on the list:
    ``pydantic.types.datetime.now`` is ``datetime.datetime.now``."""
    return name in CLOCKS or _value(name) in CLOCK_VALUES


def _does_io(node: ast.AST) -> bool:
    """A builtin that is not on the list, also as a value and not only in a call, a Path method
    that reads or writes, an attribute of os, or a getattr with a constant name. The import
    scan finds a module of I/O or of logs."""
    if _getattr_by_text(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in OTHER_BUILTINS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        target = [keyword.arg for keyword in node.keywords] == ["target"]
        one_target = (len(node.args) == 1 and not node.keywords) or (not node.args and target)
        return node.func.attr in PATH_IO or (node.func.attr in PATH_MOVES and one_target)
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return False


def _io(source: str) -> list[str]:
    """Each place in ``source`` that does I/O or logs."""
    return [_where(node) for node in ast.walk(ast.parse(source)) if _does_io(node)]


def _runtime(node: ast.AST, names: dict[str, str]) -> Iterator[ast.AST]:
    """``node`` and each node under it, except the body of ``if TYPE_CHECKING:``. That body
    never runs: only the type checker reads it."""
    yield node
    children: Iterable[ast.AST] = ast.iter_child_nodes(node)
    if isinstance(node, ast.If) and _full_name(node.test, names) == "typing.TYPE_CHECKING":
        children = [node.test, *node.orelse]
    for child in children:
        yield from _runtime(child, names)


def _allowed(node: ast.Import | ast.ImportFrom) -> bool:
    """True if each module that the import loads is on the list: ``from urllib import parse``
    loads urllib.parse. A relative import is not on the list."""
    if isinstance(node, ast.Import):
        return all(_under(alias.name, IMPORTS) for alias in node.names)
    if node.level or node.module is None:
        return False
    module = node.module
    return _under(module, IMPORTS) or all(
        _under(f"{module}.{alias.name}", IMPORTS) for alias in node.names
    )


def _owner(value: object) -> str | None:
    """The module that ``value`` comes from: the module itself, or the ``__module__`` of a
    function or a class. None for a value with no module, such as a number."""
    if isinstance(value, ModuleType):
        return value.__name__
    owner = getattr(value, "__module__", None)
    return owner if isinstance(owner, str) else None


def _allowed_owner(owner: str) -> bool:
    """True if ``owner`` is on the list or is the module of the builtins. Also if it holds a part
    of a module on the list: ``_decimal`` is the code in C of decimal, and ``pydantic_core`` is
    the core of pydantic."""
    top = owner.split(".")[0]
    if top == "pydantic_core":
        owner = "pydantic"
    elif top.startswith("_"):
        owner = owner[1:]
    return _under(owner, IMPORTS | {"builtins"})


def _parent(name: str) -> bool:
    """True if ``name`` is a package of a module on the list: ``import collections.abc`` binds
    the name collections."""
    return any(module.startswith(f"{name}.") for module in IMPORTS)


def _imports(source: str) -> list[str]:
    """Each import in ``source`` that runs and loads a module that is not on the list, also an
    import inside a function. Also each name that reaches such a module through a module on the
    list: ``typing.sys`` is the module sys, and ``pydantic.fields.warn`` is a function of the
    module warnings. Also each name that reaches another module through a package of a module on
    the list: ``collections._sys`` is the module sys."""
    tree = ast.parse(source)
    names = _bound(tree)
    found: list[str] = []
    for node in _runtime(tree, names):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if not _allowed(node):
                found.append(_where(node))
        elif isinstance(node, ast.Name | ast.Attribute):
            name = _full_name(node, names)
            if name is None:
                continue
            owner = _owner(_value(name))
            if not (_under(name, IMPORTS) or _parent(name)) or (
                owner is not None and not _allowed_owner(owner)
            ):
                found.append(_where(node))
    return found


def _loads_a_module(node: ast.AST) -> bool:
    """An import of a driver or of importlib, or a builtin that can load any module, also as a
    value and not only in a call."""
    if isinstance(node, ast.Import | ast.ImportFrom):
        return any(_under(module, DRIVERS | DYNAMIC_IMPORT) for module in _imported(node))
    return isinstance(node, ast.Name) and node.id in LOADERS


def _drivers(source: str) -> list[str]:
    """Each place in ``source`` that can load a driver, also one inside a function."""
    return [_where(node) for node in ast.walk(ast.parse(source)) if _loads_a_module(node)]


def test_the_core_loads_no_driver_and_no_other_part_of_unshelve() -> None:
    modules = _modules()
    assert modules, f"no modules in {WATCH}"
    names = ["unshelve.watch", *modules]

    done = subprocess.run(  # noqa: S603 - this Python, with a script of this test
        [sys.executable, "-c", PROBE, *names], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr
    loaded = set(done.stdout.split())
    assert set(names) <= loaded
    assert {name.split(".")[0] for name in loaded} & DRIVERS == set()
    # Settings is a name for the type checker only: no module of unshelve outside the core.
    others = {name for name in loaded if name.startswith("unshelve.")}
    assert {name for name in others if not _under(name, {"unshelve.watch"})} == set()
    assert "pydantic_settings" not in loaded


def test_the_core_imports_no_driver_also_in_a_function() -> None:
    found = [f"{file.name}:{where}" for file in _files() for where in _drivers(_source(file))]

    assert found == []


def test_the_core_reads_no_clock() -> None:
    found = [f"{file.name}:{where}" for file in _files() for where in _clocks(_source(file))]

    assert found == []


def test_only_the_state_module_does_io() -> None:
    files = [file for file in _files() if file.name not in DOES_IO]
    found = [f"{file.name}:{where}" for file in files for where in _io(_source(file))]

    assert found == []


def test_the_core_imports_only_the_modules_on_the_list() -> None:
    files = [file for file in _files() if file.name not in DOES_IO]
    found = [f"{file.name}:{where}" for file in files for where in _imports(_source(file))]

    assert found == []


def test_the_package_file_imports_nothing() -> None:
    tree = ast.parse(_source(WATCH / "__init__.py"))

    assert [
        _where(node) for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)
    ] == []


def test_each_value_type_of_the_core_is_frozen_and_has_slots() -> None:
    kinds: list[type] = []
    for name in _modules():
        module = importlib.import_module(name)
        kinds += [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and dataclasses.is_dataclass(value)
            and value.__module__ == name
        ]
    assert kinds, f"no dataclasses in {WATCH}"

    loose = [
        kind.__qualname__
        for kind in kinds
        if not vars(kind)["__dataclass_params__"].frozen or "__slots__" not in vars(kind)
    ]

    assert loose == []


# --- The scans find what they look for ---------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import time\ntime.time()",
        "import time as clock\nclock.time()",
        "from time import monotonic_ns",
        "from datetime import datetime as dt\ndt.now()",
        "import datetime as d\nd.date.today()",
        "from datetime import date\ndate.today()",
        "import datetime\ndatetime.datetime.utcnow()",
        "import datetime\ngetattr(datetime.datetime, 'now')()",
        # pydantic.types imports the class datetime, so the clock is an attribute of it.
        "import pydantic.types\npydantic.types.datetime.now()",
        "from pydantic import types\ntypes.datetime.now()",
    ],
)
def test_the_clock_scan_finds_a_clock_by_any_name(source: str) -> None:
    assert _clocks(source) != []


def test_the_clock_scan_lets_a_time_that_is_an_argument_pass() -> None:
    source = "from datetime import UTC, datetime\ndatetime.fromtimestamp(now, UTC)"

    assert _clocks(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "print(obs)",
        "open('/dev/null')",
        "path.read_text()",
        "path.touch()",
        "path.replace(target)",
        "getattr(path, 'write_text')(text)",
        "os.environ.get('ANTHROPIC_API_KEY')",
        "path.rename(target=new)",
        "path.is_file()",
        "list(path.iterdir())",
        # Each builtin that is not on the list, also as a value: it writes or reads a text, runs
        # code from a text, or reaches such a builtin.
        "input(text)",
        "help(obs)",
        "breakpoint()",
        "__builtins__['print'](text)",
        "def say(text, out=print):\n    out(text)",
        "exec('print(obs)')",
        "vars(obs)",
    ],
)
def test_the_io_scan_finds_io(source: str) -> None:
    assert _io(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "text.replace('a', 'b')",
        "replace(obs, error=None)",
        "dataclasses.replace(obs, error=None)",
        "getattr(settings, name)",
        "moment.replace(tzinfo=UTC)",
        "sorted({value for value in values if value}, key=len)",
        "isinstance(value, SecretStr) and len(value) >= 12",
        "max((len(v) for v in values), default=0)",
        "raise ValueError(msg)",
        # A module has its own __name__: it is not the builtin.
        "__name__",
    ],
)
def test_the_io_scan_lets_pure_code_pass(source: str) -> None:
    assert _io(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import logging",
        "import urllib.request",
        "from urllib import request",
        "from http.client import HTTPConnection",
        "import subprocess",
        "import sys\nsys.stdout.write(text)",
        "from sys import stderr",
        "import warnings\nwarnings.warn(text)",
        "import builtins\nbuiltins.print(text)",
        "import pprint",
        "import io",
        "import posix",
        "from pathlib import Path",
        "import uuid",
        "from email.utils import formatdate",
        "import asyncio",
        "import psycopg",
        "from unshelve import config",
        "from . import limits",
        "def secrets_of():\n    from unshelve.config import Settings\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import logging",
        # A module on the list can hold another module as an attribute: typing imports sys.
        "import typing\ntyping.sys.stderr.write(text)",
        "import typing\ntyping.sys.modules['importlib'].import_module('httpx')",
        "from typing import sys\nsys.stderr.write(text)",
        "import urllib.parse\nurllib.parse.warnings.warn(text)",
        "import re\nre.enum",
        # It can also hold a function or a class of such a module: pydantic imports warn,
        # import_module, datetime and Path.
        "import pydantic.fields\npydantic.fields.warn(text)",
        "import pydantic\npydantic.import_module('sqlalchemy')",
        "from pydantic.types import datetime\ndatetime.fromtimestamp(0).now()",
        "from pydantic.types import datetime\ngetattr(datetime, NOW)()",
        "import pydantic.types\npydantic.types.Path.cwd()",
        "import pydantic.types\npydantic.types.Path.replace(source, target)",
        # An import of a module binds its package too, and the package holds other modules:
        # collections imports sys as _sys.
        "import collections.abc\ncollections._sys.stderr.write(text)",
        "import collections.abc\ndef load():\n    return collections._sys.modules['importlib']",
        "import urllib.parse\nurllib.request.urlopen(url)",
        "import unshelve.watch.limits\nunshelve.config.Settings()",
    ],
)
def test_the_import_scan_finds_each_module_that_is_not_on_the_list(source: str) -> None:
    assert _imports(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from urllib.parse import quote",
        "from urllib import parse",
        "import collections.abc",
        "from unshelve.watch.limits import ERROR_CHARS",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from unshelve import config",
        "import typing\nif typing.TYPE_CHECKING:\n    import logging",
        "import urllib.parse\nurllib.parse.quote(text)",
        "import collections.abc\ncollections.abc.Iterable",
        "from pydantic import SecretStr\nSecretStr('made-up')",
        "import re\nre.compile('a+').sub('b', text)",
        # A module on the list can keep a part in another module: decimal.localcontext comes
        # from _decimal, its code in C, and pydantic.ValidationError from pydantic_core.
        "from decimal import localcontext\nlocalcontext()",
        "import decimal\ndecimal.getcontext().prec",
        "from pydantic import ValidationError\nValidationError",
        "from pydantic import ValidationInfo\nValidationInfo",
    ],
)
def test_the_import_scan_lets_the_modules_on_the_list_pass(source: str) -> None:
    assert _imports(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "def scrub():\n    import sqlalchemy\n",
        "from aiogram.types import Message",
        "from psycopg import AsyncConnection",
        "import importlib\nimportlib.import_module('httpx')",
        "from importlib import import_module",
        "__import__('anthropic')",
        "def load():\n    exec('import sqlalchemy')",
        "eval(\"__import__('httpx')\")",
        "load = __import__\nload('aiogram')",
        "__builtins__['__import__']('psycopg')",
    ],
)
def test_the_driver_scan_finds_each_way_to_load_a_driver(source: str) -> None:
    assert _drivers(source) != []


def test_the_driver_scan_lets_other_imports_pass() -> None:
    assert _drivers("from urllib.parse import quote\nimport pydantic") == []
