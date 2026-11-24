"""Unshelve: a goal-aware reading inbox."""

# The version comes from the metadata of the installed package, read only when a module asks
# for it. The healthcheck of the worker and the bot imports this package every 30 s
# (unshelve.alive), and importlib.metadata alone took longer than the whole check.
__version__: str


def __getattr__(name: str) -> str:
    if name == "__version__":
        from importlib.metadata import version  # read on the first use only

        return version("unshelve")
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
