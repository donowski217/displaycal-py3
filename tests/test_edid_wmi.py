"""Tests for the COM lifetime handling of the WMI EDID lookup on Windows.

COM objects (the WMI connection and the monitor objects) must all be released
before CoUninitialize() is called on the thread, and every CoInitialize() must
be balanced, even when WMI fails. pythoncom and wmi are replaced by fakes, so
no real WMI query is made.
"""

import gc
import sys
import threading
import weakref

import pytest

from DisplayCAL import edid

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

RAW_EDID = bytes(range(128))


class FakeDevice:
    DeviceID = r"MONITOR\DEL40E0\{4d36e96e-e325-11ce-bfc1-08002be10318}\0001"


class FakeMonitor:
    def __init__(self, instance_name):
        self.InstanceName = instance_name

    def WmiGetMonitorRawEEdidV1Block(self, block):  # noqa: N802
        return tuple(RAW_EDID), 0


class FakeConnection:
    def __init__(self, com, fail_query=False):
        self.com = com
        self.fail_query = fail_query
        com.track(self)

    def WmiMonitorDescriptorMethods(self):  # noqa: N802
        if self.fail_query:
            raise RuntimeError("WMI query failed")
        monitors = [
            FakeMonitor(r"DISPLAY\OTH1234\5&1&UID256_0"),
            FakeMonitor(r"DISPLAY\DEL40E0\5&2&UID257_0"),
        ]
        for monitor in monitors:
            self.com.track(monitor)
        return monitors


class FakeCOM:
    """Fake pythoncom + wmi recording the COM apartment and object lifetimes."""

    def __init__(self, fail_connect=False, fail_query=False):
        self.fail_connect = fail_connect
        self.fail_query = fail_query
        self.calls = []
        self.refs = []
        self.alive_at_uninitialize = None

    def track(self, obj):
        self.refs.append(weakref.ref(obj))

    # pythoncom
    def CoInitialize(self):  # noqa: N802
        self.calls.append("CoInitialize")

    def CoUninitialize(self):  # noqa: N802
        self.calls.append("CoUninitialize")
        self.alive_at_uninitialize = sum(ref() is not None for ref in self.refs)

    # wmi
    def WMI(self, namespace=None):  # noqa: N802
        if self.fail_connect:
            raise RuntimeError("x_wmi: could not connect")
        return FakeConnection(self, self.fail_query)


@pytest.fixture
def fake_com(monkeypatch):
    def install(**kwargs):
        com = FakeCOM(**kwargs)
        monkeypatch.setattr(edid, "pythoncom", com, raising=False)
        monkeypatch.setattr(edid, "wmi", com, raising=False)
        return com

    return install


def _get_edid_in_worker_thread():
    """Call get_edid_windows like the monitoring thread does (not main thread)."""
    outcome = {}

    def target():
        try:
            outcome["result"] = edid.get_edid_windows(0, FakeDevice())
        except Exception as exception:
            outcome["error"] = exception
        finally:
            # Clear the traceback reference cycle like a finished frame would
            gc.collect()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    return outcome


def test_wmi_objects_are_released_before_couninitialize(fake_com):
    com = fake_com()

    outcome = _get_edid_in_worker_thread()

    assert outcome == {"result": RAW_EDID}
    assert com.calls == ["CoInitialize", "CoUninitialize"]
    assert com.alive_at_uninitialize == 0


def test_couninitialize_balances_coinitialize_when_wmi_connect_fails(fake_com):
    com = fake_com(fail_connect=True)

    outcome = _get_edid_in_worker_thread()

    assert isinstance(outcome.get("error"), RuntimeError)
    assert com.calls == ["CoInitialize", "CoUninitialize"]


def test_wmi_query_error_releases_objects_before_couninitialize(fake_com):
    com = fake_com(fail_query=True)

    outcome = _get_edid_in_worker_thread()

    assert isinstance(outcome.get("error"), edid.WMIError)
    assert com.calls == ["CoInitialize", "CoUninitialize"]
    assert com.alive_at_uninitialize == 0


def test_main_thread_does_not_touch_the_com_apartment(fake_com):
    com = fake_com()

    assert edid.get_edid_windows(0, FakeDevice()) == RAW_EDID
    assert com.calls == []
