"""Tests for the Qt scripting host (``DisplayCAL.ui.scripting``).

Covers both ends of the line-based protocol (newline-terminated UTF-8
commands, EOT-terminated replies) as implemented by ``ScriptingHostMixin``:

* the server side, ``message_handler``, which must survive bytes that are not
  valid UTF-8, multi-byte characters split across ``recv()`` calls and
  exceptions raised while handling a single line, and must always close the
  connection;
* the client side, ``send_command``, whose replies are bytes on Python 3.

Everything runs against fake connections or throwaway loopback sockets. No
widget is created or shown (the mixin is used on a plain object), and no real
DisplayCAL instance, profile loader, config file or display is touched.
"""

from __future__ import annotations

import os
import socket
import socketserver
import sys
import threading
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

pytest.importorskip("qtpy")

from DisplayCAL.config import APPBASENAME, PYNAME  # noqa: E402
from DisplayCAL.meta import NAME as APPNAME  # noqa: E402
from DisplayCAL.ui import scripting  # noqa: E402
from DisplayCAL.ui.scripting import ScriptingHostMixin, responseformats  # noqa: E402

LOADER_LOCK_NAME = f"{APPBASENAME}-apply-profiles"
LOADER_APPNAME = f"{APPNAME}-apply-profiles"
# What bytes that are not valid UTF-8 decode to
REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


class FakeHost(ScriptingHostMixin):
    """A scripting host without a window.

    UI commands are recorded instead of being marshalled to the GUI thread.

    Args:
        hosts (list): What ``get_scripting_hosts()`` returns.
    """

    def __init__(self, hosts: list[str] = ()) -> None:
        self.listening = True
        self.ui_commands = []
        self._hosts = list(hosts)
        self._finish_bridge = types.SimpleNamespace(
            requested=types.SimpleNamespace(
                emit=lambda data, conn, timestamp: self.ui_commands.append(data)
            )
        )

    def get_scripting_hosts(self) -> list:
        """Return the configured scripting hosts.

        Returns:
            list: ``"<ip>:<port> <lock name>"`` strings.
        """
        return list(self._hosts)


class FakeConn:
    """A connection whose ``recv()`` returns predefined chunks, then EOF.

    A chunk that is an exception instance is raised instead.

    Args:
        chunks (list): The byte strings ``recv()`` returns, in order.
    """

    def __init__(self, chunks: list) -> None:
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, bufsize: int) -> bytes:
        """Return the next chunk, or b"" (EOF) when there are none left.

        Args:
            bufsize (int): Ignored.

        Returns:
            bytes: The next chunk.
        """
        chunk = self.chunks.pop(0) if self.chunks else b""
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, data: bytes) -> None:
        """Record sent data.

        Args:
            data (bytes): The data.
        """
        self.sent.append(data)

    def shutdown(self, how: int) -> None:
        """Do nothing.

        Args:
            how (int): Ignored.
        """

    def close(self) -> None:
        """Mark the connection as closed."""
        self.closed = True


@pytest.fixture
def logged_errors(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record the exceptions passed to log_error_traceback by the host.

    Returns:
        list: The exceptions being handled when it was called.
    """
    errors = []
    monkeypatch.setattr(
        scripting, "log_error_traceback", lambda: errors.append(sys.exc_info()[1])
    )
    return errors


def _run_handler(host: FakeHost, chunks: list) -> FakeConn:
    conn = FakeConn(chunks)
    host.message_handler(conn, ("127.0.0.1", 12345))
    assert conn.closed
    assert conn not in responseformats
    return conn


# -- ScriptingHostMixin.message_handler (server side) -------------------------


def test_message_handler_answers_getappname(logged_errors: list) -> None:
    """A non-UI command is answered on the handler thread."""
    host = FakeHost()
    conn = _run_handler(host, [b"getappname\n"])
    assert conn.sent == [f"{PYNAME}\4".encode()]
    assert logged_errors == []


def test_message_handler_survives_invalid_utf8(logged_errors: list) -> None:
    """A non-UTF-8 line (e.g. from a foreign program) doesn't end the session."""
    host = FakeHost()
    conn = _run_handler(host, [b"\x92\x93junk\n", b"getappname\n"])
    assert host.ui_commands == [[REPLACEMENT * 2 + "junk"]]
    assert conn.sent == [f"{PYNAME}\4".encode()]
    assert logged_errors == []


def test_message_handler_split_multibyte_character(logged_errors: list) -> None:
    """A UTF-8 character split across two recv() calls is decoded intact."""
    host = FakeHost()
    _run_handler(host, [b'notify "caf\xc3', b'\xa9" silent\n'])
    assert host.ui_commands == [["notify", "caf\xe9", "silent"]]
    assert logged_errors == []


def test_message_handler_partial_and_multiple_lines(logged_errors: list) -> None:
    """Several lines per chunk and lines spanning chunks are all handled."""
    host = FakeHost()
    conn = _run_handler(host, [b"getappname\ngetapp", b"name\n\n", b"close\n"])
    assert conn.sent == [f"{PYNAME}\4".encode()] * 2
    assert host.ui_commands == [["close"]]
    assert logged_errors == []


def test_message_handler_survives_exception_in_one_line(
    logged_errors: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error while handling one line is logged; the next line still works."""
    real_split = scripting.split_command_line

    def split(line: str) -> list:
        if line == "boom":
            raise ValueError("boom")
        return real_split(line)

    monkeypatch.setattr(scripting, "split_command_line", split)
    host = FakeHost()

    conn = _run_handler(host, [b"boom\n", b"getappname\n"])

    assert conn.sent == [f"{PYNAME}\4".encode()]
    assert [type(e) for e in logged_errors] == [ValueError]


def test_message_handler_closes_connection_on_unexpected_error(
    logged_errors: list,
) -> None:
    """An unexpected error outside one line's handling still closes the conn."""
    host = FakeHost()
    conn = _run_handler(host, [b"getappname\n", RuntimeError("recv broke")])
    assert conn.sent == [f"{PYNAME}\4".encode()]
    assert [str(e) for e in logged_errors] == ["recv broke"]


def _read_reply(sock: socket.socket) -> bytes:
    reply = b""
    while not reply.endswith(b"\4"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        reply += chunk
    return reply


def test_message_handler_over_a_real_socket(logged_errors: list) -> None:
    """Bad bytes and a split character do not end a real connection."""
    host = FakeHost()
    server_side, client = socket.socketpair()
    server_side.settimeout(0.2)  # like connection_handler
    client.settimeout(10)
    thread = threading.Thread(
        target=host.message_handler,
        args=(server_side, ("127.0.0.1", 12345)),
        daemon=True,
    )
    thread.start()
    try:
        client.sendall(b"\xff\xfe foreign program\n")
        client.sendall(b'echo "caf\xc3')
        client.sendall(b'\xa9"\ngetappname\n')
        assert _read_reply(client) == f"{PYNAME}\4".encode()
    finally:
        client.close()
    thread.join(10)

    assert not thread.is_alive()
    assert host.ui_commands == [
        [REPLACEMENT * 2, "foreign", "program"],
        ["echo", "caf\xe9"],
    ]
    assert server_side not in responseformats
    assert logged_errors == []


# -- ScriptingHostMixin.send_command (client side) ----------------------------


class _LoaderHandler(socketserver.StreamRequestHandler):
    """Answer every command line like the scripting host does (EOT-terminated)."""

    def handle(self) -> None:
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.server.received.append(line)
            if line == "getappname":
                reply = self.server.appname.encode("utf-8")
            else:
                reply = b"ok"
            self.wfile.write(reply + b"\4")
            self.wfile.flush()


class FakeScriptingHost(socketserver.ThreadingTCPServer):
    """A loopback server that records commands and replies like the loader.

    Args:
        appname (str): Reply to ``getappname``.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, appname: str = LOADER_APPNAME) -> None:
        super().__init__(("127.0.0.1", 0), _LoaderHandler)
        self.appname = appname
        self.received = []

    @property
    def port(self) -> int:
        """int: The port the server listens on."""
        return self.server_address[1]


@pytest.fixture
def loader() -> FakeScriptingHost:
    """Run a FakeScriptingHost on a background thread.

    Yields:
        FakeScriptingHost: The running server.
    """
    server = FakeScriptingHost()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _host_for(server: FakeScriptingHost) -> FakeHost:
    return FakeHost([f"127.0.0.1:{server.port} {LOADER_LOCK_NAME}"])


def test_send_command_delivers_command_and_returns_str(
    loader: FakeScriptingHost,
) -> None:
    """The command reaches the loader and the decoded reply is returned."""
    result = _host_for(loader).send_command("apply-profiles", "apply-profiles")

    assert result == "ok"
    assert loader.received == ["getappname", "apply-profiles"]


def test_send_command_accepts_lowercase_appname(loader: FakeScriptingHost) -> None:
    """When running from source, the loader reports its name in lowercase."""
    loader.appname = LOADER_APPNAME.lower()

    result = _host_for(loader).send_command("apply-profiles", "apply-profiles")

    assert result == "ok"
    assert loader.received == ["getappname", "apply-profiles"]


def test_send_command_skips_foreign_app_on_port(loader: FakeScriptingHost) -> None:
    """Another program on the lock file's port gets no command; None is returned."""
    loader.appname = "SomethingElse"

    result = _host_for(loader).send_command("apply-profiles", "apply-profiles")

    assert result is None
    assert loader.received == ["getappname"]
