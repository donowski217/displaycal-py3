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
import sys
import threading

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
