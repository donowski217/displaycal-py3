"""Tests for how DisplayCAL.main deals with other instances and processes.

Nothing here starts, signals or kills a process.
"""

from __future__ import annotations

import ast
import faulthandler
import os
import pathlib
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


# Frozen multiprocessing children must not import DisplayCAL before
# freeze_support() takes over

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
ENTRY_SCRIPTS = sorted(
    path.name
    for path in SCRIPTS_DIR.glob("displaycal*")
    # The launcher never imports DisplayCAL; it only starts the loader
    if path.name != "displaycal-apply-profiles-launcher"
)


def _imports_displaycal(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Import) and any(
            alias.name.split(".")[0] == "DisplayCAL" for alias in child.names
        ):
            return True
        if (
            isinstance(child, ast.ImportFrom)
            and child.module
            and child.module.split(".")[0] == "DisplayCAL"
        ):
            return True
    return False


def _is_freeze_support_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    func = node.value.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name == "freeze_support"


def test_entry_scripts_were_found():
    assert "displaycal-apply-profiles" in ENTRY_SCRIPTS
    assert len(ENTRY_SCRIPTS) >= 10


@pytest.mark.parametrize("script", ENTRY_SCRIPTS)
def test_entry_script_calls_freeze_support_before_importing_displaycal(script):
    tree = ast.parse((SCRIPTS_DIR / script).read_text(encoding="utf-8"))
    for node in tree.body:
        if _is_freeze_support_call(node):
            break
        assert not _imports_displaycal(node), (
            f"{script} imports DisplayCAL before calling freeze_support()"
        )
    else:
        pytest.fail(f"{script} does not call freeze_support()")


# The WCS worker child (same exe name) is not another instance

LOADER_EXE = "DisplayCAL-apply-profiles.exe"
LOADER_PID = 1111
WORKER_PID = 2222
GONE_PID = 3333
PROTECTED_PID = 4444
CMDLINES = {
    LOADER_PID: [rf"C:\Program Files\DisplayCAL\{LOADER_EXE}", "--task"],
    WORKER_PID: [
        rf"C:\Program Files\DisplayCAL\{LOADER_EXE}",
        "--multiprocessing-fork",
        f"parent_pid={LOADER_PID}",
        "pipe_handle=568",
    ],
}


@pytest.fixture
def fake_processes(monkeypatch):
    """Fake process list and psutil; record (never run) taskkill calls."""
    psutil = pytest.importorskip("psutil")

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def cmdline(self) -> list[str]:
            if self.pid == GONE_PID:
                raise psutil.NoSuchProcess(self.pid)
            if self.pid == PROTECTED_PID:
                raise psutil.AccessDenied(self.pid)
            return CMDLINES[self.pid]

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    env = types.SimpleNamespace(processes=[], terminated=[])
    session_id = 1
    monkeypatch.setitem(
        sys.modules,
        "win32ts",
        types.SimpleNamespace(
            ProcessIdToSessionId=lambda pid: session_id,
            WTSEnumerateProcesses=lambda: [
                (session_id, pid, LOADER_EXE, None) for pid in env.processes
            ],
        ),
    )
    return env


@pytest.fixture
def instance_env(main, fake_processes, monkeypatch, tmp_path):
    def terminate_running_instance(incoming, pid, startupinfo):
        fake_processes.terminated.append(pid)
        # A graceful taskkill cannot close the windowless worker
        return incoming if pid == WORKER_PID else "ok"

    monkeypatch.setattr(main, "terminate_running_instance", terminate_running_instance)
    monkeypatch.setattr(main, "EXE_EXT", ".exe")
    lockfilename = tmp_path / "DisplayCAL-apply-profiles.lock"
    lockfilename.write_text("")
    fake_processes.lockfilename = str(lockfilename)
    fake_processes.lock = types.SimpleNamespace(unlock=lambda: None)
    return fake_processes


def _terminate_conflicting_instance(main, env):
    return main.terminate_conflicting_instance(
        "apply-profiles", env.lockfilename, env.lock, None, None
    )


windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="terminate_conflicting_instance is Windows-only"
)


@windows_only
@pytest.mark.parametrize(
    "processes", [[LOADER_PID, WORKER_PID], [WORKER_PID, LOADER_PID]]
)
def test_multiprocessing_child_is_not_another_instance(main, instance_env, processes):
    """The loader's WCS worker must not make the new loader refuse to start.

    With the worker listed after the loader, the worker's failed taskkill used
    to override the loader's successful one: the new loader closed the old
    one and then refused to start ("Not starting another instance.").
    """
    instance_env.processes = processes

    assert _terminate_conflicting_instance(main, instance_env) == "ok"
    assert instance_env.terminated == [LOADER_PID]


@windows_only
def test_orphaned_worker_alone_does_not_block_start(main, instance_env):
    instance_env.processes = [WORKER_PID]

    assert _terminate_conflicting_instance(main, instance_env) is None
    assert instance_env.terminated == []


@windows_only
def test_process_that_exited_meanwhile_is_ignored(main, instance_env):
    instance_env.processes = [GONE_PID]

    assert _terminate_conflicting_instance(main, instance_env) is None
    assert instance_env.terminated == []


@windows_only
def test_process_that_cannot_be_inspected_is_still_an_instance(main, instance_env):
    instance_env.processes = [PROTECTED_PID]

    assert _terminate_conflicting_instance(main, instance_env) == "ok"
    assert instance_env.terminated == [PROTECTED_PID]


def test_is_other_instance_process(main, fake_processes):
    assert main.is_other_instance_process(LOADER_PID) is True
    assert main.is_other_instance_process(WORKER_PID) is False
    assert main.is_other_instance_process(GONE_PID) is False
    assert main.is_other_instance_process(PROTECTED_PID) is True
