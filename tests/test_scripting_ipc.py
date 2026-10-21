"""Tests for the wx scripting IPC between DisplayCAL and its standalone tools.

Covers both ends of the line-based protocol (newline-terminated UTF-8
commands, EOT-terminated replies):

* the client side, ``BaseFrame.send_command`` and the ``display_cal.MainFrame``
  code that uses it to talk to the running profile loader. On Python 3
  ``ScriptingClientSocket.get_single_response()`` returns bytes, so comparing
  the reply with a str never matched and no command was ever delivered;
* the server side, ``BaseFrame.message_handler``, which must survive bytes that
  are not valid UTF-8, multi-byte characters split across ``recv()`` calls and
  exceptions raised while handling a single line.

Everything runs against throwaway loopback sockets or fake connection objects.
No wx window is created, nothing is shown, and no real DisplayCAL instance,
profile loader, config file or display is touched.
"""

import socketserver
import sys
import threading
import types
from unittest import mock

import pytest

from DisplayCAL import config, wx_windows
from DisplayCAL import log as dc_log
from DisplayCAL.config import APPBASENAME, PYNAME
from DisplayCAL.meta import NAME as APPNAME
from DisplayCAL.profile_loader import PLFrame
from DisplayCAL.wexpect import split_command_line
from DisplayCAL.wx_windows import BaseFrame, responseformats

LOADER_LOCK_NAME = f"{APPBASENAME}-apply-profiles"
LOADER_APPNAME = f"{APPNAME}-apply-profiles"

# -- fake scripting host (the "profile loader") ------------------------------


class _LoaderHandler(socketserver.StreamRequestHandler):
    """Answer every command line like ``message_handler``/``send_response``."""

    def handle(self):
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.server.received.append(line)
            self.wfile.write(self.server.respond(line) + b"\4")
            self.wfile.flush()


class FakeScriptingHost(socketserver.ThreadingTCPServer):
    """A loopback server that records commands and replies like the loader.

    Lines are split with the real ``split_command_line`` and the argument
    checks of ``BaseFrame``/``PLFrame`` are applied, so a command the real
    loader would reject is answered "invalid" here too. ``notify`` goes
    through the real ``PLFrame.process_data``.

    Args:
        appname (str): Reply to ``getappname``.
        replies (dict): Optional ``{command prefix: reply bytes}`` overrides.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, appname=LOADER_APPNAME, replies=None):
        super().__init__(("127.0.0.1", 0), _LoaderHandler)
        self.appname = appname
        self.replies = replies or {}
        self.received = []
        self.notifications = []

    @property
    def port(self):
        """int: The port the server listens on."""
        return self.server_address[1]

    def notify(self, msgs, errors, sticky=False, show_notification=False):
        """Record a notification instead of showing it (``ProfileLoader.notify``).

        Args:
            msgs (list): The messages.
            errors (list): The errors.
            sticky (bool): Whether the notification is sticky.
            show_notification (bool): Whether to show a popup.
        """
        self.notifications.append((msgs, sticky, show_notification))

    def respond(self, line):
        """Return the reply bytes (without EOT) for one command line.

        Args:
            line (str): The command line.

        Returns:
            bytes: The reply.
        """
        for prefix, reply in self.replies.items():
            if line.startswith(prefix):
                return reply
        data = split_command_line(line)
        if data == ["getappname"]:
            return self.appname.encode("utf-8")
        if data == ["getcfg", "profile.load_on_login"]:
            return b"profile.load_on_login = 0"
        if data[0] == "notify":
            plframe = types.SimpleNamespace(pl=self)
            return PLFrame.process_data(plframe, data).encode("utf-8")
        if (
            (data[0] == "setcfg" and len(data) == 3)
            or (data[0] == "setcfg" and len(data) == 4 and data[3] == "force")
            or (data[0] == "setlanguage" and len(data) == 2)
            or (
                data[0] == "apply-profiles"
                and data[1:] in ([], ["force"], ["display-changed"])
            )
        ):
            return b"ok"
        return b"invalid"


@pytest.fixture
def loader():
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


def _hosts_for(server, lock_name=LOADER_LOCK_NAME):
    return [f"127.0.0.1:{server.port} {lock_name}"]


# -- BaseFrame.send_command --------------------------------------------------


def test_send_command_delivers_command_and_returns_str(loader):
    """The command reaches the loader and the decoded reply is returned."""
    frame = FakeFrame(_hosts_for(loader))
    result = frame.send_command("apply-profiles", "getcfg profile.load_on_login")
    assert result == "profile.load_on_login = 0"
    assert loader.received == ["getappname", "getcfg profile.load_on_login"]


def test_send_command_ok_reply(loader):
    """A plain "ok" reply compares equal to the str "ok"."""
    frame = FakeFrame(_hosts_for(loader))
    command = "apply-profiles display-changed"
    assert frame.send_command("apply-profiles", command) == "ok"
    assert loader.received[-1] == command


def test_send_command_appname_match_is_case_insensitive(loader):
    """A loader run from source reports a lower-case name; still accept it."""
    loader.appname = LOADER_APPNAME.lower()
    frame = FakeFrame(_hosts_for(loader))
    command = "setcfg profile.load_on_login 1"
    assert frame.send_command("apply-profiles", command) == "ok"
    assert loader.received == ["getappname", command]


def test_send_command_foreign_app_returns_none(loader):
    """Another program on the (stale) port: no command sent, None returned."""
    loader.appname = "SomethingElse"
    frame = FakeFrame(_hosts_for(loader))
    command = "setcfg profile.load_on_login 1"
    assert frame.send_command("apply-profiles", command) is None
    assert loader.received == ["getappname"]


def test_send_command_tries_next_port_after_foreign_app(loader):
    """A stale foreign entry in the lock file does not hide the real host."""
    foreign = FakeScriptingHost(appname="SomethingElse")
    thread = threading.Thread(
        target=foreign.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        # Foreign entry first
        frame = FakeFrame(_hosts_for(foreign) + _hosts_for(loader))
        command = "apply-profiles display-changed"
        assert frame.send_command("apply-profiles", command) == "ok"
        assert loader.received == ["getappname", command]
        assert foreign.received == ["getappname"]
    finally:
        foreign.shutdown()
        foreign.server_close()
        thread.join(5)


def test_fake_loader_parses_notify_like_plframe(loader):
    """The fake applies PLFrame's notify argument check (2-4 arguments)."""
    frame = FakeFrame(_hosts_for(loader))
    assert frame.send_command("apply-profiles", 'notify "a b" silent') == "ok"
    assert frame.send_command("apply-profiles", "notify") == "invalid"
    assert loader.notifications == [(["a b"], False, False)]


def test_send_command_undecodable_reply(loader):
    """A reply that is not valid UTF-8 is decoded with replacement chars."""
    loader.replies = {"getcfg": b"caf\xe9"}
    frame = FakeFrame(_hosts_for(loader))
    assert frame.send_command("apply-profiles", "getcfg lang") == "caf�"


def test_send_command_not_running():
    """No lock file entry for the host: None."""
    frame = FakeFrame([f"127.0.0.1:1 {APPBASENAME}-curve-viewer"])
    assert frame.send_command("apply-profiles", "getcfg lang") is None


def test_send_command_connection_error_returns_exception():
    """A failed connect is returned as the exception (unchanged)."""
    frame = FakeFrame([f"127.0.0.1:1 {LOADER_LOCK_NAME}"])
    error = ConnectionRefusedError("refused")
    frame.connect = lambda ip, port: error
    assert frame.send_command("apply-profiles", "getcfg lang") is error


# -- display_cal.MainFrame callers -------------------------------------------


@pytest.fixture
def restore_cfg_defaults():
    """Restore DisplayCAL's in-memory default section after the test."""
    defaults = config.CFG.defaults()
    saved = dict(defaults)
    try:
        yield
    finally:
        defaults.clear()
        defaults.update(saved)


@pytest.fixture
def display_cal():
    """Import DisplayCAL.display_cal (heavy, so only when needed).

    Returns:
        module: The display_cal module.
    """
    return pytest.importorskip("DisplayCAL.display_cal")


def _on_login_frame(display_cal, hosts, checked):
    frame = FakeFrame(hosts, cls=display_cal.MainFrame)
    frame.profile_load_on_login = mock.Mock()
    frame.profile_load_on_login.GetValue.return_value = checked
    frame.profile_load_by_os = mock.Mock()
    frame.profile_load_by_os.GetValue.return_value = False
    frame.profile_load_by_os_handler = mock.Mock()
    return frame


def _toggle_load_on_login(display_cal, loader, monkeypatch):
    """Turn "load on login" on with a running loader that has it off.

    Returns:
        tuple: The mocked ``initcfg`` and ``writecfg``.
    """
    monkeypatch.setattr(display_cal, "is_superuser", lambda: False)
    monkeypatch.setattr(
        display_cal.util_win, "calibration_management_isenabled", lambda: False
    )
    initcfg = mock.Mock()
    writecfg = mock.Mock()
    monkeypatch.setattr(display_cal, "initcfg", initcfg)
    monkeypatch.setattr(display_cal, "writecfg", writecfg)
    frame = _on_login_frame(display_cal, _hosts_for(loader), checked=True)
    frame.profile_load_on_login_handler(event=object())
    return initcfg, writecfg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only code path")
def test_profile_load_on_login_handler_updates_running_loader(
    display_cal, loader, monkeypatch, restore_cfg_defaults
):
    """Toggling "load on login" reaches the running loader, no ini fallback."""
    initcfg, writecfg = _toggle_load_on_login(display_cal, loader, monkeypatch)

    assert loader.received[1] == "getcfg profile.load_on_login"
    assert loader.received[3] == "setcfg profile.load_on_login 1"
    # The loader had 0, the GUI now has 1: a notify command is sent
    assert loader.received[5].startswith("notify ")
    assert loader.received.count("getappname") == 3
    initcfg.assert_not_called()
    writecfg.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only code path")
def test_profile_load_on_login_handler_notify_accepted(
    display_cal, loader, monkeypatch, restore_cfg_defaults
):
    """The loader accepts the notify command and shows the new state."""
    _toggle_load_on_login(display_cal, loader, monkeypatch)

    text = display_cal.lang.getstr("calibration.preserve")
    assert loader.notifications == [([text], False, True)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only code path")
def test_profile_load_on_login_handler_falls_back_when_unreachable(
    display_cal, monkeypatch, restore_cfg_defaults
):
    """A loader that can't be reached is handled like one that isn't running."""
    monkeypatch.setattr(display_cal, "is_superuser", lambda: False)
    monkeypatch.setattr(
        display_cal.util_win, "calibration_management_isenabled", lambda: False
    )
    initcfg = mock.Mock()
    writecfg = mock.Mock()
    monkeypatch.setattr(display_cal, "initcfg", initcfg)
    monkeypatch.setattr(display_cal, "writecfg", writecfg)
    frame = _on_login_frame(
        display_cal, [f"127.0.0.1:1 {LOADER_LOCK_NAME}"], checked=False
    )
    frame.connect = lambda ip, port: ConnectionRefusedError("stale port")

    frame.profile_load_on_login_handler(event=object())

    initcfg.assert_called_once_with("apply-profiles", force_load=True)
    writecfg.assert_called_once()


def _set_remote_language(display_cal, loader, monkeypatch):
    monkeypatch.setattr(
        sys,
        "_appsocket",
        types.SimpleNamespace(getsockname=lambda: ("127.0.0.1", 1)),
        raising=False,
    )
    frame = FakeFrame(_hosts_for(loader), cls=display_cal.MainFrame)
    frame.set_remote_language()


def test_set_remote_language_updates_loader(display_cal, loader, monkeypatch, capsys):
    """setlanguage gets through and the notify command is sent."""
    _set_remote_language(display_cal, loader, monkeypatch)

    assert loader.received[0] == "getappname"
    assert loader.received[1].startswith("setlanguage ")
    assert loader.received[2].startswith("notify ")
    assert loader.received[2].endswith(" silent sticky")
    assert "couldn't set language" not in capsys.readouterr().out


def test_set_remote_language_loader_accepts_notify(
    display_cal, loader, monkeypatch, capsys
):
    """The loader accepts the notify command and updates its text."""
    _set_remote_language(display_cal, loader, monkeypatch)

    text = display_cal.lang.getstr("app.detected.calibration_loading_disabled", APPNAME)
    assert loader.notifications == [([text], True, False)]
    assert "couldn't update profile loader" not in capsys.readouterr().out


def test_set_remote_language_skips_self(display_cal, loader, monkeypatch):
    """A host that turns out to be the GUI itself is skipped."""
    loader.appname = APPNAME
    monkeypatch.setattr(
        sys,
        "_appsocket",
        types.SimpleNamespace(getsockname=lambda: ("0.0.0.0", 1)),
        raising=False,
    )
    frame = FakeFrame(_hosts_for(loader, APPBASENAME), cls=display_cal.MainFrame)

    frame.set_remote_language()

    assert loader.received == ["getappname"]


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
