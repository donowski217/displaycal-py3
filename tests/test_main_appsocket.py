"""Tests for the single-instance probe in DisplayCAL.main (AppSocket).

A lock file can keep the port of an instance that is gone (after a crash or a
kill). At the next start, DisplayCAL connects to that port and asks
"getappname". If a foreign service listens there now, its reply used to kill
startup with "Fatal error: 'utf-8' codec can't decode byte 0x92 ...", and a
service that never replied blocked startup forever.

Only loopback sockets owned by the tests are used.
"""

from __future__ import annotations

import contextlib
import io
import socket
import sys
import threading
import time

import pytest

HOST = "127.0.0.1"


@pytest.fixture(scope="module")
def main():
    """Import DisplayCAL.main without keeping its sys.excepthook."""
    saved_hook = sys.excepthook
    try:
        from DisplayCAL import main as main_module
    finally:
        sys.excepthook = saved_hook
    return main_module


def _recv_line(conn: socket.socket) -> str:
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8").strip()


class FakeServer:
    """Loopback server that runs handler(conn, server) for each connection."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.received = []
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((HOST, 0))
        self.sock.listen(5)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn, contextlib.suppress(OSError):
                self.handler(conn, self)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()


@pytest.fixture
def server():
    servers = []

    def start(handler) -> FakeServer:
        servers.append(FakeServer(handler))
        return servers[-1]

    yield start
    for fake_server in servers:
        fake_server.close()


def foreign_cp1252_reply(conn: socket.socket, server: FakeServer) -> None:
    """A foreign (non-DisplayCAL) service answering in its own encoding."""
    server.received.append(_recv_line(conn))
    conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\x92 Anfrage ung\xfcltig\r\n\r\n")


def silent_listener(conn: socket.socket, server: FakeServer) -> None:
    """Accepts the connection, then never replies and never closes it."""
    server.stop.wait(10)


def close_without_reply(conn: socket.socket, server: FakeServer) -> None:
    server.received.append(_recv_line(conn))


def utf8_split_across_recv_calls(conn: socket.socket, server: FakeServer) -> None:
    server.received.append(_recv_line(conn))
    data = ("x" * 1023 + "é" + "\x04").encode("utf-8")
    # The first recv(1024) ends in the middle of the two-byte "é"
    conn.sendall(data[:1024])
    time.sleep(0.2)
    conn.sendall(data[1024:])


def _connect(main, port: int):
    appsocket = main.AppSocket()
    assert appsocket.connect(HOST, port)
    assert appsocket.send("getappname")
    return appsocket


def test_read_escapes_non_utf8_reply(main, server):
    fake = server(foreign_cp1252_reply)
    appsocket = _connect(main, fake.port)
    try:
        data = appsocket.read()
    finally:
        appsocket.close()
    assert data == "HTTP/1.1 400 Bad Request\r\n\\x92 Anfrage ung\\xfcltig\r\n\r\n"


def test_read_decodes_utf8_split_across_recv_calls(main, server):
    fake = server(utf8_split_across_recv_calls)
    appsocket = _connect(main, fake.port)
    try:
        data = appsocket.read()
    finally:
        appsocket.close()
    assert data == "x" * 1023 + "é" + "\x04"


def test_read_tells_timeout_from_eof(main, server):
    silent = server(silent_listener)
    appsocket = _connect(main, silent.port)
    try:
        start = time.monotonic()
        assert appsocket.read(timeout=0.3) is None
        assert time.monotonic() - start < 5
        # The socket is blocking again afterwards
        assert appsocket.gettimeout() is None
    finally:
        appsocket.close()

    closing = server(close_without_reply)
    appsocket = _connect(main, closing.port)
    try:
        assert appsocket.read(timeout=5) == ""
    finally:
        appsocket.close()


def test_read_keeps_partial_reply_after_timeout(main, server):
    """A read() that times out must not lose what already arrived."""

    def slow_reply(conn: socket.socket, fake: FakeServer) -> None:
        fake.received.append(_recv_line(conn))
        conn.sendall("DisplayCAL-apply-".encode())
        time.sleep(1.0)
        conn.sendall("profiles ✓\x04".encode())

    fake = server(slow_reply)
    appsocket = _connect(main, fake.port)
    try:
        assert appsocket.read(timeout=0.3) is None
        assert appsocket.read(timeout=10) == "DisplayCAL-apply-profiles ✓\x04"
    finally:
        appsocket.close()


class _Lock:
    def __init__(self) -> None:
        self.unlocked = 0

    def unlock(self) -> None:
        self.unlocked += 1


def _run_probe(main, module: str, port: int):
    """Run connect_and_notify_running_instance() with a time limit.

    Returns:
        tuple: (result, lock, elapsed seconds)
    """
    lock = _Lock()
    outcome = {}

    def target() -> None:
        start = time.monotonic()
        try:
            outcome["result"] = main.connect_and_notify_running_instance(
                module, lock, HOST, [(None, port)]
            )
        except BaseException as exception:  # noqa: BLE001
            outcome["exception"] = exception
        outcome["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(15)
    assert not thread.is_alive(), "the single-instance probe hangs"
    if "exception" in outcome:
        raise outcome["exception"]
    return outcome["result"], lock, outcome["elapsed"]


def test_probe_treats_foreign_reply_as_not_our_instance(main, server):
    """The user's 'Fatal error: ... can't decode byte 0x92 in position 26'.

    stdout is cp1252 here (as for a source install started by the launcher),
    so printing the reply must not raise either.
    """
    fake = server(foreign_cp1252_reply)
    saved = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    try:
        result, lock, _ = _run_probe(main, "apply-profiles", fake.port)
    finally:
        sys.stdout = saved
    assert result is None
    assert lock.unlocked == 0
    assert fake.received == ["getappname"]


def _port_owner(monkeypatch, main, ours: bool) -> list[int]:
    """Make is_port_of_running_instance() answer `ours`; return the ports asked."""
    asked = []

    def is_port_of_running_instance(port: int) -> bool:
        asked.append(port)
        return ours

    monkeypatch.setattr(
        main, "is_port_of_running_instance", is_port_of_running_instance
    )
    return asked


def test_probe_gives_up_on_silent_foreign_listener(main, server, monkeypatch):
    monkeypatch.setattr(main.AppSocket, "PROBE_TIMEOUT", 0.3)
    asked = _port_owner(monkeypatch, main, ours=False)
    fake = server(silent_listener)
    result, lock, elapsed = _run_probe(main, "apply-profiles", fake.port)
    assert result is None
    assert lock.unlocked == 0
    assert elapsed < 5
    assert asked == [fake.port]


def test_probe_waits_for_our_instance_that_is_still_starting(main, server, monkeypatch):
    """An instance listens early, but only replies once its UI is set up.

    That can take longer than the probe's time limit (splash screen, display
    and instrument enumeration, first-run dialogs). It must still be notified
    instead of being mistaken for a foreign service, which would start a
    second instance.
    """
    monkeypatch.setattr(main, "PYNAME", "DisplayCAL")
    monkeypatch.setattr(main.AppSocket, "PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(sys, "argv", ["DisplayCAL.exe"])
    asked = _port_owner(monkeypatch, main, ours=True)

    def starting_gui_instance(conn: socket.socket, fake: FakeServer) -> None:
        fake.received.append(_recv_line(conn))
        time.sleep(0.5)
        conn.sendall(b"Display")
        time.sleep(0.5)
        conn.sendall(b"CAL\x04")
        fake.received.append(_recv_line(conn))
        conn.sendall(b"ok\x04")

    fake = server(starting_gui_instance)
    result, lock, elapsed = _run_probe(main, None, fake.port)

    assert fake.received == ["getappname", "DisplayCAL"]
    assert result == "ok"
    assert lock.unlocked == 0
    assert elapsed >= 1.0
    assert len(asked) >= 2
    assert set(asked) == {fake.port}


def _our_instance(reply_to_exit: bytes, delay: float):
    """A running profile loader that takes `delay` seconds to handle "exit"."""

    def handler(conn: socket.socket, server: FakeServer) -> None:
        server.received.append(_recv_line(conn))
        conn.sendall(b"DisplayCAL-apply-profiles\x04")
        server.received.append(_recv_line(conn))
        time.sleep(delay)
        if reply_to_exit:
            conn.sendall(reply_to_exit)

    return handler


@pytest.mark.parametrize(
    ("reply_to_exit", "expected"),
    [(b"", "ok"), (b"failed\x04", "failed")],
)
def test_slow_exit_handshake_is_not_cut_short(
    main, server, monkeypatch, reply_to_exit, expected
):
    """Only the getappname probe has a time limit.

    The running instance may take longer than that to handle "exit". A
    timeout there would be mistaken for success ("ok"), and two profile
    loaders could end up running.
    """
    monkeypatch.setattr(main, "PYNAME", "DisplayCAL-apply-profiles")
    monkeypatch.setattr(main.AppSocket, "PROBE_TIMEOUT", 0.3)
    fake = server(_our_instance(reply_to_exit, delay=1.0))

    result, lock, elapsed = _run_probe(main, "apply-profiles", fake.port)

    assert fake.received == ["getappname", "exit"]
    assert result == expected
    assert lock.unlocked == 1
    assert elapsed >= 1.0


def test_probe_still_recognizes_our_instance(main, server, monkeypatch):
    """A second start of the main application notifies the running one."""
    monkeypatch.setattr(main, "PYNAME", "DisplayCAL")
    monkeypatch.setattr(sys, "argv", ["DisplayCAL.exe"])

    def gui_instance(conn: socket.socket, fake: FakeServer) -> None:
        fake.received.append(_recv_line(conn))
        conn.sendall(b"DisplayCAL\x04")
        fake.received.append(_recv_line(conn))
        conn.sendall(b"ok\x04")

    fake = server(gui_instance)
    result, lock, _ = _run_probe(main, None, fake.port)

    assert fake.received == ["getappname", "DisplayCAL"]
    assert result == "ok"
    assert lock.unlocked == 0
