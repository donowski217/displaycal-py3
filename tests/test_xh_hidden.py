"""Tests for the ``<hidden>`` handling of DisplayCAL's custom XRC handlers.

wxWidgets 3.x applies ``<hidden>1</hidden>`` only inside the C++
``XRC_MAKE_INSTANCE`` macro, which Python ``XmlResourceHandler`` subclasses
cannot use, so every ``DisplayCAL/xh_*.py`` handler has to hide the control
itself. That is expected behaviour, not an error, so it must not log the
"<name> should have been hidden" line that used to appear at every GUI start.

No window is ever shown: the controls are created inside a frame that is
never ``Show()``-n and is destroyed at the end of the test.
"""

import pytest

wx = pytest.importorskip("wx")
xrc = pytest.importorskip("wx.xrc")

from DisplayCAL import (
    log,
    xh_bitmapctrls,
    xh_fancytext,
    xh_filebrowsebutton,
    xh_floatspin,
    xh_hstretchstatbmp,
)

HIDDEN_CONTROLS = {
    "hidden_bitmapbutton": "wxBitmapButton",
    "hidden_staticbitmap": "wxStaticBitmap",
    "hidden_fancytext": "StaticFancyText",
    "hidden_filebrowsebutton": "FileBrowseButton",
    "hidden_filebrowsebutton_history": "FileBrowseButtonWithHistory",
    "hidden_floatspin": "FloatSpin",
    "hidden_hstretchstatbmp": "HStretchStaticBitmap",
}

# Same classes without <hidden>; these must stay shown.
SHOWN_CONTROLS = {
    name.replace("hidden_", "shown_"): cls for name, cls in HIDDEN_CONTROLS.items()
}


def _object_xml(name: str, cls: str, hidden: bool) -> str:
    """Return the XRC ``sizeritem`` for one control.

    Args:
        name (str): The XRC object name.
        cls (str): The XRC class name.
        hidden (bool): Whether to add ``<hidden>1</hidden>``.

    Returns:
        str: The XML snippet.
    """
    params = ""
    if cls in ("wxBitmapButton", "wxStaticBitmap"):
        # Read via the patched get_bitmap. HStretchStaticBitmap gets no
        # <bitmap>, so wx never tries to load a missing file.
        params += "<bitmap>dummy.png</bitmap>"
    elif cls == "StaticFancyText":
        # An empty label renders to a 0x0 bitmap, which wx asserts on.
        params += "<label>text</label>"
    if hidden:
        params += "<hidden>1</hidden>"
    return (
        '<object class="sizeritem">'
        f'<object class="{cls}" name="{name}">{params}</object>'
        "</object>"
    )


def _build_xrc() -> bytes:
    """Return an XRC document with every custom-handled class, hidden and shown.

    Returns:
        bytes: The XRC document.
    """
    items = [_object_xml(name, cls, True) for name, cls in HIDDEN_CONTROLS.items()]
    items += [_object_xml(name, cls, False) for name, cls in SHOWN_CONTROLS.items()]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<resource version="2.5.3.0" xmlns="http://www.wxwidgets.org/wxxrc">'
        '<object class="wxPanel" name="xh_hidden_panel">'
        '<object class="wxBoxSizer"><orient>wxVERTICAL</orient>'
        + "".join(items)
        + "</object></object></resource>"
    ).encode("utf-8")


@pytest.fixture
def wx_app():
    """Provide a wx.App with wx log output routed to stderr (never a dialog).

    Yields:
        wx.App: The application object.
    """
    app = wx.GetApp() or wx.App()
    # An XRC error would otherwise pop up a modal wxLogError dialog.
    old_target = wx.Log.SetActiveTarget(wx.LogStderr())
    try:
        yield app
    finally:
        wx.Log.SetActiveTarget(old_target)


@pytest.fixture
def logged(monkeypatch):
    """Record everything written through DisplayCAL's ``log.safe_print``.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        list: The recorded messages (one string per call).
    """
    messages = []

    def write(*args, **kwargs):
        messages.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(log.safe_print, "write", write)
    return messages


def _load_panel(frame):
    """Load the test panel into ``frame`` through DisplayCAL's handlers.

    Args:
        frame (wx.Frame): The (never shown) parent frame.

    Returns:
        tuple: ``(res, panel)``. The caller must delete ``res`` before the
            wx.App goes away (an XmlResource holding Python handlers that
            outlives the app crashes the interpreter at exit).
    """
    res = xrc.XmlResource()
    res.InsertHandler(xh_bitmapctrls.BitmapButton())
    res.InsertHandler(xh_bitmapctrls.StaticBitmap())
    res.InsertHandler(xh_fancytext.StaticFancyTextCtrlXmlHandler())
    res.InsertHandler(xh_filebrowsebutton.FileBrowseButtonXmlHandler())
    res.InsertHandler(xh_filebrowsebutton.FileBrowseButtonWithHistoryXmlHandler())
    res.InsertHandler(xh_floatspin.FloatSpinCtrlXmlHandler())
    res.InsertHandler(xh_hstretchstatbmp.HStretchStaticBitmapXmlHandler())
    assert res.LoadFromBuffer(_build_xrc())
    panel = res.LoadPanel(frame, "xh_hidden_panel")
    assert panel, "XRC panel failed to load"
    return res, panel


def test_custom_xrc_handlers_hide_without_logging(wx_app, logged, monkeypatch):
    """Hidden controls end up hidden, and nothing is logged about it."""
    monkeypatch.setattr(xh_bitmapctrls, "get_bitmap", lambda name: wx.Bitmap(16, 16))
    frame = wx.Frame(None)  # never shown
    res = None
    try:
        res, panel = _load_panel(frame)
        for name, cls in HIDDEN_CONTROLS.items():
            ctrl = wx.FindWindowByName(name, panel)
            assert ctrl is not None, f"{cls} {name} was not created"
            assert not ctrl.IsShown(), f"{cls} {name} should be hidden"
        for name, cls in SHOWN_CONTROLS.items():
            ctrl = wx.FindWindowByName(name, panel)
            assert ctrl is not None, f"{cls} {name} was not created"
            assert ctrl.IsShown(), f"{cls} {name} should not be hidden"
    finally:
        frame.Destroy()
        del res
    noise = [msg for msg in logged if "should have been hidden" in msg]
    assert noise == []
