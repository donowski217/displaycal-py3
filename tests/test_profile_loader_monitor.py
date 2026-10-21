"""Tests for the Windows profile loader's display configuration monitoring.

These cover the DisplayConfigurationMonitoring thread and the helpers it calls
every few seconds. A single exception there used to end the thread, show an
error dialog and exit the loader (the user-visible "crash" of the tray app).

No hardware is touched: every call that would read or change video card gamma
ramps, ICC profile associations or the configuration file is stubbed, and no
window is ever shown.
"""

import ctypes
import os
import queue
import sys
import threading
import time

import pytest

from DisplayCAL import profile_loader
from DisplayCAL.profile_loader import ProfileLoader

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="The profile loader tray app is Windows-only"
)

if sys.platform == "win32":
    import pywintypes


def _make_pl():
    """Return a ProfileLoader without running __init__ (no wx, no threads)."""
    pl = ProfileLoader.__new__(ProfileLoader)
    pl.lock = threading.Lock()
    pl._is_other_running_lock = threading.Lock()
    pl._shutdown = False
    pl.monitoring = True
    pl._hwnds_pids = set()
    pl._ProfileLoader__other_component = (None, None, 0)
    pl._known_window_classes = {"CalClient.exe"}
    pl._known_apps = set()
    pl._exceptions = {}
    pl._exception_names = set()
    pl._pid = os.getpid()
    pl._tid = threading.get_ident()
    pl._madvr_reset_cal = {}
    pl.monitors = []
    pl._current_display_key = None
    return pl


STOP = "stop"
OK = "ok"


class MonitorHarness:
    """Drive the real monitoring loop with scripted iteration outcomes.

    Everything the loop calls that could touch displays, LUTs, profile
    associations, the config file or wx is replaced by a stub. Each entry of
    ``script`` is the outcome of one ``_check_profile_associations`` call: an
    exception instance (raised), ``OK`` or ``STOP`` (ends monitoring).
    """

    def __init__(self, monkeypatch, script):
        self.pl = pl = _make_pl()
        self.script = list(script)
        self.calls = 0
        self.posted = []
        self.shutdown_threads = []
        pl._timestamp = time.time()
        pl.reload_count = 0
        pl._has_display_changed = False
        pl._manual_restore = False
        pl._skip = False
        pl._display_changed_event = False
        pl._active_displays = ["display-key"]
        pl._ProfileLoader__apply_profiles = None
        pl._post_to_gui_thread = lambda func, *args: self.posted.append(func)
        pl._should_apply_profiles = lambda *args, **kwargs: False
        pl._check_display_changed = lambda first_run=False, dry_run=False: False
        pl._check_profile_associations = self._check_profile_associations
        pl._handle_profile_updates = lambda *args: None
        pl._check_keep_running = lambda: True
        pl.shutdown = lambda: self.shutdown_threads.append(threading.current_thread())
        monkeypatch.setattr(profile_loader.time, "sleep", lambda seconds: None)

    def _check_profile_associations(self, first_run, idle, *args):
        assert self.pl.lock.locked(), "iteration must run with self.lock held"
        outcome = self.script[self.calls] if self.calls < len(self.script) else STOP
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == STOP:
            self.pl.monitoring = False
        return 0, False, True, None

    def run(self):
        self.pl._check_display_conf_wrapper()
        return self

    @property
    def fatal_posted(self):
        return any(
            getattr(func, "__name__", "") == "_handle_fatal_error"
            for func in self.posted
        )


# --- _is_other_running / window enumeration callbacks ----------------------


def test_is_other_running_survives_oserror_from_enumwindows(monkeypatch):
    """pywin32 passes an OSError raised in the callback straight through."""
    pl = _make_pl()

    def enum_windows(callback, extra):
        raise ctypes.WinError(31)

    monkeypatch.setattr(profile_loader.win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(pl, "_look_for_known_processes", lambda other: None)

    assert pl._is_other_running(True) is False
    assert not pl._is_other_running_lock.locked()


def test_is_other_running_releases_lock_on_error(monkeypatch):
    """A failure while holding the lock must not leave it held (deadlock)."""
    pl = _make_pl()
    monkeypatch.setattr(profile_loader.win32gui, "EnumWindows", lambda cb, x: None)

    def look_for_known_processes(other_component):
        raise RuntimeError("boom")

    monkeypatch.setattr(pl, "_look_for_known_processes", look_for_known_processes)

    with pytest.raises(RuntimeError):
        pl._is_other_running(True)
    assert not pl._is_other_running_lock.locked()


def test_is_other_running_with_exiting_process_of_known_window(monkeypatch):
    """Real EnumWindows: a known window whose process just exited.

    get_process_filename raises ctypes.WinError (an OSError, e.g. WinError 31
    for an exited process), which is not a pywintypes.error.
    """
    pl = _make_pl()
    monkeypatch.setattr(
        profile_loader.win32gui, "GetClassName", lambda hwnd: "CalClient.exe"
    )

    def get_process_filename(pid):
        raise ctypes.WinError(31)

    monkeypatch.setattr(profile_loader, "get_process_filename", get_process_filename)
    monkeypatch.setattr(pl, "_look_for_known_processes", lambda other: None)

    assert pl._is_other_running(True) is False
    assert not pl._is_other_running_lock.locked()
    assert pl._hwnds_pids == set()


def test_enumerate_windows_callback_skips_vanished_window(monkeypatch):
    pl = _make_pl()

    def get_class_name(hwnd):
        raise pywintypes.error(1400, "GetClassName", "Invalid window handle.")

    monkeypatch.setattr(profile_loader.win32gui, "GetClassName", get_class_name)

    pl._enumerate_windows_callback(12345, None)
    assert pl._hwnds_pids == set()


def test_enumerate_windows_callback_records_known_window(monkeypatch):
    pl = _make_pl()
    monkeypatch.setattr(
        profile_loader.win32gui, "GetClassName", lambda hwnd: "CalClient.exe"
    )
    monkeypatch.setattr(
        profile_loader.win32process, "GetWindowThreadProcessId", lambda hwnd: (7, 42)
    )
    monkeypatch.setattr(
        profile_loader, "get_process_filename", lambda pid: r"C:\x\CalClient.exe"
    )

    pl._enumerate_windows_callback(12345, None)
    assert pl._hwnds_pids == {(r"C:\x\CalClient.exe", 42, 7, 12345)}
    assert pl._ProfileLoader__other_component == (
        r"C:\x\CalClient.exe",
        "CalClient.exe",
        0,
    )


def test_enumerate_own_windows_callback_skips_vanished_window(monkeypatch):
    pl = _make_pl()

    def get_class_name(hwnd):
        raise pywintypes.error(1400, "GetClassName", "Invalid window handle.")

    monkeypatch.setattr(profile_loader.win32gui, "GetClassName", get_class_name)
    windows = []
    pl._enumerate_own_windows_callback(12345, windows)
    assert windows == []

    monkeypatch.setattr(
        profile_loader.win32gui, "GetClassName", lambda hwnd: "SysTrayIcon"
    )
    pl._enumerate_own_windows_callback(12345, windows)
    assert windows == ["SysTrayIcon"]


# --- Monitoring loop resilience --------------------------------------------


def test_transient_error_does_not_kill_monitoring(monkeypatch, capsys):
    harness = MonitorHarness(monkeypatch, [RuntimeError("transient"), STOP]).run()

    assert harness.calls == 2
    assert not harness.fatal_posted
    assert not harness.pl.lock.locked()
    # The real traceback is logged (stderr), not a bare 'NoneType: None'
    assert "RuntimeError: transient" in capsys.readouterr().err


def test_escalates_after_consecutive_failures(monkeypatch):
    errors = [RuntimeError(f"failure {i}") for i in range(10)]
    harness = MonitorHarness(monkeypatch, errors).run()

    assert harness.calls == ProfileLoader.MONITOR_MAX_CONSECUTIVE_FAILURES
    assert harness.fatal_posted
    assert not harness.pl.lock.locked()


def test_failure_counter_resets_after_success(monkeypatch):
    error = RuntimeError("transient")
    script = [error] * 4 + [OK] + [error] * 4 + [STOP]
    harness = MonitorHarness(monkeypatch, script).run()

    assert harness.calls == len(script)
    assert not harness.fatal_posted


def test_wait_loop_error_is_not_fatal(monkeypatch):
    """E.g. the wx top-level window race in the keep-running check."""
    harness = MonitorHarness(monkeypatch, [OK, STOP])
    keep_running_calls = []

    def check_keep_running():
        keep_running_calls.append(None)
        if len(keep_running_calls) == 1:
            raise RuntimeError("wrapped C/C++ object of type Frame has been deleted")
        return True

    harness.pl._check_keep_running = check_keep_running
    harness.run()

    assert harness.calls == 2
    assert not harness.fatal_posted


def test_initial_should_apply_profiles_error_is_not_fatal(monkeypatch):
    harness = MonitorHarness(monkeypatch, [STOP])
    should_apply_calls = []

    def should_apply_profiles(*args, **kwargs):
        should_apply_calls.append(None)
        if len(should_apply_calls) == 1:
            raise TimeoutError("WCS worker did not answer")
        return False

    harness.pl._should_apply_profiles = should_apply_profiles
    harness.run()

    assert harness.calls == 1
    assert not harness.fatal_posted


def test_wrapper_does_not_release_a_lock_it_does_not_own(monkeypatch):
    """Releasing a lock held by another thread breaks that thread later."""
    harness = MonitorHarness(monkeypatch, [])

    def check_display_conf():
        raise RuntimeError("fatal")

    harness.pl._check_display_conf = check_display_conf
    # Another thread (e.g. the GUI thread) holds the lock right now
    assert harness.pl.lock.acquire(blocking=False)
    try:
        harness.run()
        assert harness.pl.lock.locked()
    finally:
        harness.pl.lock.release()
    assert harness.fatal_posted


def test_failed_display_change_is_redone(monkeypatch):
    """A failure while handling a display change must not be forgotten."""
    harness = MonitorHarness(monkeypatch, [STOP])
    pl = harness.pl
    seen = []

    def check_display_changed(first_run=False, dry_run=False):
        seen.append((first_run, pl._display_changed_event, list(pl._active_displays)))
        if len(seen) == 1:
            # The new configuration was already recorded when the error hit
            pl._active_displays = ["new-display-key"]
            raise RuntimeError("failed while fixing profile associations")
        return False

    pl._check_display_changed = check_display_changed
    harness.run()

    assert len(seen) == 2
    first_run, event, active_displays = seen[1]
    assert first_run is True
    assert event is True
    assert active_displays == []
    assert not harness.fatal_posted


def test_backoff_grows_and_is_capped(monkeypatch, capsys):
    error = RuntimeError("transient")
    harness = MonitorHarness(monkeypatch, [error] * 7 + [STOP])
    harness.pl.MONITOR_MAX_CONSECUTIVE_FAILURES = 10
    harness.run()

    out = capsys.readouterr().out
    delays = [
        int(line.rsplit("retrying in ", 1)[1].split()[0])
        for line in out.splitlines()
        if "retrying in" in line
    ]
    assert delays == [3, 6, 12, 24, 30, 30, 30]
    assert not harness.fatal_posted


def test_identical_tracebacks_are_logged_once(monkeypatch, capsys):
    def raise_same_error():
        raise RuntimeError("same")

    harness = MonitorHarness(monkeypatch, [])
    script = iter([raise_same_error, raise_same_error, None])

    def check_profile_associations(*args):
        step = next(script)
        if step is None:
            harness.pl.monitoring = False
        else:
            step()
        return 0, False, True, None

    harness.pl._check_profile_associations = check_profile_associations
    harness.run()

    captured = capsys.readouterr()
    assert captured.err.count("RuntimeError: same") == 1
    assert "same as before" in captured.out


def test_steady_state_iteration_is_quiet(monkeypatch, capsys):
    """No per-iteration lock chatter in the log during normal operation."""
    monkeypatch.setattr(profile_loader, "DEBUG", False)
    monkeypatch.setattr(profile_loader, "VERBOSE", 0)
    MonitorHarness(monkeypatch, [OK, STOP]).run()

    out = capsys.readouterr().out
    assert "lock" not in out.lower()


def test_apply_profiles_releases_lock_on_error(monkeypatch):
    from DisplayCAL import worker

    pl = _make_pl()
    pl._tray_active = True

    class BrokenWorker:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no worker")

    monkeypatch.setattr(worker, "Worker", BrokenWorker)

    with pytest.raises(RuntimeError):
        pl.apply_profiles()
    assert not pl.lock.locked()


# --- Keep-running check: no wx calls off the GUI thread --------------------


def _run_in_monitor_thread(func):
    """Run ``func`` in a thread named like the real monitoring thread."""
    outcome = {}

    def target():
        try:
            outcome["result"] = func()
        except BaseException as exception:  # pragma: no cover - reported below
            outcome["error"] = exception

    thread = threading.Thread(target=target, name="DisplayConfigurationMonitoring")
    thread.start()
    return thread, outcome


def test_keep_running_counts_windows_on_gui_thread(monkeypatch):
    pl = _make_pl()
    pl.numwindows = 2
    gui_queue = queue.Queue()
    enumerated_on = []

    def enumerate_own_top_level_windows():
        enumerated_on.append(threading.current_thread())
        return ["SysTrayIcon", "frame"]

    pl._enumerate_own_top_level_windows = enumerate_own_top_level_windows
    pl._post_to_gui_thread = lambda func, *args: gui_queue.put((func, args))

    thread, outcome = _run_in_monitor_thread(pl._check_keep_running)
    # Act as the GUI thread's event loop
    func, args = gui_queue.get(timeout=5)
    func(*args)
    thread.join(5)

    assert outcome == {"result": True}
    assert enumerated_on == [threading.main_thread()]
    assert pl.numwindows == 2


def test_keep_running_assumes_true_while_gui_thread_is_busy(monkeypatch):
    pl = _make_pl()
    pl.numwindows = 2
    pl.GUI_REQUEST_TIMEOUT = 0.05
    posted = []
    pl._post_to_gui_thread = lambda func, *args: posted.append(func)

    def enumerate_own_top_level_windows():
        raise AssertionError("must not enumerate on the monitoring thread")

    pl._enumerate_own_top_level_windows = enumerate_own_top_level_windows

    for _ in range(3):
        thread, outcome = _run_in_monitor_thread(pl._check_keep_running)
        thread.join(5)
        assert outcome == {"result": True}
    # Only one request is ever outstanding
    assert len(posted) == 1


def test_keep_running_does_not_post_while_shutting_down():
    pl = _make_pl()
    pl.numwindows = 2
    pl.monitoring = False
    posted = []
    pl._post_to_gui_thread = lambda func, *args: posted.append(func)

    thread, outcome = _run_in_monitor_thread(pl._check_keep_running)
    thread.join(5)

    assert outcome == {"result": True}
    assert posted == []


def test_keep_running_needs_two_consecutive_lower_counts():
    pl = _make_pl()
    pl.numwindows = 0
    counts = iter([3, 2, 3, 2, 2])
    pl._enumerate_own_top_level_windows = lambda: [None] * next(counts)

    results = [pl._check_keep_running() for _ in range(5)]

    assert results == [True, True, True, True, False]


def test_keep_running_when_enumeration_fails():
    pl = _make_pl()
    pl.numwindows = 2

    def enumerate_own_top_level_windows():
        raise RuntimeError("wrapped C/C++ object of type Frame has been deleted")

    pl._enumerate_own_top_level_windows = enumerate_own_top_level_windows

    assert pl._check_keep_running() is True
    assert pl.numwindows == 2


class _FakeWindow:
    def __init__(self, name, being_deleted=False):
        self.name = name
        self.being_deleted = being_deleted

    def IsBeingDeleted(self):  # noqa: N802
        return self.being_deleted

    @property
    def Name(self):  # noqa: N802
        return self.name


class _DeletedWindow:
    def IsBeingDeleted(self):  # noqa: N802
        raise RuntimeError("wrapped C/C++ object of type Frame has been deleted")

    @property
    def Name(self):  # noqa: N802
        raise RuntimeError("wrapped C/C++ object of type Frame has been deleted")


def test_enumerate_own_top_level_windows_skips_dead_windows(monkeypatch):
    pl = _make_pl()
    frame = _FakeWindow("frame")
    monkeypatch.setattr(
        profile_loader.win32gui, "EnumThreadWindows", lambda tid, cb, extra: None
    )
    monkeypatch.setattr(
        profile_loader.wx,
        "GetTopLevelWindows",
        lambda: [
            _DeletedWindow(),
            _FakeWindow("other", being_deleted=True),
            frame,
            _FakeWindow("TaskBarNotification"),
        ],
    )

    assert pl._enumerate_own_top_level_windows() == [frame]
