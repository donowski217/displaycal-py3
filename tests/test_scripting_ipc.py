"""Tests for the wx scripting host's ``BaseFrame.message_handler``.

The scripting protocol is line based (newline-terminated UTF-8 commands). The
handler must survive bytes that are not valid UTF-8, multi-byte characters
split across ``recv()`` calls and exceptions raised while handling a single
line.

Everything runs against fake connection objects. No wx window is created,
nothing is shown, and no real DisplayCAL instance or display is touched.
"""

import types

import pytest

from DisplayCAL import log as dc_log
from DisplayCAL import wx_windows
from DisplayCAL.config import PYNAME
from DisplayCAL.wx_windows import BaseFrame, responseformats


class FakeFrame:
    """Stand-in for a BaseFrame subclass, without creating a wx window.

    Any attribute it does not define is taken from ``BaseFrame`` (or ``cls``)
    and bound to this instance, so the real ``send_command``/``connect``/
    ``message_handler`` code runs unchanged.

    Args:
        hosts (list): What ``get_scripting_hosts()`` returns.
        cls (type): The class to borrow methods from.
    """

    def __init__(self, hosts=(), cls=BaseFrame):
        self._hosts = list(hosts)
        self._cls = cls
        self.listening = True
        self.ui_commands = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        attr = getattr(self._cls, name)
        if callable(attr):
            return types.MethodType(attr, self)
        return attr

    def get_scripting_hosts(self):
        """Return the configured scripting hosts.

        Returns:
            list: ``"<ip>:<port> <lock name>"`` strings.
        """
        return list(self._hosts)

    def finish_processing(self, data, conn, command_timestamp):
        """Record UI commands instead of processing them.

        Args:
            data (list): The split command line.
            conn: The client connection.
            command_timestamp (str): The command timestamp.
        """
        self.ui_commands.append(data)


# -- BaseFrame.message_handler (server side) ---------------------------------


class FakeConn:
    """A connection whose ``recv()`` returns predefined chunks, then EOF.

    Args:
        chunks (list): The byte strings ``recv()`` returns, in order.
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, bufsize):
        """Return the next chunk, or b"" (EOF) when there are none left.

        Args:
            bufsize (int): Ignored.

        Returns:
            bytes: The next chunk.
        """
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        """Record sent data.

        Args:
            data (bytes): The data.
        """
        self.sent.append(data)

    def shutdown(self, how):
        """Do nothing.

        Args:
            how (int): Ignored.
        """

    def close(self):
        """Mark the connection as closed."""
        self.closed = True


@pytest.fixture
def host(monkeypatch):
    """A FakeFrame whose UI commands are recorded synchronously.

    Returns:
        FakeFrame: The scripting host.
    """
    monkeypatch.setattr(wx_windows.wx, "CallAfter", lambda func, *args: func(*args))
    return FakeFrame()


def _run_handler(host, chunks):
    conn = FakeConn(chunks)
    host.message_handler(conn, ("127.0.0.1", 12345))
    assert conn.closed
    assert conn not in responseformats
    return conn


def test_message_handler_answers_getappname(host):
    conn = _run_handler(host, [b"getappname\n"])
    assert conn.sent == [f"{PYNAME}\4".encode()]


def test_message_handler_survives_invalid_utf8(host):
    """A non-UTF-8 line (e.g. from a foreign program) doesn't end the session."""
    conn = _run_handler(host, [b"\x92\x93junk\n", b"getappname\n"])
    assert host.ui_commands == [["��junk"]]
    assert conn.sent == [f"{PYNAME}\4".encode()]


def test_message_handler_split_multibyte_character(host):
    """A UTF-8 character split across two recv() calls is decoded intact."""
    _run_handler(host, [b'notify "caf\xc3', b'\xa9" silent\n'])
    assert host.ui_commands == [["notify", "caf\xe9", "silent"]]


def test_message_handler_partial_and_multiple_lines(host):
    """Several lines per chunk and lines spanning chunks are all handled."""
    conn = _run_handler(host, [b"getappname\ngetapp", b"name\n\n", b"close\n"])
    assert conn.sent == [f"{PYNAME}\4".encode()] * 2
    assert host.ui_commands == [["close"]]


def test_message_handler_survives_exception_in_one_line(host, monkeypatch):
    """An error while handling one line is logged; the next line still works."""
    real_split = wx_windows.split_command_line

    def split(line):
        if line == "boom":
            raise ValueError("boom")
        return real_split(line)

    monkeypatch.setattr(wx_windows, "split_command_line", split)
    logged = []
    monkeypatch.setattr(
        dc_log.safe_print,
        "write",
        lambda *args, **kwargs: logged.append(" ".join(map(str, args))),
    )

    conn = _run_handler(host, [b"boom\n", b"getappname\n"])

    assert conn.sent == [f"{PYNAME}\4".encode()]
    assert any("boom" in msg for msg in logged)
