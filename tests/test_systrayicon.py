"""Tests for the SysTrayIcon window procedure (pywin32 message map) hardening.

Uses a message-only window (HWND_MESSAGE parent): it is never shown, never
receives broadcasts, and no tray icon is added (SetIcon is never called).
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

    from DisplayCAL import systrayicon


@pytest.fixture
def make_tray_icon(monkeypatch):
    """Create SysTrayIcon (sub)class instances on message-only windows."""
    create_window = win32gui.CreateWindow

    def create_message_only_window(*args):
        args = list(args)
        args[7] = win32con.HWND_MESSAGE  # parent
        return create_window(*args)

    monkeypatch.setattr(
        systrayicon.win32gui, "CreateWindow", create_message_only_window
    )
    excepthook_calls = []
    monkeypatch.setattr(
        sys, "excepthook", lambda *exc_info: excepthook_calls.append(exc_info)
    )
    icons = []

    def make(cls=systrayicon.SysTrayIcon):
        icon = cls()
        icons.append(icon)
        return icon

    make.excepthook_calls = excepthook_calls
    yield make
    hinstance = win32api.GetModuleHandle(None)
    for icon in icons:
        win32gui.DestroyWindow(icon.hwnd)
    # OnDestroy posts WM_QUIT when no wx main loop is running: drain it
    while win32gui.PeekMessage(None, 0, 0, win32con.PM_REMOVE)[0]:
        pass
    win32gui.UnregisterClass("SysTrayIcon", hinstance)


def _taskbar_created():
    return win32gui.RegisterWindowMessage("TaskbarCreated")


def test_handler_returning_none_is_tolerated(make_tray_icon, capsys):
    """The <= 3.9.17 OnTaskbarCreated had no return statement."""

    class OldTrayIcon(systrayicon.SysTrayIcon):
        def OnTaskbarCreated(self, hwnd, msg, wparam, lparam):  # noqa: N802
            return None

    icon = make_tray_icon(OldTrayIcon)

    assert win32gui.SendMessage(icon.hwnd, _taskbar_created(), 0, 0) == 0
    assert "cannot be converted to LRESULT" not in capsys.readouterr().err
    assert make_tray_icon.excepthook_calls == []


def test_handler_exception_is_logged_not_passed_to_pywin32(make_tray_icon, capsys):
    class BrokenTrayIcon(systrayicon.SysTrayIcon):
        def OnTaskbarNotify(self, hwnd, msg, wparam, lparam):  # noqa: N802
            raise RuntimeError("boom in OnTaskbarNotify")

    icon = make_tray_icon(BrokenTrayIcon)

    result = win32gui.SendMessage(
        icon.hwnd, win32con.WM_USER + 20, 0, win32con.WM_MOUSEMOVE
    )

    assert result == 0
    err = capsys.readouterr().err
    assert "WNDPROC handler failed" not in err
    assert "RuntimeError: boom in OnTaskbarNotify" in err
    assert make_tray_icon.excepthook_calls == []


def test_regular_handlers_still_return_their_values(make_tray_icon, capsys):
    icon = make_tray_icon()

    assert win32gui.SendMessage(icon.hwnd, _taskbar_created(), 0, 0) == 1
    assert (
        win32gui.SendMessage(icon.hwnd, win32con.WM_USER + 20, 0, win32con.WM_MOUSEMOVE)
        == 1
    )
    assert win32gui.SendMessage(icon.hwnd, win32con.WM_COMMAND, 1, 0) == 0
    assert "WNDPROC" not in capsys.readouterr().err
    assert make_tray_icon.excepthook_calls == []


def test_handler_exception_is_one_app_log_record(monkeypatch):
    """In a windowed build, not line by line through stderr (the app log)."""
    from DisplayCAL import log

    class LogStream:
        is_log_stream = True

        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(text)
            return len(text)

        def flush(self):
            pass

    stream = LogStream()
    records = []
    monkeypatch.setattr(log, "log_error", lambda text: records.append(text) or True)

    def handler(hwnd, msg, wparam, lparam):
        raise RuntimeError("boom in handler")

    # Not via monkeypatch in a fixture: pytest's output capturing installs its
    # own sys.stderr when the test call starts
    saved_stderr = sys.stderr
    sys.stderr = stream
    try:
        result = systrayicon._safe_wndproc(handler)(0, 0, 0, 0)
    finally:
        sys.stderr = saved_stderr

    assert result == 0
    assert len(records) == 1
    assert "RuntimeError: boom in handler" in records[0]
    assert stream.writes == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [(None, 0), (0, 0), (1, 1), (True, 1), (False, 0), (-1, -1), ("x", 0)],
)
def test_safe_wndproc_result_conversion(result, expected):
    wndproc = systrayicon._safe_wndproc(lambda hwnd, msg, wparam, lparam: result)

    assert wndproc(0, 0, 0, 0) == expected
