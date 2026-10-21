"""Tests for the profile loader's per-display profile association polling.

``ProfileLoader._check_profile_associations`` runs every three seconds on the
display configuration monitoring thread and asks Windows (through the WCS
worker process) which profile each display uses. A failed lookup for one
display must be handled for that display only. It used to raise
UnboundLocalError for every error (the ``except ... as exception`` name is
unbound when the handler ends), which failed the whole monitoring iteration
and, after five of them in a row, showed the fatal error dialog and ended the
loader.

No hardware is touched: profile lookups, association writes, gamma ramps and
the configuration are stubbed, and no window is shown.
"""

import errno
import os
import sys
import threading

import pytest

from DisplayCAL import profile_loader
from DisplayCAL.profile_loader import ProfileLoader

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="The profile loader tray app is Windows-only"
)

if sys.platform == "win32":
    from DisplayCAL import mscms


class _FakeLang:
    @staticmethod
    def getstr(name, *args):
        return name


class _FakeDevice:
    def __init__(self, devicekey):
        self.DeviceKey = devicekey
        self.DeviceID = devicekey


def _make_pl(monkeypatch, fix_profile_associations=0):
    """A ProfileLoader (no __init__) with one display and stubbed I/O."""
    pl = ProfileLoader.__new__(ProfileLoader)
    pl.lock = threading.Lock()
    pl._shutdown = False
    pl.monitoring = True
    pl.monitors = [
        ("Display 1", None, {"Device": r"\\.\DISPLAY1"}, _FakeDevice("key0"))
    ]
    pl.profile_associations = {}
    pl.profiles = {}
    pl.ramps = {}
    pl.devices2profiles = {}
    pl.setgammaramp_success = {}
    pl._last_exception_args = ()
    pl._has_display_changed = False
    pl._manual_restore = False
    pl._next = False
    pl._current_display_key = None
    pl._reset_gamma_ramps = False
    # State that ProfileLoader.__init__ initializes
    pl._calibration_management_state = None
    pl._calibration_management_state_checked = None
    pl._last_wcs_error = None
    pl._last_monitor_traceback = None
    pl._window_count_drop_seen = False
    pl._window_count_request = None
    pl._last_window_count_error = None
    pl.gdi32 = None  # never read or write a gamma ramp
    pl._should_apply_profiles = lambda *args, **kwargs: False
    pl._is_displaycal_running = lambda: False
    pl.dry_run_checks = []
    pl._check_display_changed = lambda first_run=False, dry_run=False: (
        pl.dry_run_checks.append(dry_run) and False
    )
    monkeypatch.setattr(profile_loader, "lang", _FakeLang(), raising=False)
    monkeypatch.setattr(profile_loader, "DEBUG", False)
    monkeypatch.setattr(
        profile_loader,
        "getcfg",
        lambda name, *args, **kwargs: (
            fix_profile_associations
            if name == "profile_loader.fix_profile_associations"
            else 0
        ),
    )
    monkeypatch.setattr(profile_loader.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        profile_loader, "invalidate_per_user_profiles_cache", lambda *a, **k: None
    )
    monkeypatch.setattr(profile_loader, "get_active_display_device", lambda *a: None)
    return pl


def _raise(exception):
    def get_display_profile(display_no=0, path_only=False, devicekey=None):
        raise exception

    return get_display_profile


WCS_AND_OS_ERRORS = [
    TimeoutError("Timeout waiting for response to request 1234"),
    TimeoutError(),  # empty args
    PermissionError(errno.EACCES, "Access is denied"),
]
if sys.platform == "win32":
    WCS_AND_OS_ERRORS += [
        mscms.WCSWorkerError(
            "WCSWorker process PID 4242 is not running, exit code 3221225477 "
            "(0xC0000005), restart deferred"
        ),
        mscms.WCSWorkerDiedError("WCSWorker process died"),
        mscms.WCSManagerShutdownError("WCSManager is set to shut down"),
    ]


@pytest.mark.parametrize("first_run", [True, False])
@pytest.mark.parametrize("error", WCS_AND_OS_ERRORS, ids=repr)
def test_profile_lookup_error_is_handled_per_display(monkeypatch, error, first_run):
    pl = _make_pl(monkeypatch)
    monkeypatch.setattr(profile_loader, "get_display_profile", _raise(error))

    result = pl._check_profile_associations(first_run, True, set(), None, [], [])

    assert result == (1, False, True, None)
    # Error icon for this display, the iteration itself carries on
    assert pl.setgammaramp_success == {0: None}
    profile_key, mtime, desc = pl.profile_associations["key0"]
    # A stable, non-empty key (empty would mean "no profile assigned")
    assert profile_key.startswith(f"<{type(error).__name__}")
    assert mtime == 0
    assert desc == (str(error) or repr(error))
    assert pl._next is False


def _timeouts():
    """A get_display_profile that fails with a new request ID every call."""
    calls = []

    def get_display_profile(display_no=0, path_only=False, devicekey=None):
        calls.append(devicekey)
        raise TimeoutError(f"Timeout waiting for response to request {len(calls)}")

    return get_display_profile


@pytest.mark.parametrize("fix_profile_associations", [0, 1])
def test_repeated_lookup_error_is_not_a_new_association(
    monkeypatch, capsys, fix_profile_associations
):
    """Only the first of a run of identical failures counts as a change."""
    pl = _make_pl(monkeypatch, fix_profile_associations)
    monkeypatch.setattr(profile_loader, "get_display_profile", _timeouts())

    changed = [
        pl._check_profile_associations(first_run, True, set(), None, [], [])[0]
        for first_run in (True, False, False, False)
    ]

    assert changed == [1, 0, 0, 0]
    assert "A profile change has been detected" not in capsys.readouterr().out
    # No settle delay and immediate re-poll for a supposed association change
    assert pl.dry_run_checks == []
    assert pl._next is False


def test_lookup_error_after_a_profile_is_one_change(monkeypatch, capsys):
    pl = _make_pl(monkeypatch)
    monkeypatch.setattr(
        profile_loader,
        "get_display_profile",
        lambda display_no=0, path_only=False, devicekey=None: r"C:\x\good.icc",
    )
    monkeypatch.setattr(profile_loader, "get_profile_desc", lambda name: name)
    pl._check_profile_associations(True, True, set(), None, [], [])
    assert pl.profile_associations["key0"][0] == "good.icc"
    monkeypatch.setattr(profile_loader, "get_display_profile", _timeouts())

    changed = [
        pl._check_profile_associations(False, True, set(), None, [], [])[0]
        for _ in range(3)
    ]

    assert changed == [1, 0, 0]
    assert capsys.readouterr().out.count("A profile change has been detected") == 1
    assert pl.profile_associations["key0"][0] == "<TimeoutError>"


@pytest.mark.parametrize("first_run", [True, False])
def test_unassigned_profile_is_not_an_error(monkeypatch, first_run):
    pl = _make_pl(monkeypatch)
    monkeypatch.setattr(
        profile_loader,
        "get_display_profile",
        _raise(FileNotFoundError(errno.ENOENT, "No such file or directory")),
    )

    pl._check_profile_associations(first_run, True, set(), None, [], [])

    # Unassigned: no error icon, and the association is "no profile"
    assert pl.setgammaramp_success == {0: True}
    assert pl.profile_associations["key0"][0] == ""


def test_monitoring_iterations_survive_persistent_wcs_errors(monkeypatch):
    """The real iteration, with every profile lookup timing out.

    Before the fix, every iteration raised UnboundLocalError and the fifth one
    in a row was fatal (error dialog, loader exits).
    """
    pl = _make_pl(monkeypatch)
    pl._timestamp = 0
    pl.reload_count = 0
    pl._skip = False
    pl._display_changed_event = False
    pl._active_displays = ["key0"]
    pl._hwnds_pids = set()
    pl._program_pids = set()
    pl._ProfileLoader__apply_profiles = None
    pl._handle_profile_updates = lambda first_run, apply, running, *args: running
    pl._check_keep_running = lambda: True
    posted = []
    pl._post_to_gui_thread = lambda func, *args: posted.append(func)
    monkeypatch.setattr(
        profile_loader, "calibration_management_isenabled", lambda: False
    )
    lookups = []

    def get_display_profile(display_no=0, path_only=False, devicekey=None):
        lookups.append(devicekey)
        if len(lookups) >= ProfileLoader.MONITOR_MAX_CONSECUTIVE_FAILURES * 2:
            pl.monitoring = False
        raise TimeoutError(f"Timeout waiting for response to request {len(lookups)}")

    monkeypatch.setattr(profile_loader, "get_display_profile", get_display_profile)

    pl._check_display_conf_wrapper()

    assert len(lookups) == ProfileLoader.MONITOR_MAX_CONSECUTIVE_FAILURES * 2
    assert not [func for func in posted if func.__name__ == "_handle_fatal_error"]
    assert pl.setgammaramp_success == {0: None}


def test_failed_task_creation_is_reported_without_debug(monkeypatch, capsys):
    """``except Exception:`` used to read the unbound name ``exception``."""
    from DisplayCAL import taskscheduler

    class FakeTaskScheduler:
        stdout = b""

        def query_task(self, name):
            return False

        def create_task(self, *args, **kwargs):
            raise RuntimeError("access denied by policy")

    monkeypatch.setattr(taskscheduler, "TaskScheduler", FakeTaskScheduler)
    monkeypatch.setattr(profile_loader, "DEBUG", False)
    monkeypatch.setattr(profile_loader, "is_superuser", lambda: True)
    monkeypatch.setattr(profile_loader, "win_ver", lambda: ("Windows 7", 0, "", ""))
    monkeypatch.setattr(sys, "argv", [sys.argv[0]])

    exe = os.path.join("C:\\", "DisplayCAL", "DisplayCAL-apply-profiles.exe")
    profile_loader.setup_profile_loader_task(exe, os.path.dirname(exe), "C:\\Python")

    out = capsys.readouterr().out
    assert "Could not create task" in out
    assert "access denied by policy" in out
