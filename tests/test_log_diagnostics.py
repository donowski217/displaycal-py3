"""Tests for the diagnostics side of DisplayCAL.log.

These tests never call setup_logging(): it would attach handlers to the root
logger (interfering with pytest's own logging capture and keeping files open,
which breaks tmp_path cleanup on Windows). Instead, log.LOGGER is replaced by a
private logger with a UTF-8 file handler for the duration of each test.
"""

from __future__ import annotations

import codecs
import contextlib
import faulthandler
import gc
import io
import locale
import logging
import os
import subprocess
import sys
import threading
import traceback
import types
import uuid
import weakref

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
    session_text = (tmp_path / "session" / f"{session.filename}.log").read_text(
        encoding="utf-8"
    )
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
    # Streams left over by other tests must not show up in this test's log
    monkeypatch.setattr(log, "_ALL_STDIO_LOG_STREAMS", weakref.WeakSet())
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
    # Text the app log took does not also go to py2exe's stderr log, whose
    # first write arms an "Errors in ..." message box at exit
    assert env.shim.written == []
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


def _summary(line: str, count: int) -> str:
    """Return the repeat summary StdioToLog writes for line."""
    return f'(previous line repeated {count} time{"" if count == 1 else "s"}: "{line}")'


def test_stdio_to_log_collapses_repeated_lines(stdio_env):
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        for _ in range(5):
            print("DisplayConfigurationMonitoringThread: Acquired lock")
        print("Display configuration changed")

    assert _messages(stdio_env.logger) == [
        (logging.INFO, "DisplayConfigurationMonitoringThread: Acquired lock"),
        (
            logging.INFO,
            _summary("DisplayConfigurationMonitoringThread: Acquired lock", 4),
        ),
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
        _summary("Acquired lock", 2),
        _summary("Acquired lock", 1),
    ]


def test_stdio_to_log_logs_repeats_separated_by_other_records(
    stdio_env: types.SimpleNamespace,
) -> None:
    """Only a copy of the previous record in the log file is collapsed.

    The log file is shared by both streams, LOG()/safe_print() and
    log_error(): a line repeated after other records is a new event (e.g. a
    retry after a traceback, or the same error again minutes later) and must
    stay in place in the timeline.
    """
    applying = "Applying calibration to display 1"
    wcs_error = "WCS call failed: timeout"
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        print(applying)
        try:
            raise OSError(31, "SetDeviceGammaRamp failed")
        except OSError:
            traceback.print_exc()
        print(applying)
        log.LOG("Calibration applied")
        print(applying)
        print("Display 1 done")
        sys.stderr.write(f"{wcs_error}\n")
        print("Next iteration")
        sys.stderr.write(f"{wcs_error}\n")
        log.log_error("Something else failed")
        sys.stderr.write(f"{wcs_error}\n")
        print("Exiting")
    log._flush_stdio_log_streams()

    messages = _messages(stdio_env.logger)
    assert not [m for _, m in messages if "repeated" in m]
    traceback_end = messages.index(
        (logging.ERROR, "OSError: [Errno 31] SetDeviceGammaRamp failed")
    )
    assert messages[0] == (logging.INFO, applying)
    assert messages[1] == (logging.ERROR, "Traceback (most recent call last):")
    assert messages[traceback_end + 1 :] == [
        (logging.INFO, applying),
        (logging.INFO, "Calibration applied"),
        (logging.INFO, applying),
        (logging.INFO, "Display 1 done"),
        (logging.ERROR, wcs_error),
        (logging.INFO, "Next iteration"),
        (logging.ERROR, wcs_error),
        (logging.ERROR, "Something else failed"),
        (logging.ERROR, wcs_error),
        (logging.INFO, "Exiting"),
    ]


@pytest.mark.parametrize("source", ["LOG", "stderr", "log_error", "child logger"])
def test_stdio_to_log_writes_repeat_summary_before_next_record(
    stdio_env: types.SimpleNamespace, source: str
) -> None:
    """The summary follows the line it counts, before any other record."""
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        for _ in range(3):
            print("Acquired lock")
        if source == "LOG":
            log.LOG("Other record")
        elif source == "stderr":
            sys.stderr.write("Other record\n")
        elif source == "log_error":
            log.log_error("Other record")
        else:
            logging.getLogger(f"{stdio_env.logger.name}.child").warning("Other record")
        print("Acquired lock")

    expected = [
        "Acquired lock",
        _summary("Acquired lock", 2),
        "Other record",
        "Acquired lock",
    ]
    assert [m for _, m in _messages(stdio_env.logger)] == expected
    file_lines = stdio_env.path.read_text(encoding="utf-8").splitlines()
    # "<date> <time> <message>"
    assert [line.split(" ", 2)[2] for line in file_lines] == expected


def test_stdio_to_log_repeat_summary_quotes_long_lines_shortened(
    stdio_env: types.SimpleNamespace,
) -> None:
    """A summary quotes at most max_summary_quote_length characters."""
    long_line = "x" * 300
    with _stdio(stdio_env.blackhole, stdio_env.shim):
        log.redirect_stdio_to_log()
        print(long_line)
        print(long_line)
        print("end")

    assert [m for _, m in _messages(stdio_env.logger)] == [
        long_line,
        _summary("x" * 200 + "...", 1),
        "end",
    ]


class _LocalLogsWhileLocked(threading.local):
    """Thread-local storage of a StdioToLog stream, used under its lock.

    Logs a record directly to the app logger the first time the line buffer
    is looked up while armed, like a finalizer calling LOG() when a garbage
    collection interrupts the stream's bookkeeping.
    """

    def __init__(self, logger: logging.Logger, text: str) -> None:
        self.logger = logger
        self.text = text
        self.armed = False
        self._buffer = None

    @property
    def buffer(self) -> object:
        if self.armed:
            self.armed = False
            self.logger.info(self.text)
        return self._buffer

    @buffer.setter
    def buffer(self, value: object) -> None:
        self._buffer = value


def test_stdio_to_log_record_interrupting_bookkeeping_with_pending_summary(
    stdio_env: types.SimpleNamespace,
) -> None:
    """A record logged while the stream holds its lock does not take its lock.

    The pending summary then follows that record, still naming its line.
    """
    stream = log.StdioToLog(logging.INFO, stdio_env.blackhole)
    stream._local = _LocalLogsWhileLocked(stdio_env.logger, "record from finalizer")

    def scenario() -> None:
        stream.write("Acquired lock\n")
        stream.write("Acquired lock\n")
        stream._local.armed = True
        stream.write("Acquired lock\n")
        stream.write("Display configuration changed\n")

    thread = _run_with_time_limit(scenario)

    assert not thread.is_alive(), "the thread deadlocked on the stream's own lock"
    assert [m for _, m in _messages(stdio_env.logger)] == [
        "Acquired lock",
        "record from finalizer",
        _summary("Acquired lock", 1),
        "Acquired lock",
        "Display configuration changed",
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
    assert "RuntimeError: thread boom" not in "".join(stdio_env.shim.written)


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


# A write from a finalizer while the stream holds its lock must not deadlock


class _WritesWhileLocked(weakref.WeakSet):
    """The set of line buffers of a StdioToLog stream, used under its lock.

    Writes to the stream once, the first time it is used while armed. That is
    what a finalizer does if a garbage collection runs it in the middle of the
    stream's bookkeeping: e.g. the default sys.unraisablehook writes
    "Exception ignored in ..." to sys.stderr.
    """

    def __init__(self, stream: log.StdioToLog, text: str) -> None:
        super().__init__()
        self.stream = stream
        self.text = text
        self.armed = True

    def _finalizer_writes(self) -> None:
        if self.armed:
            self.armed = False
            self.stream.write(self.text)

    def add(self, item: object) -> None:
        self._finalizer_writes()
        super().add(item)

    def __iter__(self):
        self._finalizer_writes()
        yield from super().__iter__()


FINALIZER_TEXT = "Exception ignored in: <function Finalizer.__del__>\n"


def _run_with_time_limit(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    thread.join(10)
    return thread


def test_stdio_to_log_write_from_finalizer_on_first_write(stdio_env):
    """A thread's first write registers its buffer under the stream's lock."""
    stream = log.StdioToLog(logging.ERROR, stdio_env.shim, passthrough=True)
    stream._buffers = _WritesWhileLocked(stream, FINALIZER_TEXT)

    thread = _run_with_time_limit(stream.write, "first line of a new thread\n")

    assert not thread.is_alive(), "the thread deadlocked on the stream's own lock"
    assert not stream._buffers.armed
    assert [m for _, m in _messages(stdio_env.logger)] == [
        "first line of a new thread",
        FINALIZER_TEXT.rstrip(),
    ]
    assert "".join(stdio_env.shim.written) == (
        f"first line of a new thread\n{FINALIZER_TEXT}"
    )


def test_stdio_to_log_write_from_finalizer_while_flushing(stdio_env):
    """flush_pending() (at exit) goes through all buffers under the lock."""
    stream = log.StdioToLog(logging.ERROR, stdio_env.shim, passthrough=True)
    stream._buffers = _WritesWhileLocked(stream, FINALIZER_TEXT)
    stream._buffers.armed = False
    stream.write("partial line at exit")
    stream._buffers.armed = True

    thread = _run_with_time_limit(stream.flush_pending)

    assert not thread.is_alive(), "flush_pending() deadlocked on the stream's lock"
    assert not stream._buffers.armed
    assert [m for _, m in _messages(stdio_env.logger)] == [
        "partial line at exit",
        FINALIZER_TEXT.rstrip(),
    ]


GC_FINALIZER_CHAIN_SCRIPT = """
import faulthandler
import gc
import logging
import sys
import threading

from DisplayCAL import log

scenario, log_path, dump_path = sys.argv[1:4]
dump = open(dump_path, "w", encoding="utf-8")
faulthandler.dump_traceback_later(20, exit=True, file=dump)

logger = logging.getLogger("gc_finalizer_chain")
logger.propagate = False
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))
log.LOGGER = logger


class Stderr:
    def write(self, text):
        pass

    def flush(self):
        pass


sys.stderr = log.StdioToLog(logging.ERROR, Stderr(), passthrough=True)
sys.unraisablehook = sys.__unraisablehook__
STOP = False


class GarbageChain:
    # Cyclic garbage whose finalizer raises and leaves the next cycle behind,
    # so a garbage collection can run a finalizer at nearly any allocation.

    def __init__(self, remaining):
        self.remaining = remaining
        self.cycle = self

    def __del__(self):
        if self.remaining and not STOP:
            GarbageChain(self.remaining - 1)
        raise RuntimeError(f"chain link {self.remaining}")


def first_write():
    GarbageChain(2000)
    sys.stderr.write("first line of a new thread\\n")


def flush_at_exit():
    GarbageChain(2000)
    sys.stderr.flush_pending()


if scenario == "flush":
    sys.stderr.write("partial line at exit")
    target = flush_at_exit
else:
    target = first_write
gc.set_threshold(1)
thread = threading.Thread(target=target, daemon=True)
thread.start()
thread.join()
STOP = True
gc.set_threshold(700, 10, 10)
gc.collect()
faulthandler.cancel_dump_traceback_later()
"""


@pytest.mark.parametrize("scenario", ["first-write", "flush"])
def test_stdio_to_log_survives_gc_finalizers_writing_to_stderr(tmp_path, scenario):
    """The real thing: the default sys.unraisablehook run by the garbage collector.

    With gc.set_threshold(1), nearly every allocation can run a finalizer,
    also while the stream holds its lock. Runs in a child process, so a
    deadlock cannot hang the test run (faulthandler ends it after 20 s and
    dumps the stacks).
    """
    log_path = tmp_path / "app.log"
    dump_path = tmp_path / "stacks.txt"
    root = os.path.dirname(os.path.dirname(os.path.abspath(log.__file__)))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            GC_FINALIZER_CHAIN_SCRIPT,
            scenario,
            str(log_path),
            str(dump_path),
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    stacks = dump_path.read_text(encoding="utf-8") if dump_path.exists() else ""
    assert result.returncode == 0, f"deadlocked or failed:\n{stacks}{result.stderr}"
    text = log_path.read_text(encoding="utf-8")
    if scenario == "flush":
        assert "partial line at exit" in text
    else:
        assert "first line of a new thread" in text
    assert "Exception ignored in" in text
    assert "RuntimeError: chain link" in text


# The faulthandler log is appended to, not truncated


PREVIOUS_DUMP = (
    "Windows fatal exception: access violation\n\n"
    "Current thread 0x00001234 (most recent call first):\n"
    '  File "mscms.py", line 42 in get_calibration_management_state\n'
)


def test_open_fault_log_keeps_previous_crash_dump(tmp_path):
    path = tmp_path / "DisplayCAL-apply-profiles-fault.log"
    path.write_text(PREVIOUS_DUMP, encoding="utf-8")

    fault_log = log.open_fault_log(str(path), "DisplayCAL-apply-profiles.exe 3.10")
    fault_log.close()

    text = path.read_text(encoding="utf-8")
    assert text.startswith(PREVIOUS_DUMP)
    header = text[len(PREVIOUS_DUMP) :]
    assert header.startswith("=== ")
    assert header.endswith(
        f" DisplayCAL-apply-profiles.exe 3.10 PID {os.getpid()} started ===\n"
    )


def test_open_fault_log_moves_big_file_aside(tmp_path):
    path = tmp_path / "DisplayCAL-fault.log"
    path.write_text(PREVIOUS_DUMP * 100, encoding="utf-8")

    fault_log = log.open_fault_log(str(path), "DisplayCAL", max_size=1000)
    fault_log.close()

    prev = tmp_path / "DisplayCAL-fault.log.prev"
    assert prev.read_text(encoding="utf-8") == PREVIOUS_DUMP * 100
    text = path.read_text(encoding="utf-8")
    assert text.startswith("=== ")
    assert text.endswith(" started ===\n")
    assert text.count("\n") == 1


def test_open_fault_log_works_with_faulthandler(tmp_path):
    """faulthandler writes to the file descriptor of the returned file."""
    path = tmp_path / "DisplayCAL-fault.log"
    fault_log = log.open_fault_log(str(path), "DisplayCAL")
    try:
        faulthandler.dump_traceback(file=fault_log, all_threads=False)
        log.write_fault_log_footer(fault_log, "DisplayCAL")
    finally:
        fault_log.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(" started ===")
    assert any("test_open_fault_log_works_with_faulthandler" in line for line in lines)
    assert lines[-1].endswith(f" DisplayCAL PID {os.getpid()} exited ===")


def test_write_fault_log_footer_never_raises(tmp_path):
    fault_log = log.open_fault_log(str(tmp_path / "fault.log"), "DisplayCAL")
    fault_log.close()
    log.write_fault_log_footer(fault_log, "DisplayCAL")


# Multiprocessing children must not pose as another application instance

LOADER = "DisplayCAL-apply-profiles"
FROZEN_CHILD_ARGV = [
    r"C:\Program Files\DisplayCAL\DisplayCAL-apply-profiles.exe",
    "--multiprocessing-fork",
    "parent_pid=1234",
    "pipe_handle=568",
]


@pytest.fixture
def loader_dirs(tmp_path, monkeypatch):
    """Config and log dirs of a running profile loader (as seen by its child)."""
    confighome = tmp_path / "config"
    logdir = tmp_path / "logs"
    confighome.mkdir()
    logdir.mkdir()
    # The running parent's lock file: one line with its port
    (confighome / f"{LOADER}.lock").write_text("15411\n")
    (logdir / f"{LOADER}.log").write_text("parent log\n")
    (logdir / f"{LOADER}.1.log").write_text("")
    registered = []
    monkeypatch.setattr(
        log,
        "atexit",
        types.SimpleNamespace(register=lambda fn, *a: registered.append((fn, a))),
    )
    return types.SimpleNamespace(confighome=confighome, logdir=logdir)


def _set_mtimes(logdir, main_is_older: bool) -> None:
    older, newer = 1_000_000_000, 1_000_100_000
    os.utime(
        logdir / f"{LOADER}.log", (older, older) if main_is_older else (newer, newer)
    )
    os.utime(
        logdir / f"{LOADER}.1.log", (newer, newer) if main_is_older else (older, older)
    )


def test_main_process_is_not_a_multiprocessing_child(monkeypatch):
    monkeypatch.setattr(sys, "argv", [r"C:\DisplayCAL\DisplayCAL-apply-profiles.exe"])
    assert log.is_multiprocessing_child() is False


def test_named_child_process_is_a_multiprocessing_child(monkeypatch):
    monkeypatch.setattr(
        log.mp, "current_process", lambda: types.SimpleNamespace(name="Process-1")
    )
    assert log.is_multiprocessing_child() is True


@pytest.mark.parametrize("main_is_older", [True, False])
def test_frozen_multiprocessing_child_is_detected_before_freeze_support(
    loader_dirs, monkeypatch, main_is_older
):
    """A frozen child imports DisplayCAL before freeze_support() names it.

    At that point its process name is still "MainProcess", so it used to
    count the parent's lock file line as another instance and pick (and
    rotate) the parent's log or the ".1" log: the 0-byte
    DisplayCAL-apply-profiles.1.log files.
    """
    monkeypatch.setattr(sys, "argv", FROZEN_CHILD_ARGV)
    _set_mtimes(loader_dirs.logdir, main_is_older)
    assert log.is_multiprocessing_child() is True

    result = log.update_filename_for_confighome(
        "midnight",
        str(loader_dirs.logdir),
        LOADER,
        str(loader_dirs.confighome),
        "a",
    )

    assert result == ("never", f"{LOADER}.mp-worker-1", "w")


def test_frozen_multiprocessing_child_leaves_parent_logs_alone(
    loader_dirs, monkeypatch, file_loggers
):
    monkeypatch.setattr(sys, "argv", FROZEN_CHILD_ARGV)
    _set_mtimes(loader_dirs.logdir, main_is_older=False)
    # Lock file of another live worker of the same parent
    sibling_lock = loader_dirs.confighome / f"{LOADER}.mp-worker-1.lock"
    sibling_lock.write_text("")
    before = {p.name: p.stat().st_mtime for p in loader_dirs.logdir.iterdir()}

    logger = log.get_file_logger(
        _unique_name("child"),
        logdir=str(loader_dirs.logdir),
        filename=LOADER,
        confighome=str(loader_dirs.confighome),
    )
    file_loggers.append(logger)

    assert [os.path.basename(h.baseFilename) for h in logger.handlers] == [
        f"{LOADER}.mp-worker-2.log"
    ]
    after = {p.name: p.stat().st_mtime for p in loader_dirs.logdir.iterdir()}
    assert after.pop(f"{LOADER}.mp-worker-2.log", None) is not None
    assert after == before
    assert sibling_lock.exists()


@pytest.mark.parametrize("main_is_older", [True, False])
def test_second_main_process_still_gets_instance_log(
    loader_dirs, monkeypatch, main_is_older
):
    """A genuine second instance keeps using the '.1' logic."""
    monkeypatch.setattr(sys, "argv", [FROZEN_CHILD_ARGV[0], "--task"])
    _set_mtimes(loader_dirs.logdir, main_is_older)
    when, filename, mode = log.update_filename_for_confighome(
        "midnight",
        str(loader_dirs.logdir),
        LOADER,
        str(loader_dirs.confighome),
        "a",
    )
    assert (when, mode) == ("midnight", "a")
    assert filename == (LOADER if main_is_older else f"{LOADER}.1")
