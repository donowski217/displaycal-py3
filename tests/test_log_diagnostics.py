"""Tests for the diagnostics side of DisplayCAL.log.

These tests never call setup_logging(): it would attach handlers to the root
logger (interfering with pytest's own logging capture and keeping files open,
which breaks tmp_path cleanup on Windows). Instead, log.LOGGER is replaced by a
private logger with a UTF-8 file handler for the duration of each test.
"""

from __future__ import annotations

import codecs
import contextlib
import gc
import io
import locale
import logging
import sys
import threading
import types
import uuid

import pytest

from DisplayCAL import log


class RecordingFileHandler(logging.FileHandler):
    """A real file handler which also keeps the records it emitted."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        super().emit(record)


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


@pytest.fixture
def app_log(tmp_path, monkeypatch):
    """Use a private logger with a UTF-8 file handler as the app logger."""
    logger = logging.getLogger(f"test_log_diagnostics.app.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    path = tmp_path / "app.log"
    handler = RecordingFileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    monkeypatch.setattr(log, "LOGGER", logger)
    monkeypatch.setattr(log, "_LOGDIR", str(tmp_path))
    yield logger, path
    _close_logger(logger)


def _messages(logger: logging.Logger) -> list[tuple[int, str]]:
    """Return (level, message) of every record the app log handler emitted."""
    return [(r.levelno, r.getMessage()) for r in logger.handlers[0].records]


@pytest.fixture
def file_loggers():
    """Collect loggers created by get_file_logger() and close them afterwards."""
    loggers = []
    yield loggers
    for logger in loggers:
        _close_logger(logger)


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# get_file_logger() must not replace the application logger


def test_session_logfile_does_not_hijack_app_log(app_log, tmp_path, file_loggers):
    """LOG() keeps writing to the app log while and after a session log exists."""
    app_logger, app_log_path = app_log
    log.LOG("line before session")
    session = log.LogFile(_unique_name("MyProfile"), str(tmp_path / "session"))
    file_loggers.append(session._logger)
    try:
        assert log.LOGGER is app_logger
        log.LOG("line during session")
        session.write("session only line")
    finally:
        session.close()
    log.LOG("line after session")

    assert log.LOGGER is app_logger
    app_text = app_log_path.read_text(encoding="utf-8")
    assert "line before session" in app_text
    assert "line during session" in app_text
    assert "line after session" in app_text
    assert "session only line" not in app_text
    session_text = (
        tmp_path / "session" / f"{session.filename}.log"
    ).read_text(encoding="utf-8")
    assert "session only line" in session_text
    assert "line during session" not in session_text


def test_named_file_logger_does_not_hijack_app_log(app_log, tmp_path, file_loggers):
    """get_file_logger('interact') etc. returns its logger without rebinding LOGGER."""
    app_logger, app_log_path = app_log
    name = _unique_name("interact")
    interact = log.get_file_logger(name)
    file_loggers.append(interact)

    assert interact is logging.getLogger(name)
    assert log.LOGGER is app_logger
    log.LOG("app line")
    interact.info("interact line")
    assert "app line" in app_log_path.read_text(encoding="utf-8")
    interact_text = (tmp_path / f"{name}.log").read_text(encoding="utf-8")
    assert "interact line" in interact_text
    assert "app line" not in interact_text


# Log files are always UTF-8


@pytest.fixture
def cp1252_locale(monkeypatch):
    """Make the 'locale' text encoding cp1252, as on the user's machine.

    So the encoding tests do not depend on the locale (or UTF-8 mode) of the
    machine running them.
    """
    if hasattr(io, "text_encoding"):
        monkeypatch.setattr(
            io,
            "text_encoding",
            lambda encoding, stacklevel=2: (
                "cp1252" if encoding in (None, "locale") else encoding
            ),
        )
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args: "cp1252")


NON_CP1252_TEXT = "box ─┐ arrow → CJK 漢字 approx ≈ Δ"


@pytest.mark.parametrize("when", ["never", "midnight"])
def test_file_logger_writes_utf8(cp1252_locale, tmp_path, file_loggers, when):
    """Records with characters outside the locale code page are not dropped."""
    name = _unique_name("encoding")
    logger = log.get_file_logger(name, when=when, logdir=str(tmp_path))
    file_loggers.append(logger)
    handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert handlers
    for handler in handlers:
        assert codecs.lookup(handler.encoding).name == "utf-8"

    logger.info(NON_CP1252_TEXT)
    logger.info("plain ascii line")
    for handler in handlers:
        handler.flush()

    text = (tmp_path / f"{name}.log").read_text(encoding="utf-8")
    assert NON_CP1252_TEXT in text
    assert "plain ascii line" in text


# Standard streams that discard their output are redirected to the app log


class Blackhole:
    """Stand-in for py2exe's sys.stdout of windowed exes: discards everything."""

    softspace = 0

    def __init__(self) -> None:
        self.written = []

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class Stderr:
    """Stand-in for py2exe's sys.stderr of windowed exes (%APPDATA% log file)."""

    def __init__(self) -> None:
        self.written = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass




@contextlib.contextmanager
def _stdio(stdout: object, stderr: object):
    """Temporarily replace sys.stdout and sys.stderr.

    Done inside the test body (not with monkeypatch in a fixture), because
    pytest's output capturing re-installs its own streams when the test call
    starts and ends.
    """
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def stdio_env(app_log, monkeypatch):
    """Isolate the stdio redirection state of the log module."""
    registered = []
    monkeypatch.setattr(log, "_STDIO_LOG_STREAMS", [])
    monkeypatch.setattr(
        log,
        "atexit",
        types.SimpleNamespace(register=lambda fn, *a, **kw: registered.append(fn)),
    )
    return types.SimpleNamespace(
        logger=app_log[0],
        path=app_log[1],
        atexit_registered=registered,
        blackhole=Blackhole(),
        shim=Stderr(),
    )


def test_redirect_stdio_to_log_in_windowed_build(stdio_env):
    """print() and stderr output reach the app log; stderr is passed through."""
    env = stdio_env
    with _stdio(env.blackhole, env.shim):
        assert log.redirect_stdio_to_log() is True
        stdout, stderr = sys.stdout, sys.stderr
        print("Acquired lock file: banner", 42)
        sys.stderr.write("Traceback line on stderr\n")
        sys.stdout.flush()
        sys.stderr.flush()
        # A second call does nothing
        assert log.redirect_stdio_to_log() is False
        assert sys.stdout is stdout and sys.stderr is stderr

    assert isinstance(stdout, log.StdioToLog)
    assert isinstance(stderr, log.StdioToLog)
    assert stdout.original is env.blackhole
    assert stderr.original is env.shim
    assert stdout.is_log_stream and stderr.is_log_stream
    # Like py2exe's Blackhole, no isatty()/fileno(): Worker.exec_cmd checks
    # hasattr(sys.stdout, "isatty") to decide how to capture subprocess output
    for stream in (stdout, stderr):
        assert not hasattr(stream, "isatty")
        assert not hasattr(stream, "fileno")

    assert (logging.INFO, "Acquired lock file: banner 42") in _messages(env.logger)
    assert (logging.ERROR, "Traceback line on stderr") in _messages(env.logger)
    text = env.path.read_text(encoding="utf-8")
    assert "Acquired lock file: banner 42" in text
    assert "Traceback line on stderr" in text
    # py2exe's stderr log (and its message at exit) keep working
    assert "".join(env.shim.written) == "Traceback line on stderr\n"
    assert env.blackhole.written == []
    assert env.atexit_registered == [log._flush_stdio_log_streams]


def test_redirect_stdio_to_log_handles_pythonw_none(stdio_env):
    """Under pythonw sys.stdout and sys.stderr are None."""
    with _stdio(None, None):
        assert log.redirect_stdio_to_log() is True
        stdout, stderr = sys.stdout, sys.stderr
        print("pythonw print")
        sys.stderr.write("pythonw stderr\n")

    assert stdout.original is None and stderr.original is None
    assert (logging.INFO, "pythonw print") in _messages(stdio_env.logger)
    assert (logging.ERROR, "pythonw stderr") in _messages(stdio_env.logger)


def test_redirect_stdio_leaves_console_and_test_runs_alone(stdio_env):
    """Real streams (console, pipes, pytest capture) are never replaced."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with _stdio(stdout, stderr):
        assert log.redirect_stdio_to_log() is False
        assert sys.stdout is stdout and sys.stderr is stderr
    assert stdio_env.atexit_registered == []


def test_redirect_stdio_needs_app_log(stdio_env, monkeypatch):
    """Nothing is redirected while there is no app log file handler."""
    monkeypatch.setattr(log, "LOGGER", None)
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        assert log.redirect_stdio_to_log() is False
        assert sys.stdout is stdio_env.blackhole
        assert sys.stderr is stdio_env.shim


def test_setup_logging_redirects_stdio(stdio_env, monkeypatch, tmp_path):
    """setup_logging() installs the redirection once the app log exists."""
    app_logger = stdio_env.logger
    monkeypatch.setattr(log, "LOGGER", None)
    monkeypatch.setattr(log, "get_file_logger", lambda *args, **kwargs: app_logger)
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.setup_logging(str(tmp_path), "DisplayCAL-apply-profiles", ".exe")
        stdout, stderr = sys.stdout, sys.stderr
    assert log.LOGGER is app_logger
    assert isinstance(stdout, log.StdioToLog)
    assert isinstance(stderr, log.StdioToLog)


def test_stdio_to_log_passes_through_without_app_log(monkeypatch):
    """Without an app log file handler, text goes to the original stream."""
    monkeypatch.setattr(log, "LOGGER", None)
    original = io.StringIO()
    stream = log.StdioToLog(logging.INFO, original)
    stream.write("partial ")
    stream.write("line\n")
    assert original.getvalue() == "partial line\n"


def test_stdio_to_log_accepts_bytes_and_carriage_returns(stdio_env):
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        sys.stdout.write(b"bytes \x92 line\n")
        sys.stdout.write("progress 1\rprogress 2\r\n")
    messages = [m for _, m in _messages(stdio_env.logger)]
    assert messages == ["bytes \\x92 line", "progress 1", "progress 2"]


class StderrWritingHandler(RecordingFileHandler):
    """A file handler which reports something on sys.stderr while emitting."""

    def emit(self, record: logging.LogRecord) -> None:
        sys.stderr.write(f"handler noise for {record.getMessage()}\n")
        super().emit(record)


@pytest.fixture
def stderr_writing_handler(stdio_env, tmp_path):
    logger = stdio_env.logger
    _close_logger(logger)
    handler = StderrWritingHandler(tmp_path / "noisy.log", encoding="utf-8")
    logger.addHandler(handler)
    return handler


def test_stdio_to_log_does_not_recurse(stdio_env, stderr_writing_handler):
    """A handler writing to sys.stderr while emitting is passed through only."""
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        print("stdout line")
        sys.stderr.write("stderr line\n")

    messages = [m for _, m in _messages(stdio_env.logger)]
    assert messages == ["stdout line", "stderr line"]
    shim_text = "".join(stdio_env.shim.written)
    assert "handler noise for stdout line\n" in shim_text
    assert "handler noise for stderr line\n" in shim_text


def test_stdio_to_log_two_threads_do_not_deadlock(stdio_env, stderr_writing_handler):
    """Concurrent stdout and stderr writers with a stderr-writing handler.

    An earlier design held a per-stream lock while calling the handlers, which
    deadlocked against the handler's lock within seconds in this scenario.
    """
    count = 300

    def print_lines() -> None:
        for i in range(count):
            print("stdout", i)

    def write_stderr_lines() -> None:
        for i in range(count):
            sys.stderr.write(f"stderr {i}\n")

    threads = [
        threading.Thread(target=print_lines, daemon=True),
        threading.Thread(target=write_stderr_lines, daemon=True),
    ]
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        alive = [thread for thread in threads if thread.is_alive()]
    assert not alive

    messages = {m for _, m in _messages(stdio_env.logger)}
    assert {f"stdout {i}" for i in range(count)} <= messages
    assert {f"stderr {i}" for i in range(count)} <= messages


def test_stdio_to_log_collapses_repeated_lines(stdio_env):
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        for _ in range(5):
            print("DisplayConfigurationMonitoringThread: Acquired lock")
        print("Display configuration changed")

    assert _messages(stdio_env.logger) == [
        (logging.INFO, "DisplayConfigurationMonitoringThread: Acquired lock"),
        (logging.INFO, "(last message repeated 4 times)"),
        (logging.INFO, "Display configuration changed"),
    ]


def test_stdio_to_log_keeps_a_heartbeat_while_collapsing(stdio_env, monkeypatch):
    """A repeat summary is written periodically, so the log shows liveness."""
    clock = [1000.0]
    monkeypatch.setattr(log, "time", lambda: clock[0])
    interval = log.StdioToLog.repeat_summary_interval
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        print("Acquired lock")
        for _ in range(3):
            clock[0] += interval / 2
            print("Acquired lock")
    # The process exits here; at exit the remaining count is written
    log._flush_stdio_log_streams()

    assert [m for _, m in _messages(stdio_env.logger)] == [
        "Acquired lock",
        "(last message repeated 2 times)",
        "(last message repeated 1 time)",
    ]


def test_stdio_to_log_writes_partial_line_at_exit(stdio_env):
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        print("Menu command:", end=" ")
        sys.stdout.flush()
    assert _messages(stdio_env.logger) == []

    assert stdio_env.atexit_registered == [log._flush_stdio_log_streams]
    stdio_env.atexit_registered[0]()
    assert _messages(stdio_env.logger) == [(logging.INFO, "Menu command:")]


def _raise_in_thread() -> None:
    raise RuntimeError("thread boom")


def test_thread_exception_reaches_app_log(stdio_env):
    """The default threading.excepthook writes to sys.stderr, i.e. the log.

    pytest replaces threading.excepthook while a test runs, so use the default
    one here.
    """
    saved_hook = threading.excepthook
    threading.excepthook = threading.__excepthook__
    try:
        with _stdio(stdio_env.blackhole, stdio_env.shim):
            log.redirect_stdio_to_log()
            thread = threading.Thread(target=_raise_in_thread, name="OtherThread")
            thread.start()
            thread.join(10)
    finally:
        threading.excepthook = saved_hook

    text = stdio_env.path.read_text(encoding="utf-8")
    assert "Exception in thread OtherThread" in text
    assert "_raise_in_thread" in text
    assert "RuntimeError: thread boom" in text
    assert "RuntimeError: thread boom" in "".join(stdio_env.shim.written)


class _RaisesInDel:
    def __del__(self) -> None:
        raise RuntimeError("del boom")


def test_unraisable_exception_reaches_app_log(stdio_env):
    """The default sys.unraisablehook writes to sys.stderr, i.e. the log.

    pytest replaces sys.unraisablehook while a test runs, so use the default
    one here.
    """
    saved_hook = sys.unraisablehook
    sys.unraisablehook = sys.__unraisablehook__
    try:
        with _stdio(stdio_env.blackhole, stdio_env.shim):
            log.redirect_stdio_to_log()
            obj = _RaisesInDel()
            del obj
            gc.collect()
    finally:
        sys.unraisablehook = saved_hook

    text = stdio_env.path.read_text(encoding="utf-8")
    assert "Exception ignored in" in text
    assert "RuntimeError: del boom" in text
