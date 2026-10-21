"""Tests for DisplayCAL.debughelpers.handle_error().

handle_error() used to start with traceback.print_exc() and to use
traceback.format_exc() for exception instances. Both only see the exception
*currently being handled*, so whenever handle_error() was called outside an
``except`` block (from sys.excepthook, a wx/pywin32 callback via PyErr_Print,
or for an exception caught in another thread) the logs only got
"NoneType: None", and the real traceback was printed to stdout, which py2exe's
windowed builds discard.

No dialog is ever shown: show_error_dialog is replaced by a recorder.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
import types
import uuid

import pytest

from DisplayCAL import debughelpers, log
from DisplayCAL.debughelpers import (
    Error,
    format_error_traceback,
    handle_error,
    log_error_traceback,
    print_error_traceback,
)


class RecordingFileHandler(logging.FileHandler):
    """A real file handler which also keeps the records it emitted."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        super().emit(record)


@pytest.fixture
def app_log(tmp_path, monkeypatch):
    """Use a private logger with a UTF-8 file handler as the app logger."""
    logger = logging.getLogger(f"test_debughelpers.app.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = RecordingFileHandler(tmp_path / "app.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    monkeypatch.setattr(log, "LOGGER", logger)
    yield handler
    handler.close()
    logger.removeHandler(handler)


@pytest.fixture
def dialogs(monkeypatch):
    """Record show_error_dialog() calls instead of showing a dialog."""
    calls = []

    def record(error, msg, parent=None):
        calls.append(types.SimpleNamespace(error=error, msg=msg, parent=parent))

    monkeypatch.setattr(debughelpers, "show_error_dialog", record)
    return calls


def _log_text(handler: RecordingFileHandler) -> str:
    return "\n".join(record.getMessage() for record in handler.records)


def _assert_no_nonetype(*texts: str) -> None:
    for text in texts:
        assert "NoneType: None" not in text


def _monitoring_thread_body() -> None:
    raise KeyError("missing-key-in-display-thread")


def _exception_caught_in_thread() -> BaseException:
    """Mimic the profile loader's display monitoring thread fatal error path.

    The exception is caught in a thread and handed to the GUI thread, where
    handle_error(exception) runs outside any except block.
    """
    caught = []

    def target() -> None:
        try:
            _monitoring_thread_body()
        except Exception as exception:
            caught.append(exception)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    return caught[0]


def test_handle_error_outside_except_logs_given_traceback(app_log, dialogs, capsys):
    exception = _exception_caught_in_thread()
    assert sys.exc_info()[1] is None

    handle_error(exception)

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert len(app_log.records) == 1
    assert app_log.records[0].levelno == logging.ERROR
    assert "Traceback (most recent call last)" in text
    assert "_monitoring_thread_body" in text
    assert "KeyError: 'missing-key-in-display-thread'" in text
    # The dialog shows the real traceback instead of "NoneType: None"
    assert len(dialogs) == 1
    assert dialogs[0].error is exception
    assert "_monitoring_thread_body" in dialogs[0].msg
    # Console runs still get the traceback on stderr
    assert "_monitoring_thread_body" in err
    _assert_no_nonetype(text, out, err, dialogs[0].msg)


def test_handle_error_from_excepthook_tuple(app_log, dialogs, capsys):
    exception = _exception_caught_in_thread()

    handle_error((type(exception), exception, exception.__traceback__))

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert "_monitoring_thread_body" in text
    assert "KeyError: 'missing-key-in-display-thread'" in text
    assert "_monitoring_thread_body" in dialogs[0].msg
    _assert_no_nonetype(text, out, err, dialogs[0].msg)


def test_handle_error_from_wndproc_style_excepthook_tuple(app_log, dialogs, capsys):
    """pywin32 passes an error raised in C, i.e. without a traceback."""
    message = "WPARAM is simple, so must be an int object (got NoneType)"
    exception = TypeError(message)

    handle_error((TypeError, exception, None), silent=True)

    out, err = capsys.readouterr()
    assert _log_text(app_log) == f"TypeError: {message}"
    assert not dialogs
    _assert_no_nonetype(out, err)


def _excepthook(etype, value, tb) -> None:
    """Like DisplayCAL.main._excepthook."""
    handle_error((etype, value, tb))


def test_handle_error_in_sys_excepthook_called_by_pyerr_print(
    app_log, dialogs, capsys
):
    """C code reports errors via PyErr_Print() -> sys.excepthook.

    Inside the hook, no exception is being handled (sys.exc_info() is empty).
    That is how pywin32 WNDPROC and wx event handler errors arrive.
    """
    saved_hook = sys.excepthook
    sys.excepthook = _excepthook
    try:
        ctypes.pythonapi.PyRun_SimpleString(
            b"def _wx_event_handler():\n"
            b"    raise RuntimeError('boom from PyErr_Print')\n"
            b"_wx_event_handler()\n"
        )
    finally:
        sys.excepthook = saved_hook
        for name in ("last_type", "last_value", "last_traceback"):
            with contextlib.suppress(AttributeError):
                delattr(sys, name)

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert "_wx_event_handler" in text
    assert "RuntimeError: boom from PyErr_Print" in text
    assert "_wx_event_handler" in err
    assert len(dialogs) == 1
    _assert_no_nonetype(text, out, err, dialogs[0].msg)


def _decode_foreign_reply() -> str:
    return b"HTTP/1.1 400 Bad Request\r\n\x92".decode("utf-8")


def test_handle_error_in_except_block_with_wrapper_error(app_log, dialogs, capsys):
    """main.main() pattern: a wrapper error created inside an except block.

    Both the wrapper's message and the original exception's frames must be
    recorded.
    """
    try:
        _decode_foreign_reply()
    except Exception as exception:
        handle_error(Error(f"Fatal error: {exception}"))

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert text.startswith("Error: Fatal error: 'utf-8' codec can't decode byte 0x92")
    assert "_decode_foreign_reply" in text
    assert "UnicodeDecodeError" in text
    assert "_decode_foreign_reply" in dialogs[0].msg
    assert dialogs[0].msg.startswith("Fatal error: ")
    assert "_decode_foreign_reply" in err
    _assert_no_nonetype(text, out, err, dialogs[0].msg)


def test_handle_error_string_in_except_block_keeps_frames(app_log, dialogs, capsys):
    try:
        _decode_foreign_reply()
    except Exception:
        handle_error("Could not process the reply", silent=True)

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert text.startswith("Could not process the reply\n")
    assert "_decode_foreign_reply" in text
    assert "_decode_foreign_reply" in err
    assert not dialogs


def test_handle_error_unraised_warning_outside_except(app_log, dialogs, capsys):
    """An error that was never raised has no traceback to show or log."""
    handle_error(UserWarning("Warning: No language files found."))

    out, err = capsys.readouterr()
    assert _log_text(app_log) == "UserWarning: Warning: No language files found."
    assert dialogs[0].msg == "Warning: No language files found."
    assert err == ""
    _assert_no_nonetype(out)


def test_handle_error_with_stdio_none(app_log, dialogs):
    """Under pythonw sys.stdout/sys.stderr may be None: no crash, dialog shown."""
    exception = _exception_caught_in_thread()
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = None
    try:
        handle_error(exception)
    finally:
        sys.stdout, sys.stderr = saved

    assert "_monitoring_thread_body" in _log_text(app_log)
    assert len(dialogs) == 1


class _LogStream:
    """Stand-in for log.StdioToLog which records what is written to it."""

    is_log_stream = True

    def __init__(self) -> None:
        self.written = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass


def test_handle_error_writes_once_when_stdio_is_the_log(app_log, dialogs):
    """Frozen builds: stdio already goes to the log, so no duplicate records."""
    exception = _exception_caught_in_thread()
    stdout, stderr = _LogStream(), _LogStream()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        handle_error(exception)
    finally:
        sys.stdout, sys.stderr = saved

    assert stdout.written == []
    assert stderr.written == []
    assert len(app_log.records) == 1
    assert "_monitoring_thread_body" in _log_text(app_log)


def test_handle_error_without_app_log_writes_to_stderr_once(
    monkeypatch, dialogs, capsys
):
    """Before setup_logging(), the record goes to sys.stderr instead."""
    monkeypatch.setattr(log, "LOGGER", None)
    exception = _exception_caught_in_thread()

    handle_error(exception)

    out, err = capsys.readouterr()
    assert err.count("Traceback (most recent call last)") == 1
    assert "KeyError: 'missing-key-in-display-thread'" in err
    _assert_no_nonetype(out, err)


def test_handle_error_logs_dialog_failure(app_log, monkeypatch, capsys):
    def broken_dialog(error, msg, parent=None):
        raise RuntimeError("no display available")

    monkeypatch.setattr(debughelpers, "show_error_dialog", broken_dialog)

    handle_error(UserWarning("something happened"))

    out, err = capsys.readouterr()
    text = _log_text(app_log)
    assert "UserWarning: something happened" in text
    assert "could not show the error dialog" in text
    assert "broken_dialog" in text
    assert "RuntimeError: no display available" in text
    _assert_no_nonetype(text, out, err)


def test_format_error_traceback_without_traceback_is_empty():
    assert sys.exc_info()[1] is None
    assert format_error_traceback(UserWarning("never raised")) == ""
    assert format_error_traceback("just a message") == ""
    assert format_error_traceback((None, None, None)) == ""


def test_format_error_traceback_uses_the_exceptions_own_traceback():
    exception = _exception_caught_in_thread()
    try:
        raise ValueError("unrelated exception being handled")
    except ValueError:
        text = format_error_traceback(exception)
    assert "_monitoring_thread_body" in text
    assert "unrelated exception being handled" not in text


def _raise_and_catch(message: str) -> Exception:
    try:
        raise ValueError(message)
    except ValueError as exception:
        return exception


def test_print_error_traceback_unraised_exception_outside_except(capsys):
    """An exception that was never raised has no traceback to print."""
    print_error_traceback(Error("returned, not raised"))
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def test_print_error_traceback_uses_the_exceptions_own_traceback(capsys):
    """A caught-and-returned exception prints its own traceback, not None."""
    exception = _raise_and_catch("from a returned exception")
    print_error_traceback(exception)
    out, err = capsys.readouterr()
    assert out == ""
    assert "NoneType: None" not in err
    assert "_raise_and_catch" in err
    assert "ValueError: from a returned exception" in err


def test_print_error_traceback_with_stderr_none(monkeypatch):
    """Windowed builds without a stderr must not make it raise."""
    monkeypatch.setattr(sys, "stderr", None)
    print_error_traceback(_raise_and_catch("no stderr"))


def test_worker_base_log_does_not_print_nonetype_none(capsys):
    """WorkerBase.log() with a returned exception logs no "NoneType: None"."""
    from DisplayCAL.worker_base import WorkerBase

    worker = WorkerBase()
    worker.log(Error("argyll tool failed"))
    worker.log(_raise_and_catch("raised elsewhere"))
    out, err = capsys.readouterr()
    assert "NoneType: None" not in err
    assert "argyll tool failed" in out
    assert "ValueError: raised elsewhere" in err


def test_log_error_traceback_in_except_block(app_log, capsys):
    """A recovered error is recorded in the app log, and shown on a console."""
    try:
        raise KeyError("recovered")
    except KeyError:
        log_error_traceback()
    out, err = capsys.readouterr()
    assert out == ""
    assert "KeyError: 'recovered'" in err
    logged = "\n".join(r.getMessage() for r in app_log.records)
    assert "KeyError: 'recovered'" in logged
    assert "test_log_error_traceback_in_except_block" in logged


def test_log_error_traceback_outside_except_records_nothing(app_log, capsys):
    """Without a current exception there is nothing to record."""
    log_error_traceback()
    out, err = capsys.readouterr()
    assert (out, err) == ("", "")
    assert app_log.records == []


def test_log_error_traceback_does_not_write_to_the_py2exe_shim(app_log, monkeypatch):
    """With stderr redirected to the log, the traceback is logged only once."""

    class LogStream:
        is_log_stream = True

        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)

    stream = LogStream()
    monkeypatch.setattr(sys, "stderr", stream)
    log_error_traceback(_raise_and_catch("recovered in the loader"))
    assert stream.written == []
    assert len(app_log.records) == 1
    assert "ValueError: recovered in the loader" in app_log.records[0].getMessage()


def test_importing_debughelpers_does_not_import_log():
    """Library modules importing debughelpers must not pull in DisplayCAL.log.

    DisplayCAL.log replaces warnings.showwarning and sets
    logging.raiseExceptions = 0 when imported.
    """
    import subprocess

    code = (
        "import sys; import DisplayCAL.debughelpers; "
        "print('DisplayCAL.log' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.stdout.strip() == "False"
