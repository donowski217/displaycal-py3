"""Tests for how DisplayCAL.main deals with other instances and processes.

Nothing here starts, signals or kills a process.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import types

import pytest


@pytest.fixture(scope="module")
def main():
    """Import DisplayCAL.main without keeping its sys.excepthook."""
    saved_hook = sys.excepthook
    try:
        from DisplayCAL import main as main_module
    finally:
        sys.excepthook = saved_hook
    return main_module


# A second instance must not wipe the crash dump of a running instance


PREVIOUS_DUMP = "Windows fatal exception: access violation\n"


def test_initialize_fault_handler_appends(main, monkeypatch, tmp_path):
    """Every start appends a header instead of truncating the fault log.

    initialize_fault_handler() runs before the single-instance check, so a
    second start (e.g. the logon launcher) used to truncate the dump of a
    running instance, and every start the dump of a previous crash.
    """
    enabled = []
    registered = []
    monkeypatch.setattr(main, "LOGDIR", str(tmp_path))
    monkeypatch.setattr(main, "PYNAME", "DisplayCAL-apply-profiles")
    # Never enable faulthandler for the test process itself
    monkeypatch.setattr(
        faulthandler,
        "enable",
        lambda file, all_threads=True: enabled.append((file, all_threads)),
    )
    monkeypatch.setattr(
        main,
        "atexit",
        types.SimpleNamespace(register=lambda fn, *args: registered.append((fn, args))),
    )
    path = tmp_path / "DisplayCAL-apply-profiles-fault.log"
    path.write_text(PREVIOUS_DUMP, encoding="utf-8")

    try:
        main.initialize_fault_handler()
        main.initialize_fault_handler()
        for fn, args in registered:
            fn(*args)
    finally:
        for file, _ in enabled:
            file.close()

    assert len(enabled) == 2
    assert all(all_threads for _, all_threads in enabled)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == PREVIOUS_DUMP.strip()
    pid = os.getpid()
    assert [line.split(" PID ")[1] for line in lines[1:]] == [
        f"{pid} started ===",
        f"{pid} started ===",
        f"{pid} exited ===",
        f"{pid} exited ===",
    ]
    assert all("DisplayCAL-apply-profiles" in line for line in lines[1:])
