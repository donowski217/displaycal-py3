"""Tests for the Qt port of the apply-profiles hot-plug monitoring thread (issue #891).

``QtProfileLoader`` inherits
:meth:`DisplayCAL.profile_loader.ProfileLoader._check_display_conf` (the
Windows display/process hot-plug poller) unchanged; only the toolkit hooks it
calls to touch UI state from its background thread
(``_post_to_gui_thread``/``_post_to_gui_thread_delayed``,
``_animate_busy_icon``, ``_enumerate_own_top_level_windows``,
``_request_forced_shutdown``) are overridden here, plus ``exit()``'s
confirmation gate. These tests exercise those seams directly rather than the
inherited Windows-only polling logic itself (which needs a real win32
environment to run meaningfully and is unchanged/already covered by the wx
build's production usage).
"""

import os
from unittest import mock

import pytest

from DisplayCAL import config

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

pytest.importorskip("qtpy")

from qtpy.QtWidgets import QDialog, QWidget  # noqa: E402

from DisplayCAL.ui.tools.apply_profiles import (  # noqa: E402
    QtProfileLoader,
    _CallBridge,
)


@pytest.fixture(scope="session")
def qapp():
    """Provide a singleton offscreen QApplication for the test session."""
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _init_config():
    config.initcfg()
    yield


def _make_pl(monkeypatch):
    """Build a ``QtProfileLoader`` without running ``__init__``'s Qt bootstrap."""
    pl = QtProfileLoader.__new__(QtProfileLoader)
    monkeypatch.setattr(pl, "get_title", lambda: "DisplayCAL Apply Profiles")
    # Last known Windows calibration management state (see
    # ProfileLoader._calibration_management_isenabled)
    pl._calibration_management_state = False
    # Never start (or query) a real WCS worker process from these tests
    _forbid_wcs_queries(monkeypatch)
    return pl


def _forbid_wcs_queries(monkeypatch):
    """Make every direct WCS worker query fail (and record it).

    E.g. a worker in its restart cooldown raises WCSWorkerError at once, a
    stalled one only after about a minute. The GUI thread must not ask.
    """
    from DisplayCAL import profile_loader
    from DisplayCAL.ui.tools import apply_profiles

    queries = []

    def calibration_management_isenabled():
        queries.append(None)
        raise RuntimeError("WCSWorkerError: restart deferred")

    for module in (profile_loader, apply_profiles):
        monkeypatch.setattr(
            module,
            "calibration_management_isenabled",
            calibration_management_isenabled,
            raising=False,
        )
    return queries


def _fake_lang(monkeypatch):
    from DisplayCAL.ui.tools import apply_profiles

    monkeypatch.setattr(apply_profiles.lang, "getstr", lambda name, *args: name)
    monkeypatch.setattr(apply_profiles.lang, "getcode", lambda: "en")


class _FakeApp:
    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


def test_enumerate_own_top_level_windows_excludes_dialogs(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    widget = QWidget()
    dialog = QDialog()
    try:
        windows = pl._enumerate_own_top_level_windows()
        assert widget in windows
        assert dialog not in windows
    finally:
        widget.deleteLater()
        dialog.deleteLater()
        qapp.processEvents()


def test_post_to_gui_thread_runs_the_callable(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    pl._call_bridge = _CallBridge()
    pl._call_bridge.requested.connect(pl._dispatch_call)

    calls = []
    pl._post_to_gui_thread(calls.append, "from-background")
    # A direct (non-queued) connection above runs synchronously; assert the
    # callable is actually invoked with the given args rather than swallowed.
    assert calls == ["from-background"]


def test_post_to_gui_thread_delayed_schedules_with_qtimer(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    pl._call_bridge = _CallBridge()
    pl._call_bridge.requested.connect(pl._dispatch_call)

    with mock.patch(
        "DisplayCAL.ui.tools.apply_profiles.QTimer.singleShot"
    ) as single_shot:
        pl._post_to_gui_thread_delayed(1000, lambda: None)
    assert single_shot.call_count == 1
    assert single_shot.call_args[0][0] == 1000


def test_animate_busy_icon_steps_the_tray_animation(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    pl.tray = mock.Mock()
    pl._animate_busy_icon()
    pl.tray.animate.assert_called_once_with()


def test_animate_busy_icon_without_a_tray_is_a_noop(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    pl.tray = None
    assert pl._animate_busy_icon() is None


def test_request_forced_shutdown_quits_without_confirmation(qapp, monkeypatch):
    pl = _make_pl(monkeypatch)
    fake_app = _FakeApp()
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QApplication.instance",
        staticmethod(lambda: fake_app),
    )
    pl._request_forced_shutdown()
    assert fake_app.quit_calls == 1


def test_exit_with_no_event_skips_confirmation(qapp, monkeypatch):
    """Automated callers (oneshot exit, fatal error) pass no event -- must not block on a prompt."""
    pl = _make_pl(monkeypatch)
    fake_app = _FakeApp()
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QApplication.instance",
        staticmethod(lambda: fake_app),
    )
    question = mock.Mock(side_effect=AssertionError("should not prompt"))
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QMessageBox.question", question
    )

    pl.exit()

    question.assert_not_called()
    assert fake_app.quit_calls == 1


def test_exit_from_tray_menu_confirms_and_quits_on_yes(qapp, monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    pl = _make_pl(monkeypatch)
    fake_app = _FakeApp()
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QApplication.instance",
        staticmethod(lambda: fake_app),
    )
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )

    # Qt's QAction.triggered(bool) passes checked=False, not None.
    pl.exit(False)

    assert fake_app.quit_calls == 1


def test_exit_from_tray_menu_cancelled_does_not_quit(qapp, monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    pl = _make_pl(monkeypatch)
    fake_app = _FakeApp()
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QApplication.instance",
        staticmethod(lambda: fake_app),
    )
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )

    pl.exit(False)

    assert fake_app.quit_calls == 0


def test_exit_from_tray_menu_does_not_query_the_wcs_worker(qapp, monkeypatch):
    """Quit used to do nothing during the WCS worker's restart cooldown."""
    from qtpy.QtWidgets import QMessageBox

    pl = _make_pl(monkeypatch)
    pl._calibration_management_state = None  # e.g. the startup query failed
    queries = _forbid_wcs_queries(monkeypatch)
    fake_app = _FakeApp()
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QApplication.instance",
        staticmethod(lambda: fake_app),
    )
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )

    pl.exit(False)

    assert fake_app.quit_calls == 1
    assert queries == []


def test_apply_profiles_command_uses_last_known_state(qapp, monkeypatch):
    """And re-queries the cached per-user profiles flags, like wx."""
    from types import SimpleNamespace

    from DisplayCAL import profile_loader
    from DisplayCAL.ui.tools.apply_profiles import _ScriptingHost

    pl = _make_pl(monkeypatch)
    pl._calibration_management_state = True
    queries = _forbid_wcs_queries(monkeypatch)
    _fake_lang(monkeypatch)
    invalidations = []
    monkeypatch.setattr(
        profile_loader,
        "invalidate_per_user_profiles_cache",
        lambda devicekey=None: invalidations.append(devicekey),
    )
    host = SimpleNamespace(pl=pl)

    result = _ScriptingHost.process_data(host, ["apply-profiles"])

    assert result == "calibration.load.handled_by_os"
    assert queries == []
    assert invalidations == [None]


def test_display_changed_command_does_not_wait_for_a_stuck_iteration(qapp, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    from DisplayCAL.ui.tools.apply_profiles import _ScriptingHost

    pl = _make_pl(monkeypatch)
    pl.lock = threading.Lock()
    pl._shutdown = False
    pl._has_display_changed = True
    pl._manual_restore = False
    pl.GUI_LOCK_TIMEOUT = 0.05
    pl._is_displaycal_running = lambda: False
    pl._is_other_running = lambda enumerate_windows_and_processes=True: False
    reapplies = []
    pl.trigger_reapply = lambda: reapplies.append(None)
    delayed = []
    pl._post_to_gui_thread_delayed = lambda ms, func, *args: delayed.append(func)
    from DisplayCAL import profile_loader

    monkeypatch.setattr(profile_loader, "getcfg", lambda name, *args, **kwargs: 1)
    _forbid_wcs_queries(monkeypatch)
    host = SimpleNamespace(pl=pl)
    release = threading.Event()
    holding = threading.Event()

    def stuck_iteration():
        with pl.lock:
            holding.set()
            release.wait(3)

    thread = threading.Thread(target=stuck_iteration)
    thread.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        result = _ScriptingHost.process_data(
            host, ["apply-profiles", "display-changed"]
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(5)

    assert result == "ok"
    assert elapsed < 1
    assert reapplies == [None]
    assert pl._manual_restore is False
    for func in delayed:  # the GUI thread's retry timer
        func()
    assert pl._manual_restore == 2


def test_tray_menu_uses_last_known_state(qapp, monkeypatch):
    """Rebuilt on every popup: it must not wait for (or fail on) WCS.

    On Windows it also used to fail building the "Open display settings"
    item, which did not exist on the Qt loader.
    """
    from qtpy.QtWidgets import QMenu

    from DisplayCAL.ui.tools.apply_profiles import ApplyProfilesTrayIcon

    pl = _make_pl(monkeypatch)
    pl._calibration_management_state = True
    pl._is_displaycal_running = lambda: False
    pl._is_other_running = lambda enumerate_windows_and_processes=True: False
    pl._reset_gamma_ramps = False
    pl._can_fix_profile_associations = lambda: False
    queries = _forbid_wcs_queries(monkeypatch)
    _fake_lang(monkeypatch)
    tray = ApplyProfilesTrayIcon.__new__(ApplyProfilesTrayIcon)
    tray.pl = pl
    tray._menu = QMenu()
    try:
        tray._rebuild_menu()

        actions = {action.text(): action for action in tray._menu.actions()}
    finally:
        tray._menu.deleteLater()
        qapp.processEvents()

    assert queries == []
    # Windows manages calibration: loading/resetting it here is disabled
    assert not actions["calibration.load_from_display_profiles"].isEnabled()
    assert "menuitem.quit" in actions


def test_open_display_settings(qapp, monkeypatch):
    from DisplayCAL.ui.tools import apply_profiles

    calls = []
    monkeypatch.setattr(
        apply_profiles.sp, "call", lambda args, **kwargs: calls.append(args)
    )
    pl = _make_pl(monkeypatch)

    pl.open_display_settings(False)

    assert calls == [["control", "/name", "Microsoft.Display", "/page", "Settings"]]


def test_setup_ui_starts_hotplug_thread_on_windows(qapp, monkeypatch):
    monkeypatch.setattr("DisplayCAL.ui.tools.apply_profiles.sys.platform", "win32")
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QSystemTrayIcon.isSystemTrayAvailable",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles._ScriptingHost",
        lambda pl: mock.Mock(),
    )
    started = {}

    class _FakeThread:
        def __init__(self, target=None, name=None):
            started["target"] = target
            started["name"] = name

        def start(self):
            started["started"] = True

    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.threading.Thread", _FakeThread
    )

    pl = _make_pl(monkeypatch)
    pl._skip = False

    pl._setup_ui()

    assert started == {
        "target": pl._check_display_conf_wrapper,
        "name": "DisplayConfigurationMonitoring",
        "started": True,
    }
    assert pl._pid == os.getpid()


def test_setup_ui_does_not_start_hotplug_thread_off_windows(qapp, monkeypatch):
    monkeypatch.setattr("DisplayCAL.ui.tools.apply_profiles.sys.platform", "linux")
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.QSystemTrayIcon.isSystemTrayAvailable",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles._ScriptingHost",
        lambda pl: mock.Mock(),
    )
    threads = []
    monkeypatch.setattr(
        "DisplayCAL.ui.tools.apply_profiles.threading.Thread",
        lambda *a, **k: threads.append((a, k)) or mock.Mock(),
    )

    pl = _make_pl(monkeypatch)
    pl._skip = False
    config.setcfg("profile_loader.macos_reapply_watch", 0)

    pl._setup_ui()

    assert threads == []
    assert not hasattr(pl, "_pid")
