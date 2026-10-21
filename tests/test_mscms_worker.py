"""Tests for the WCS worker process manager's robustness and diagnostics.

The worker processes spawned here run test doubles of the real worker (see
tests/test_mscms.py for why they live at module level). No real Windows Color
System call is made and no profile association is touched.
"""

import io
import logging
import queue
from logging.handlers import QueueListener

import DisplayCAL.mscms as mscms_module


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _Console(io.StringIO):
    def isatty(self):
        return True


# --- Logging -----------------------------------------------------------------


def test_manager_logger_propagates_when_stderr_is_not_a_console():
    """Frozen builds: no NullHandler, records reach the app's log file."""
    test_logger = logging.getLogger("DisplayCAL.mscms.test_manager_no_console")
    mscms_module._configure_logger(test_logger, io.StringIO())

    assert test_logger.handlers == []
    assert test_logger.propagate is True


def test_manager_logger_uses_console_when_stderr_is_a_console():
    console = _Console()
    test_logger = logging.getLogger("DisplayCAL.mscms.test_manager_console")
    try:
        mscms_module._configure_logger(test_logger, console)
        test_logger.warning("hello console")
    finally:
        for handler in test_logger.handlers[:]:
            test_logger.removeHandler(handler)

    assert test_logger.propagate is False
    assert "hello console" in console.getvalue()


def test_worker_records_are_dispatched_to_the_logger_of_the_same_name():
    name = "DisplayCAL.mscms.test_dispatch_worker"
    target = _ListHandler()
    logging.getLogger(name).addHandler(target)
    log_queue = queue.Queue()
    listener = QueueListener(log_queue, mscms_module._DispatchToLogger())
    try:
        listener.start()
        log_queue.put(
            logging.LogRecord(
                name, logging.INFO, __file__, 1, "WCSWorker process started", (), None
            )
        )
    finally:
        listener.stop()
        logging.getLogger(name).removeHandler(target)

    assert [record.getMessage() for record in target.records] == [
        "WCSWorker process started"
    ]


def test_manager_forwards_worker_logs_to_local_loggers(monkeypatch):
    monkeypatch.setattr(mscms_module.logger, "handlers", [])
    original_worker = mscms_module._wcs_worker_process
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _echo_worker)
    manager = None
    try:
        manager = mscms_module.WCSManager()
        handlers = manager._log_listener.handlers
    finally:
        if manager:
            manager.shutdown()
        mscms_module._wcs_worker_process = original_worker

    assert len(handlers) == 1
    assert isinstance(handlers[0], mscms_module._DispatchToLogger)


# --- Test doubles of the worker process (must be importable by the child) ----


def _echo_worker(request_queue, response_queue, log_queue):
    """Answer every request with its method name."""
    while True:
        try:
            request = request_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if request is None:
            break
        response_queue.put(
            {"type": "resp_success", "id": request["id"], "result": request["method"]}
        )
