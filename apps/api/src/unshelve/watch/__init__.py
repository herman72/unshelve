"""The core of the watcher: it decides which message a person gets (docs/learn/55, issue #154).

The core is pure. It reads no database, no network, no Telegram and no clock: a function that
needs the time gets it as an argument. It imports only the standard library and pydantic. A
test checks these rules (tests/watch/test_pure.py).

The modules:

- ``limits``: the limits of the watcher, as constants.
- ``hide``: the secrets that the watcher holds, and ``hide_secrets``, which removes each secret
  from a text before the text leaves the watcher.

This file imports nothing.
"""
