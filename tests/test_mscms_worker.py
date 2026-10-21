"""Tests for the WCS worker process manager's robustness and diagnostics.

The worker processes spawned here run test doubles of the real worker (see
tests/test_mscms.py for why they live at module level). No real Windows Color
System call is made and no profile association is touched.
"""

import io
import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import QueueListener

import pytest

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


def test_format_exitcode():
    assert mscms_module._format_exitcode(3221225477) == "3221225477 (0xC0000005)"
    assert mscms_module._format_exitcode(0) == "0"
    assert mscms_module._format_exitcode(1) == "1"
    assert mscms_module._format_exitcode(-15) == "-15"
    assert mscms_module._format_exitcode(None) == "None"


# --- Dead / restarting worker process ---------------------------------------


@pytest.fixture
def echo_manager(monkeypatch):
    """A WCSManager whose worker answers every request with the method name."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _echo_worker)
    manager = mscms_module.WCSManager(request_timeout=0.5)
    try:
        yield manager
    finally:
        manager.shutdown()


def test_dead_worker_is_restarted_and_logged(echo_manager, caplog):
    manager = echo_manager
    assert manager.get_calibration_management_state() == (
        "GetCalibrationManagementState"
    )
    dead_worker = manager._worker_process
    # Simulate a crash of our own worker process (by its handle, not by name)
    dead_worker.terminate()
    dead_worker.join(5)
    assert not dead_worker.is_alive()

    caplog.set_level(logging.ERROR, logger="DisplayCAL.mscms.manager")
    started = time.monotonic()
    result = manager.get_calibration_management_state()
    elapsed = time.monotonic() - started

    assert result == "GetCalibrationManagementState"
    assert manager._worker_process is not dead_worker
    assert manager._worker_process.is_alive()
    # Restarted right away instead of timing out (about a minute by default)
    assert elapsed < 5
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        f"PID {dead_worker.pid} is not running" in message
        and f"exit code {mscms_module._format_exitcode(dead_worker.exitcode)}"
        in message
        for message in messages
    ), messages


def test_call_during_worker_restart_is_retried(echo_manager, monkeypatch):
    manager = echo_manager
    original_start_worker = manager._start_worker

    def slow_start_worker():
        # Widen the window in which the old worker is stopped and the new
        # one not started yet
        time.sleep(0.3)
        original_start_worker()

    monkeypatch.setattr(manager, "_start_worker", slow_start_worker)
    restart = threading.Thread(target=manager._restart_worker, args=("test",))
    restart.start()
    deadline = time.monotonic() + 10
    while not manager._shutdown_event.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager._restarting.is_set()

    result = manager.get_calibration_management_state()
    restart.join(10)

    assert result == "GetCalibrationManagementState"


def _count_worker_starts(manager, monkeypatch):
    starts = []
    original_start_worker = manager._start_worker

    def counting_start_worker():
        starts.append(None)
        original_start_worker()

    monkeypatch.setattr(manager, "_start_worker", counting_start_worker)
    return starts


def test_worker_that_keeps_dying_is_not_respawned_in_a_loop(monkeypatch):
    """E.g. a frozen child blocked by security software: it dies at once.

    Each call used to respawn the worker up to six times and wait the full
    request timeout for every attempt, forever.
    """
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _exiting_worker)
    monkeypatch.setattr(mscms_module, "worker_restart_cooldown_initial", 1.0)
    manager = mscms_module.WCSManager(request_timeout=10)
    try:
        starts = _count_worker_starts(manager, monkeypatch)
        manager._worker_process.join(10)

        started = time.monotonic()
        for _ in range(5):
            with pytest.raises(mscms_module.WCSWorkerError) as exc_info:
                manager.get_calibration_management_state()
        elapsed = time.monotonic() - started

        # The first dead worker is replaced at once. Its replacement died
        # right away too, so the next restart waits for the cooldown and
        # calls fail at once meanwhile (no waiting for the request timeout).
        assert len(starts) == 1
        assert elapsed < 8
        assert "restart deferred" in str(exc_info.value)
        assert "exit code 3" in str(exc_info.value)
        assert manager._restart_cooldown == 1.0

        # After the cooldown: one more restart, then the cooldown doubles
        time.sleep(1.1)
        with pytest.raises(mscms_module.WCSWorkerError):
            manager.get_calibration_management_state()
        assert len(starts) == 2
        assert manager._restart_cooldown == 2.0
    finally:
        manager.shutdown()


def test_worker_dying_during_a_request_is_noticed_at_once(monkeypatch, caplog):
    """E.g. a driver crash in one WCS call: don't wait for the request timeout."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _crash_on_request_worker)
    manager = mscms_module.WCSManager(request_timeout=60)
    try:
        starts = _count_worker_starts(manager, monkeypatch)
        caplog.set_level(logging.ERROR, logger="DisplayCAL.mscms.manager")

        started = time.monotonic()
        with pytest.raises(mscms_module.WCSWorkerError):
            manager.get_calibration_management_state()
        elapsed = time.monotonic() - started

        # Replaced once (and the call retried), then the restart is deferred
        assert len(starts) == 1
        assert elapsed < 20
        messages = [record.getMessage() for record in caplog.records]
        assert any("exit code 7" in message for message in messages), messages
    finally:
        manager.shutdown()


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _DeadWorker:
    pid = 1234
    exitcode = 3221225477

    def is_alive(self):
        return False


def test_restart_cooldown_doubles_is_capped_and_resets(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(mscms_module, "monotonic", clock)
    manager = mscms_module.WCSManager.__new__(mscms_module.WCSManager)
    manager._worker_started_at = clock.now
    manager._worker_restarted_after_death = False
    manager._restart_cooldown = 0.0
    manager._restart_not_before = 0.0
    manager._dead_worker_seen = None

    def worker_dies_after(seconds):
        """Return a dead worker that ran for ``seconds``, then 'restart' it."""
        clock.now += seconds
        return _DeadWorker()

    def restart():
        manager._worker_started_at = clock.now
        manager._worker_restarted_after_death = True

    # The original worker dies: restarted at once
    manager._check_restart_cooldown(worker_dies_after(5), "dead")
    assert manager._restart_cooldown == 0
    restart()

    cooldowns = []
    for _ in range(8):
        worker = worker_dies_after(2)
        with pytest.raises(mscms_module.WCSWorkerError) as exc_info:
            manager._check_restart_cooldown(worker, "dead")
        cooldowns.append(manager._restart_cooldown)
        # Still deferred just before the cooldown ends, with the same error
        clock.now += manager._restart_cooldown - 0.5
        with pytest.raises(mscms_module.WCSWorkerError) as exc_info2:
            manager._check_restart_cooldown(worker, "dead")
        assert str(exc_info2.value) == str(exc_info.value)
        clock.now += 0.5
        manager._check_restart_cooldown(worker, "dead")
        restart()

    assert cooldowns == [5, 10, 20, 40, 80, 160, 300, 300]

    # A worker that lived longer resets the cooldown: restarted at once
    manager._check_restart_cooldown(worker_dies_after(3600), "dead")
    assert manager._restart_cooldown == 0


def test_call_after_shutdown_is_not_retried(echo_manager):
    manager = echo_manager
    manager.shutdown()

    started = time.monotonic()
    with pytest.raises(mscms_module.WCSManagerShutdownError) as exc_info:
        manager.get_calibration_management_state()

    assert not isinstance(exc_info.value, mscms_module.WCSManagerRestartingError)
    assert time.monotonic() - started < 1


# --- Worker process side ------------------------------------------------------


class _FakeQueue:
    def __init__(self):
        self.items = []
        self.cancel_join_thread_called = False

    def get(self, timeout=None):
        time.sleep(0.01)
        raise queue.Empty

    def put(self, item):
        self.items.append(item)

    put_nowait = put

    def cancel_join_thread(self):
        self.cancel_join_thread_called = True


class _DeadParent:
    def is_alive(self):
        return False


class _FakeWCS:
    pass


def test_worker_exits_when_parent_process_is_gone(monkeypatch):
    worker_logger = logging.getLogger("DisplayCAL.mscms.wcsworker")
    monkeypatch.setattr(worker_logger, "propagate", worker_logger.propagate)
    monkeypatch.setattr(mscms_module, "WCS", _FakeWCS)
    monkeypatch.setattr(
        mscms_module.multiprocessing, "parent_process", lambda: _DeadParent()
    )
    request_queue, response_queue, log_queue = _FakeQueue(), _FakeQueue(), _FakeQueue()

    worker = threading.Thread(
        target=mscms_module._wcs_worker_process,
        args=(request_queue, response_queue, log_queue),
        daemon=True,
    )
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    # Must not hang in Queue._finalize_join on queues nobody reads anymore
    assert response_queue.cancel_join_thread_called
    assert log_queue.cancel_join_thread_called
    messages = [record.getMessage() for record in log_queue.items]
    assert "WCSWorker process shutting down" not in messages


# --- Per-user profiles flag cache (util_win) ----------------------------------


class _FakeWCSManager:
    """Stands in for util_win's WCSManagerProxy (no worker process)."""

    def __init__(self):
        self.answer = True
        self.get_calls = []
        self.set_calls = []

    def get_use_per_user_profiles(self, devicekey):
        self.get_calls.append(devicekey)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def set_use_per_user_profiles(self, devicekey, enable):
        self.set_calls.append((devicekey, enable))


@pytest.fixture
def util_win_cache(monkeypatch):
    util_win = pytest.importorskip("DisplayCAL.util_win")
    fake = _FakeWCSManager()
    monkeypatch.setattr(util_win, "mscms", fake)
    util_win.invalidate_per_user_profiles_cache()
    yield util_win, fake
    util_win.invalidate_per_user_profiles_cache()


def test_per_user_profiles_flag_is_cached_per_device(util_win_cache):
    util_win, fake = util_win_cache

    for _ in range(20):
        for devicekey in ("key0", "key1"):
            assert util_win.per_user_profiles_isenabled(devicekey=devicekey) is True

    assert sorted(fake.get_calls) == ["key0", "key1"]


def test_per_user_profiles_cache_expires(util_win_cache, monkeypatch):
    util_win, fake = util_win_cache
    monkeypatch.setattr(util_win, "PER_USER_PROFILES_CACHE_TTL", 0)

    for _ in range(3):
        util_win.per_user_profiles_isenabled(devicekey="key0")

    assert fake.get_calls == ["key0"] * 3


def test_per_user_profiles_cache_invalidation(util_win_cache):
    util_win, fake = util_win_cache
    util_win.per_user_profiles_isenabled(devicekey="key0")
    util_win.per_user_profiles_isenabled(devicekey="key1")
    util_win.invalidate_per_user_profiles_cache("key0")
    util_win.per_user_profiles_isenabled(devicekey="key0")
    util_win.per_user_profiles_isenabled(devicekey="key1")
    util_win.invalidate_per_user_profiles_cache()
    util_win.per_user_profiles_isenabled(devicekey="key1")

    assert fake.get_calls == ["key0", "key1", "key0", "key1"]


def test_enable_per_user_profiles_updates_cache(util_win_cache):
    util_win, fake = util_win_cache
    assert util_win.per_user_profiles_isenabled(devicekey="key0") is True

    util_win.enable_per_user_profiles(False, devicekey="key0")

    assert fake.set_calls == [("key0", False)]
    assert util_win.per_user_profiles_isenabled(devicekey="key0") is False
    assert fake.get_calls == ["key0"]


def test_per_user_profiles_errors_are_not_cached(util_win_cache):
    util_win, fake = util_win_cache
    fake.answer = TimeoutError("timed out")

    for _ in range(2):
        with pytest.raises(TimeoutError):
            util_win.per_user_profiles_isenabled(devicekey="key0")

    assert fake.get_calls == ["key0", "key0"]


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


def _exiting_worker(request_queue, response_queue, log_queue):
    """Exit at once, like a worker process that cannot start."""
    sys.exit(3)


def _crash_on_request_worker(request_queue, response_queue, log_queue):
    """Die without answering the first request, like a crash in a WCS call."""
    while True:
        try:
            request = request_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if request is None:
            break
        os._exit(7)
