"""Tests for the WCS worker process manager's robustness and diagnostics.

The worker processes spawned here run test doubles of the real worker (see
tests/test_mscms.py for why they live at module level). No real Windows Color
System call is made and no profile association is touched.
"""

import io
import logging
import os
import queue
import signal
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


def _crash(process):
    """Kill our own worker process (by its PID, not by name) like a crash.

    Unlike Process.terminate(), which main._exit() uses at application exit.
    """
    os.kill(process.pid, getattr(signal, "SIGKILL", 9))
    process.join(5)


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
    _crash(dead_worker)
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
    manager.request_timeout = 10.0
    manager._hung_worker = None
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


def test_worker_hanging_right_after_its_restart_gets_a_cooldown(monkeypatch):
    """Even with a request timeout longer than the restart window."""
    clock = _FakeClock()
    monkeypatch.setattr(mscms_module, "monotonic", clock)
    manager = mscms_module.WCSManager.__new__(mscms_module.WCSManager)
    manager.request_timeout = 2 * mscms_module.worker_restart_window
    manager._worker_started_at = clock.now
    manager._worker_restarted_after_death = True
    manager._restart_cooldown = 0.0
    manager._restart_not_before = 0.0
    manager._dead_worker_seen = None
    # It hung on a request sent 1 s after its start, and was killed when the
    # request timed out
    clock.now += 1 + manager.request_timeout
    manager._hung_worker = _DeadWorker()

    with pytest.raises(mscms_module.WCSWorkerError):
        manager._check_restart_cooldown(manager._hung_worker, "hung")

    assert manager._restart_cooldown == mscms_module.worker_restart_cooldown_initial


def test_call_after_shutdown_is_not_retried(echo_manager):
    manager = echo_manager
    manager.shutdown()

    started = time.monotonic()
    with pytest.raises(mscms_module.WCSManagerShutdownError) as exc_info:
        manager.get_calibration_management_state()

    assert not isinstance(exc_info.value, mscms_module.WCSManagerRestartingError)
    assert time.monotonic() - started < 1


# --- Hung worker process -------------------------------------------------------

# Request timeout for the hung worker tests. With the retries, a hung worker
# used to cost (5 + 1) * HUNG_TIMEOUT + 3.1 s of backoff per call, forever.
HUNG_TIMEOUT = 1.5


def test_hung_worker_is_killed_and_replaced(monkeypatch, tmp_path, caplog):
    """E.g. a WCS call that never returns: the worker reads no more requests."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _hang_once_worker)
    manager = mscms_module.WCSManager(request_timeout=HUNG_TIMEOUT)
    try:
        starts = _count_worker_starts(manager, monkeypatch)
        # Up and running (and it told us so)
        assert manager.get_calibration_management_state() == (
            "GetCalibrationManagementState"
        )
        hung_worker = manager._worker_process
        caplog.set_level(logging.ERROR, logger="DisplayCAL.mscms.manager")

        started = time.monotonic()
        result = manager._call_wcs_method("HangOnce", str(tmp_path / "hung"))
        elapsed = time.monotonic() - started

        # Answered by a new worker process after one request timeout
        assert result == "HangOnce"
        assert len(starts) == 1
        assert not hung_worker.is_alive()
        assert manager._worker_process is not hung_worker
        assert manager._worker_process.is_alive()
        assert elapsed < 2 * HUNG_TIMEOUT + 2
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            f"PID {hung_worker.pid} did not answer request" in message
            for message in messages
        ), messages
    finally:
        manager.shutdown()


def test_worker_that_keeps_hanging_is_not_waited_for(monkeypatch):
    """The replacement hangs too: calls fail at once during the cooldown."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _hanging_worker)
    monkeypatch.setattr(mscms_module, "worker_restart_cooldown_initial", 2.0)
    manager = mscms_module.WCSManager(request_timeout=HUNG_TIMEOUT)
    try:
        starts = _count_worker_starts(manager, monkeypatch)
        assert manager.get_calibration_management_state() == (
            "GetCalibrationManagementState"
        )

        started = time.monotonic()
        with pytest.raises(mscms_module.WCSWorkerError) as exc_info:
            manager._call_wcs_method("Hang")
        elapsed = time.monotonic() - started

        # The hung worker was replaced once. Its replacement hung right away
        # too, so it is not restarted until the cooldown ends.
        assert "restart deferred" in str(exc_info.value)
        assert len(starts) == 1
        assert not manager._worker_process.is_alive()
        assert elapsed < 4 * HUNG_TIMEOUT + 2

        started = time.monotonic()
        with pytest.raises(mscms_module.WCSWorkerError):
            manager.get_calibration_management_state()
        assert time.monotonic() - started < 0.5

        # After the cooldown, a new worker process answers again
        time.sleep(2.1)
        assert manager.get_calibration_management_state() == (
            "GetCalibrationManagementState"
        )
        assert len(starts) == 2
    finally:
        started = time.monotonic()
        manager.shutdown()
    assert time.monotonic() - started < 5


def test_slow_starting_worker_is_not_mistaken_for_a_hung_one(monkeypatch):
    """E.g. at logon: a worker process that is not up yet is not killed."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _slow_starting_worker)
    manager = mscms_module.WCSManager(request_timeout=0.5)
    try:
        starts = _count_worker_starts(manager, monkeypatch)
        worker = manager._worker_process

        result = manager.get_calibration_management_state()

        assert result == "GetCalibrationManagementState"
        assert starts == []
        assert manager._worker_process is worker
    finally:
        manager.shutdown()


# --- Shutdown --------------------------------------------------------------------


def _start_pending_call(manager, method):
    """Call ``method`` in a thread, and wait until the worker has read it.

    Returns:
        tuple: The thread, and a dict that receives the call's "error" and
            the time it "finished".
    """
    outcome = {}

    def call():
        try:
            outcome["result"] = manager._call_wcs_method(method)
        except Exception as exception:
            outcome["error"] = exception
        outcome["finished"] = time.monotonic()

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not manager._pending_requests and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager._pending_requests
    time.sleep(0.3)
    return thread, outcome


@pytest.fixture
def hanging_manager(monkeypatch):
    """A WCSManager whose (running) worker hangs on every "Hang" request."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _hanging_worker)
    manager = mscms_module.WCSManager(request_timeout=30)
    try:
        assert manager.get_calibration_management_state() == (
            "GetCalibrationManagementState"
        )
        yield manager
    finally:
        manager.shutdown()


def test_worker_terminated_at_exit_is_not_restarted(echo_manager, monkeypatch, caplog):
    """main._exit() terminates all child processes at application exit.

    The next call used to start a new worker process (which nothing
    terminated anymore) and log the termination as a crash.
    """
    manager = echo_manager
    assert manager.get_calibration_management_state() == (
        "GetCalibrationManagementState"
    )
    starts = _count_worker_starts(manager, monkeypatch)
    caplog.set_level(logging.INFO, logger="DisplayCAL.mscms.manager")
    worker = manager._worker_process
    # What main._exit() does
    worker.terminate()
    worker.join(5)

    started = time.monotonic()
    for _ in range(2):
        with pytest.raises(mscms_module.WCSManagerShutdownError):
            manager.get_calibration_management_state()
    elapsed = time.monotonic() - started

    assert starts == []
    assert manager._worker_process is worker
    assert elapsed < 3
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    assert errors == []


def test_pending_call_ends_when_worker_is_terminated_at_exit(
    hanging_manager, monkeypatch
):
    """E.g. the loader's monitoring thread, still in a call at exit."""
    manager = hanging_manager
    starts = _count_worker_starts(manager, monkeypatch)
    thread, outcome = _start_pending_call(manager, "Hang")

    terminated = time.monotonic()
    # What main._exit() does
    manager._worker_process.terminate()
    thread.join(5)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), mscms_module.WCSManagerShutdownError)
    assert outcome["finished"] - terminated < 3
    assert starts == []


def test_pending_call_ends_at_shutdown(hanging_manager, monkeypatch):
    """Not only once the hung worker was stopped (it ignores the poison pill)."""
    manager = hanging_manager
    starts = _count_worker_starts(manager, monkeypatch)
    thread, outcome = _start_pending_call(manager, "Hang")

    shutdown_started = time.monotonic()
    shutdown = threading.Thread(target=manager.shutdown, daemon=True)
    shutdown.start()
    thread.join(5)
    shutdown.join(15)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), mscms_module.WCSManagerShutdownError)
    assert outcome["finished"] - shutdown_started < 2
    assert not shutdown.is_alive()
    assert not manager._worker_process.is_alive()
    assert starts == []


def test_no_worker_is_started_once_shutdown_is_called(echo_manager, monkeypatch):
    """Not even by a worker restart that was in progress."""
    manager = echo_manager
    starts = _count_worker_starts(manager, monkeypatch)
    original_stop_worker = manager._stop_worker
    stopping = threading.Event()

    def slow_stop_worker():
        stopping.set()
        time.sleep(0.5)
        original_stop_worker()

    monkeypatch.setattr(manager, "_stop_worker", slow_stop_worker)
    restart = threading.Thread(target=manager._restart_worker, args=("test",))
    restart.start()
    assert stopping.wait(10)

    manager.shutdown()
    restart.join(10)

    assert starts == []
    assert not manager._worker_process.is_alive()
    with pytest.raises(mscms_module.WCSManagerShutdownError):
        manager._ensure_worker_alive()
    assert starts == []


def test_shutdown_leaves_no_threads_running(monkeypatch):
    """Each manager used to leave its worker log listener thread running."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _echo_worker)
    threads_before = set(threading.enumerate())
    manager = mscms_module.WCSManager(request_timeout=5)
    try:
        assert manager.get_calibration_management_state() == (
            "GetCalibrationManagementState"
        )
    finally:
        manager.shutdown()

    deadline = time.monotonic() + 5
    while True:
        leftover = [
            thread.name
            for thread in threading.enumerate()
            if thread not in threads_before and thread.is_alive()
        ]
        if not leftover or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert leftover == []


class _StuckListener:
    """A worker log listener whose thread does not end (e.g. stuck reading)."""

    def __init__(self):
        self.stop_calls = 0
        self.release = threading.Event()

    def stop(self):
        self.stop_calls += 1
        self.release.wait()


def test_shutdown_does_not_wait_for_a_stuck_log_listener(monkeypatch, caplog):
    """E.g. a worker process that was killed while writing a log record."""
    monkeypatch.setattr(mscms_module, "_wcs_worker_process", _echo_worker)
    monkeypatch.setattr(mscms_module, "log_listener_stop_timeout", 0.2)
    manager = mscms_module.WCSManager(request_timeout=5)
    real_listener = manager._log_listener
    stuck_listener = _StuckListener()
    manager._log_listener = stuck_listener
    caplog.set_level(logging.WARNING, logger="DisplayCAL.mscms.manager")
    try:
        started = time.monotonic()
        manager.shutdown()
        elapsed = time.monotonic() - started
        manager.shutdown()
    finally:
        stuck_listener.release.set()
        real_listener.stop()
        manager._log_queue.close()

    assert elapsed < 3
    # Idempotent: not stopped again
    assert stuck_listener.stop_calls == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("log listener did not stop" in message for message in messages)


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

    with util_win.cached_per_user_profiles():
        for _ in range(20):
            for devicekey in ("key0", "key1"):
                assert util_win.per_user_profiles_isenabled(devicekey=devicekey)

    assert sorted(fake.get_calls) == ["key0", "key1"]


def test_per_user_profiles_flag_is_queried_outside_a_cache_block(util_win_cache):
    """Callers that did not opt in (e.g. set_display_profile) read it live."""
    util_win, fake = util_win_cache

    for _ in range(3):
        assert util_win.per_user_profiles_isenabled(devicekey="key0") is True
    assert fake.get_calls == ["key0"] * 3

    # A live read also refreshes the cache for pollers
    with util_win.cached_per_user_profiles():
        assert util_win.per_user_profiles_isenabled(devicekey="key0") is True
    assert fake.get_calls == ["key0"] * 3

    # The cache block has ended
    util_win.per_user_profiles_isenabled(devicekey="key0")
    assert fake.get_calls == ["key0"] * 4


def test_per_user_profiles_cache_block_is_per_thread(util_win_cache):
    util_win, fake = util_win_cache

    def read_in_other_thread():
        thread = threading.Thread(
            target=util_win.per_user_profiles_isenabled,
            kwargs={"devicekey": "key0"},
        )
        thread.start()
        thread.join(5)

    with util_win.cached_per_user_profiles():
        util_win.per_user_profiles_isenabled(devicekey="key0")
        read_in_other_thread()
        read_in_other_thread()
        util_win.per_user_profiles_isenabled(devicekey="key0")

    assert fake.get_calls == ["key0"] * 3


def test_per_user_profiles_cache_expires(util_win_cache, monkeypatch):
    util_win, fake = util_win_cache
    monkeypatch.setattr(util_win, "PER_USER_PROFILES_CACHE_TTL", 0)

    with util_win.cached_per_user_profiles():
        for _ in range(3):
            util_win.per_user_profiles_isenabled(devicekey="key0")

    assert fake.get_calls == ["key0"] * 3


def test_per_user_profiles_cache_invalidation(util_win_cache):
    util_win, fake = util_win_cache
    with util_win.cached_per_user_profiles():
        util_win.per_user_profiles_isenabled(devicekey="key0")
        util_win.per_user_profiles_isenabled(devicekey="key1")
        util_win.invalidate_per_user_profiles_cache("key0")
        util_win.per_user_profiles_isenabled(devicekey="key0")
        util_win.per_user_profiles_isenabled(devicekey="key1")
        util_win.invalidate_per_user_profiles_cache()
        util_win.per_user_profiles_isenabled(devicekey="key1")

    assert fake.get_calls == ["key0", "key1", "key0", "key1"]


def test_invalidation_during_a_query_is_not_overwritten(util_win_cache):
    """E.g. a display change on another thread while the query is under way."""
    util_win, fake = util_win_cache
    answers = iter([True, False])

    def get_use_per_user_profiles(devicekey):
        fake.get_calls.append(devicekey)
        answer = next(answers)
        if answer:
            # The display configuration changes while WCS answers
            util_win.invalidate_per_user_profiles_cache()
        return answer

    fake.get_use_per_user_profiles = get_use_per_user_profiles

    with util_win.cached_per_user_profiles():
        assert util_win.per_user_profiles_isenabled(devicekey="key0") is True
        # The pre-change answer was not cached
        assert util_win.per_user_profiles_isenabled(devicekey="key0") is False

    assert fake.get_calls == ["key0", "key0"]


def test_enable_per_user_profiles_updates_cache(util_win_cache):
    util_win, fake = util_win_cache
    with util_win.cached_per_user_profiles():
        assert util_win.per_user_profiles_isenabled(devicekey="key0") is True

        util_win.enable_per_user_profiles(False, devicekey="key0")

        assert util_win.per_user_profiles_isenabled(devicekey="key0") is False

    assert fake.set_calls == [("key0", False)]
    assert fake.get_calls == ["key0"]


def test_per_user_profiles_errors_are_not_cached(util_win_cache):
    util_win, fake = util_win_cache
    fake.answer = TimeoutError("timed out")

    with util_win.cached_per_user_profiles():
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


def _hang():
    """Never return, like a WCS call that is stuck."""
    while True:
        time.sleep(1)


def _serve(request_queue, response_queue, hangs):
    """Tell the manager we are up, then answer requests with the method name.

    Hang instead when ``hangs(request)`` is true.
    """
    response_queue.put({"type": "worker_ready", "pid": os.getpid()})
    while True:
        try:
            request = request_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if request is None:
            break
        if hangs(request):
            _hang()
        response_queue.put(
            {"type": "resp_success", "id": request["id"], "result": request["method"]}
        )


def _hanging_worker(request_queue, response_queue, log_queue):
    """Hang on every "Hang" request."""
    _serve(request_queue, response_queue, lambda request: request["method"] == "Hang")


def _hangs_once(request):
    """Whether to hang: on a "HangOnce" request, if its marker file is new."""
    if request["method"] != "HangOnce":
        return False
    marker = request["args"][0]
    if os.path.exists(marker):
        return False
    with open(marker, "w"):
        pass
    return True


def _hang_once_worker(request_queue, response_queue, log_queue):
    """Hang on the first "HangOnce" request (in any worker process)."""
    _serve(request_queue, response_queue, _hangs_once)


def _slow_starting_worker(request_queue, response_queue, log_queue):
    """Take a while to start up (much longer than the request timeout)."""
    time.sleep(2)
    _serve(request_queue, response_queue, lambda request: False)
