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


def test_backoff_still_notices_request_to_exit(monkeypatch):
    harness = MonitorHarness(monkeypatch, [RuntimeError("transient"), STOP])
    harness.pl._check_keep_running = lambda: False
    harness.run()

    assert harness.calls == 1
    assert harness.pl.monitoring is False
    assert [func.__name__ for func in harness.posted] == ["_request_forced_shutdown"]


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


def test_repeated_gamma_ramp_read_error_is_logged_once(monkeypatch, capsys):
    pl = _make_pl()
    pl._manual_restore = False
    pl._last_exception_args = ()
    monkeypatch.setattr(profile_loader, "DEBUG", False)
    monkeypatch.setattr(profile_loader, "getcfg", lambda name, *args, **kwargs: 1)
    # Never touch a real display device context
    monkeypatch.setattr(profile_loader.win32gui, "CreateDC", lambda *args: 1234)
    monkeypatch.setattr(profile_loader.win32gui, "DeleteDC", lambda hdc: None)

    class FailingGDI32:
        @staticmethod
        def GetDeviceGammaRamp(hdc, ramp):  # noqa: N802
            raise OSError("exception: access violation reading 0x0")

    pl.gdi32 = FailingGDI32()
    for _ in range(3):
        idle, do_continue = pl._check_gamma_ramp_status(
            display="Display 1",
            idle=True,
            i=0,
            moninfo={"Device": r"\\.\DISPLAY1"},
            display_desc="1. Display 1",
            key="key0",
            profile_association_changed=False,
            apply_profiles=True,
            vcgt_values=([], [], []),
            hwnds_pids_changed=False,
        )
        assert (idle, do_continue) == (True, True)

    assert capsys.readouterr().out.count("Handled exception") == 1


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


# --- Shutdown / UI teardown only on the GUI thread -------------------------


def test_monitoring_thread_does_not_shut_down_itself(monkeypatch):
    """The UI must never be torn down from the monitoring thread."""
    harness = MonitorHarness(monkeypatch, [STOP])
    thread = threading.Thread(
        target=harness.pl._check_display_conf_wrapper,
        name="DisplayConfigurationMonitoring",
    )
    thread.start()
    thread.join(10)

    assert not thread.is_alive()
    assert harness.calls == 1
    assert harness.shutdown_threads == []


class ShutdownHarness:
    """A ProfileLoader whose shutdown() side effects are all recorded stubs."""

    def __init__(self, monkeypatch):
        self.pl = pl = _make_pl()
        self.events = []
        self.posted = []
        pl._shutdown_lock = threading.Lock()
        pl._reset_display_profile_associations = lambda: self.events.append(
            ("reset_associations", threading.current_thread())
        )
        pl.writecfg = lambda: self.events.append(
            ("writecfg", threading.current_thread())
        )
        pl._teardown_ui = lambda: self.events.append(
            ("teardown_ui", threading.current_thread())
        )
        pl._post_to_gui_thread = lambda func, *args: self.posted.append(func)
        monkeypatch.setattr(
            profile_loader,
            "getcfg",
            lambda name, *args, **kwargs: (
                1 if name == "profile_loader.fix_profile_associations" else 0
            ),
        )

    def names(self):
        return [name for name, _thread in self.events]


def test_shutdown_tears_down_ui_on_gui_thread(monkeypatch):
    harness = ShutdownHarness(monkeypatch)
    harness.pl.shutdown()
    harness.pl.shutdown()

    assert harness.names() == ["reset_associations", "writecfg", "teardown_ui"]
    assert harness.events[-1][1] is threading.main_thread()
    assert harness.pl.monitoring is False


def test_shutdown_off_gui_thread_posts_ui_teardown(monkeypatch):
    harness = ShutdownHarness(monkeypatch)
    thread = threading.Thread(target=harness.pl.shutdown)
    thread.start()
    thread.join(10)

    assert "teardown_ui" not in harness.names()
    assert len(harness.posted) == 1
    harness.posted[0]()  # what the GUI thread's event loop would do
    assert harness.events[-1] == ("teardown_ui", threading.main_thread())


def test_shutdown_runs_once_when_called_concurrently(monkeypatch):
    harness = ShutdownHarness(monkeypatch)
    barrier = threading.Barrier(5)

    def call_shutdown():
        barrier.wait()
        harness.pl.shutdown()

    threads = [threading.Thread(target=call_shutdown) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert harness.names().count("writecfg") == 1
    assert harness.names().count("reset_associations") == 1


def test_shutdown_waits_for_monitoring_thread(monkeypatch):
    harness = ShutdownHarness(monkeypatch)
    pl = harness.pl

    def monitor():
        while pl.monitoring:
            time.sleep(0.2)
        harness.events.append(("monitor_exited", threading.current_thread()))

    pl._check_display_conf_thread = threading.Thread(
        target=monitor, name="DisplayConfigurationMonitoring"
    )
    pl._check_display_conf_thread.start()
    pl.shutdown()

    assert not pl._check_display_conf_thread.is_alive()
    # The monitoring thread is gone before associations are reset
    assert harness.names()[0] == "monitor_exited"


def test_shutdown_still_writes_config_if_reset_fails(monkeypatch, capsys):
    harness = ShutdownHarness(monkeypatch)

    def reset_display_profile_associations():
        raise TimeoutError("WCS worker did not answer")

    harness.pl._reset_display_profile_associations = reset_display_profile_associations
    harness.pl.shutdown()

    assert harness.names() == ["writecfg", "teardown_ui"]
    assert "TimeoutError" in capsys.readouterr().err


def test_forced_shutdown_shuts_down_before_closing_frame(monkeypatch):
    harness = ShutdownHarness(monkeypatch)

    class Frame:
        def Close(self, force=False):  # noqa: N802
            harness.events.append(("close_frame", force))

    harness.pl.frame = Frame()
    harness.pl._request_forced_shutdown()

    assert harness.names() == [
        "reset_associations",
        "writecfg",
        "teardown_ui",
        "close_frame",
    ]
    assert harness.events[-1] == ("close_frame", True)


# --- WCS worker process IPC failures ----------------------------------------


def _in_thread(func):
    """Run ``func`` on a non-GUI thread and return its result (or raise)."""
    thread, outcome = _run_in_monitor_thread(func)
    thread.join(10)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_calibration_management_state_survives_wcs_errors(monkeypatch, capsys):
    pl = _make_pl()
    answers = iter([True, TimeoutError("timed out"), TimeoutError("timed out")])

    def calibration_management_isenabled():
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(
        profile_loader,
        "calibration_management_isenabled",
        calibration_management_isenabled,
    )

    assert _in_thread(pl._calibration_management_isenabled) is True
    # Worker stalled: the last known state is returned, the error logged once
    assert _in_thread(pl._calibration_management_isenabled) is True
    assert _in_thread(pl._calibration_management_isenabled) is True
    assert capsys.readouterr().out.count("TimeoutError: timed out") == 1


def test_calibration_management_state_on_gui_thread_does_not_block(monkeypatch):
    pl = _make_pl()
    pl._calibration_management_state = True

    def calibration_management_isenabled():
        raise AssertionError("the GUI thread must not wait for the WCS worker")

    monkeypatch.setattr(
        profile_loader,
        "calibration_management_isenabled",
        calibration_management_isenabled,
    )

    assert pl._calibration_management_isenabled() is True


def test_should_apply_profiles_survives_wcs_timeout(monkeypatch):
    pl = _make_pl()
    pl._skip = False
    pl._manual_restore = False
    pl._is_other_running = lambda enumerate_windows_and_processes=True: False
    pl._is_displaycal_running = lambda: False
    monkeypatch.setattr(profile_loader.config, "getcfg", lambda name, *args: 1)

    def calibration_management_isenabled():
        raise TimeoutError("Timeout waiting for response to request")

    monkeypatch.setattr(
        profile_loader,
        "calibration_management_isenabled",
        calibration_management_isenabled,
    )

    assert _in_thread(pl._should_apply_profiles) is True


class _FakeDevice:
    def __init__(self, devicekey):
        self.DeviceKey = devicekey
        self.DeviceID = devicekey


def _wcs_write_fails(*args, **kwargs):
    raise ValueError("WCSWorkerError: unexpected error in worker")


def test_set_active_profile_survives_wcs_errors(monkeypatch):
    pl = _make_pl()
    device = _FakeDevice("key0")
    pl.devices2profiles = {"key0": (("Display 1",), "wrong.icc", "Wrong")}
    pl._fixed_profile_associations = set()
    monkeypatch.setattr(
        profile_loader, "get_display_profile", lambda **kwargs: r"C:\x\right.icc"
    )
    monkeypatch.setattr(profile_loader, "is_superuser", lambda: True)
    monkeypatch.setattr(profile_loader, "set_display_profile", _wcs_write_fails)

    pl._set_active_profile(device, "Display 1", [device])

    assert pl._fixed_profile_associations == set()


def test_reset_display_profile_associations_survives_wcs_errors(monkeypatch):
    pl = _make_pl()
    pl._can_fix_profile_associations = lambda: True
    pl.devices2profiles = {"key0": (("Display 1",), "right.icc", "Right")}
    pl._fixed_profile_associations = {"key0"}
    monkeypatch.setattr(
        profile_loader, "get_display_profile", lambda **kwargs: r"C:\x\other.icc"
    )
    monkeypatch.setattr(profile_loader, "is_superuser", lambda: True)
    monkeypatch.setattr(profile_loader, "set_display_profile", _wcs_write_fails)
    unset_calls = []
    monkeypatch.setattr(
        profile_loader,
        "unset_display_profile",
        lambda *args, **kwargs: unset_calls.append(args),
    )

    pl._reset_display_profile_associations()

    assert unset_calls == []


def test_display_change_invalidates_per_user_profiles_cache(monkeypatch):
    from DisplayCAL import util_win

    queries = []

    class FakeWCSManager:
        @staticmethod
        def get_use_per_user_profiles(devicekey):
            queries.append(devicekey)
            return True

    # Never start a real WCS worker process from this test
    monkeypatch.setattr(util_win, "mscms", FakeWCSManager())
    util_win.invalidate_per_user_profiles_cache()
    pl = _make_pl()
    pl._display_changed_event = False
    pl._next = False
    try:
        util_win.per_user_profiles_isenabled(devicekey="key0")
        util_win.per_user_profiles_isenabled(devicekey="key0")
        pl._process_display_changed()
        util_win.per_user_profiles_isenabled(devicekey="key0")
    finally:
        util_win.invalidate_per_user_profiles_cache()

    assert queries == ["key0", "key0"]
    assert pl._display_changed_event is True


# --- GraphicsDrivers\Configuration registry key not accessible --------------


class _FakeLang:
    @staticmethod
    def getstr(name, *args):
        return name


def test_registry_access_denied_uses_fallback_without_warning(
    monkeypatch, recwarn, capsys
):
    pl = _make_pl()
    pl._display_changed_event = False
    pl._active_displays = []
    display_keys = ["key-a", "key-b"]
    open_calls = []

    def open_key(*args):
        open_calls.append(args)
        raise PermissionError(13, "Access is denied", None, 5)

    monkeypatch.setattr(profile_loader.winreg, "OpenKey", open_key)
    monkeypatch.setattr(
        profile_loader, "get_active_display_devices", lambda attr: list(display_keys)
    )
    monkeypatch.setattr(
        profile_loader,
        "getcfg",
        lambda name, *args, **kwargs: (
            1 if name == "profile_loader.fix_profile_associations" else 0
        ),
    )
    monkeypatch.setattr(profile_loader, "lang", _FakeLang(), raising=False)
    association_calls = []
    pl._enumerate_monitors = lambda: setattr(
        pl, "_active_displays", list(display_keys)
    )
    pl._set_display_profiles = lambda dry_run=False: association_calls.append("set")
    pl._reset_display_profile_associations = lambda: association_calls.append("reset")
    pl._is_displaycal_running = lambda: False

    results = []
    for first_run, added_display in ((True, None), (False, None), (False, "key-c")):
        if added_display:
            display_keys.append(added_display)
        pl._display_changed_event = True
        # Always the same caller line, so warnings' once-per-location
        # de-duplication cannot hide a warning
        results.append(pl._check_display_changed(first_run))

    assert results == [True, False, True]
    assert association_calls == ["set", "reset", "set"]
    assert len(open_calls) == 1
    assert not [w for w in recwarn if "Registry access failed" in str(w.message)]
    assert capsys.readouterr().out.count("is not accessible") == 1
