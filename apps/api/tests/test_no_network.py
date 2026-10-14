"""No test talks to a different computer.

The tests use fakes for Telegram, for the model and for the web. A guard makes this a rule
that cannot be broken by mistake (``--allow-hosts`` in pyproject.toml). It was added after
four tests made a real request to Telegram with a made-up token: a new handler answered a
recorded message, and the test bot had a real connection.
"""

import socket

import pytest
from pytest_socket import SocketConnectBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")  # the guard says it, too
def test_a_test_cannot_open_a_connection_to_an_outside_computer() -> None:
    # 192.0.2.1 is an address for documents (RFC 5737). No computer has it.
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
