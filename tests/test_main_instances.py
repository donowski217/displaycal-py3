"""Tests for how DisplayCAL.main deals with other instances and processes.

Nothing here starts, signals or kills a process.
"""

from __future__ import annotations

import ast
import faulthandler
import os
import pathlib
import socket
import subprocess
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


def test_launcher_does_not_give_the_loader_pipes():
    """The launcher stub exits after a few seconds.

    Pipes it handed to the loader would break, and every later print() in a
    non-frozen loader would then raise OSError.
    """
    tree = ast.parse(
        (SCRIPTS_DIR / "displaycal-apply-profiles-launcher").read_text(
            encoding="utf-8"
        )
    )
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert len(popen_calls) == 1
    pipe_kwargs = {
        keyword.arg
        for keyword in popen_calls[0].keywords
        if isinstance(keyword.value, ast.Attribute)
        and keyword.value.attr in ("PIPE", "STDOUT")
    }
    assert not pipe_kwargs


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


# Who listens on the port of an instance that does not reply in time?

PORT = 15411
OTHER_PORT = 49152
GUI_EXE = r"C:\Program Files\DisplayCAL\DisplayCAL.exe"


class FakeListener:
    """A process for the fake psutil of the `system` fixture.

    Args:
        pid: Process ID.
        name: Process name, or the exception that name() raises.
        cmdline: Command line, or the exception that cmdline() raises.
        ports: Ports it listens on, or the exception that its
            net_connections() raises.
        hidden: Its sockets are listed without PID by psutil.net_connections()
            (e.g. a process of another user under Linux).
        old_psutil: Only has connections() (psutil < 6.0).
    """

    def __init__(
        self,
        pid: int,
        name: str | Exception,
        cmdline: list[str] | Exception,
        ports: list[int] | Exception = (),
        hidden: bool = False,
        old_psutil: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self.ports = ports
        self.hidden = hidden
        if old_psutil:
            self.connections = self._get_connections
        else:
            self.net_connections = self._get_connections

    @staticmethod
    def _value(value):
        if isinstance(value, Exception):
            raise value
        return value

    def name(self) -> str:
        return self._value(self._name)

    def cmdline(self) -> list[str]:
        return self._value(self._cmdline)

    def _get_connections(self, kind: str = "inet") -> list:
        assert kind == "tcp"
        return _listening(self._value(self.ports))


def _listening(ports, pid=None) -> list:
    import psutil

    return [
        types.SimpleNamespace(
            pid=pid,
            status=psutil.CONN_LISTEN,
            laddr=types.SimpleNamespace(ip="127.0.0.1", port=port),
        )
        for port in ports
    ]


@pytest.fixture
def system(monkeypatch):
    """Fake psutil process and connection listings."""
    psutil = pytest.importorskip("psutil")
    env = types.SimpleNamespace(
        processes=[], gone=set(), scans=0, net_connections_error=None
    )

    def net_connections(kind: str = "inet") -> list:
        assert kind == "tcp"
        if env.net_connections_error:
            raise env.net_connections_error
        connections = []
        for process in env.processes:
            if isinstance(process.ports, Exception):
                continue
            pid = None if process.hidden else process.pid
            connections += _listening(process.ports, pid)
        return connections

    def process(pid: int) -> FakeListener:
        for candidate in env.processes:
            if candidate.pid == pid and pid not in env.gone:
                return candidate
        raise psutil.NoSuchProcess(pid)

    def process_iter():
        env.scans += 1
        return iter([p for p in env.processes if p.pid not in env.gone])

    monkeypatch.setattr(psutil, "net_connections", net_connections)
    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(psutil, "process_iter", process_iter)
    return env


def _gui(ports, pid=5555, **kwargs) -> FakeListener:
    return FakeListener(pid, "DisplayCAL.exe", [GUI_EXE], ports, **kwargs)


def _foreign(ports, pid=6666, **kwargs) -> FakeListener:
    return FakeListener(pid, "svchost.exe", ["svchost.exe", "-k"], ports, **kwargs)


def _python(cmdline, ports, pid=7777, **kwargs) -> FakeListener:
    return FakeListener(pid, "python.exe", cmdline, ports, **kwargs)


def test_port_of_our_starting_instance(main, system):
    system.processes += [_foreign([OTHER_PORT]), _gui([PORT])]
    assert main.is_port_of_running_instance(PORT) is True
    # The listener is known, so the command lines of all processes (which
    # takes seconds) are not read
    assert system.scans == 0


def test_port_of_our_instance_from_a_source_install(main, system):
    script = r"C:\DisplayCAL\scripts\displaycal-apply-profiles"
    system.processes.append(_python(["python.exe", script], [PORT]))
    assert main.is_port_of_running_instance(PORT) is True


def test_port_taken_over_by_a_foreign_service(main, system):
    """Our instance listens elsewhere; a foreign service has the old port."""
    system.processes += [_gui([OTHER_PORT]), _foreign([PORT])]
    assert main.is_port_of_running_instance(PORT) is False


def test_port_taken_over_by_another_python_program(main, system):
    system.processes.append(_python(["python.exe", "other_service.py"], [PORT]))
    assert main.is_port_of_running_instance(PORT) is False


def test_port_of_python_with_unreadable_command_line_counts(main, system):
    """It may be a source install of ours: keep waiting as before."""
    psutil = pytest.importorskip("psutil")
    system.processes.append(_python(psutil.AccessDenied(7777), [PORT]))
    assert main.is_port_of_running_instance(PORT) is True


def test_port_of_this_process_does_not_count(main, system):
    system.processes.append(_gui([PORT], pid=os.getpid()))
    assert main.is_port_of_running_instance(PORT) is False


def test_port_of_vanished_process_does_not_count(main, system):
    system.processes.append(_gui([PORT]))
    system.gone.add(5555)
    assert main.is_port_of_running_instance(PORT) is False


def test_port_nobody_listens_on_does_not_count(main, system):
    system.processes.append(_gui([OTHER_PORT]))
    assert main.is_port_of_running_instance(PORT) is False


def _hide_listeners(system, how: str) -> None:
    psutil = pytest.importorskip("psutil")
    if how == "no-permission":
        # psutil.net_connections() under macOS without root
        system.net_connections_error = psutil.AccessDenied()
    else:
        for process in system.processes:
            process.hidden = True


hidden_listeners = pytest.mark.parametrize("how", ["no-permission", "no-pid"])


@hidden_listeners
def test_hidden_port_of_our_instance(main, system, how):
    system.processes += [_foreign([OTHER_PORT]), _gui([PORT])]
    _hide_listeners(system, how)
    assert main.is_port_of_running_instance(PORT) is True
    assert system.scans == 1


@hidden_listeners
def test_hidden_port_of_our_instance_with_old_psutil(main, system, how):
    system.processes.append(_gui([PORT], old_psutil=True))
    _hide_listeners(system, how)
    assert main.is_port_of_running_instance(PORT) is True


@hidden_listeners
def test_hidden_port_taken_over_by_a_foreign_service(main, system, how):
    system.processes += [_gui([OTHER_PORT]), _foreign([PORT])]
    _hide_listeners(system, how)
    assert main.is_port_of_running_instance(PORT) is False
    assert system.scans == 1


@hidden_listeners
def test_hidden_port_of_uninspectable_instance_counts(main, system, how):
    """If a process of ours cannot be inspected, keep waiting as before."""
    psutil = pytest.importorskip("psutil")
    system.processes += [_gui(psutil.AccessDenied(5555)), _foreign([PORT])]
    _hide_listeners(system, how)
    assert main.is_port_of_running_instance(PORT) is True


def test_port_owner_without_psutil_counts(main, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert main.is_port_of_running_instance(PORT) is True


def test_port_owner_when_nothing_can_be_listed(main, system, monkeypatch):
    psutil = pytest.importorskip("psutil")

    def process_iter():
        raise psutil.AccessDenied()

    system.net_connections_error = psutil.AccessDenied()
    monkeypatch.setattr(psutil, "process_iter", process_iter)
    assert main.is_port_of_running_instance(PORT) is True


LISTENER_SCRIPT = """
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
sock.listen(1)
print(sock.getsockname()[1], flush=True)
sys.stdin.read()
"""


def test_port_owner_with_real_psutil(main):
    """The real psutil API: a child process "of ours" listening on loopback.

    Its command line contains the application name, like a DisplayCAL
    script run from a source install. The child only listens; nothing
    connects to it, and it exits when its stdin is closed.
    """
    pytest.importorskip("psutil")
    child = subprocess.Popen(
        [sys.executable, "-c", LISTENER_SCRIPT, "DisplayCAL-port-owner-test"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        port = int(child.stdout.readline())
        own = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            own.bind(("127.0.0.1", 0))
            own.listen(1)
            own_port = own.getsockname()[1]
            assert main.is_port_of_running_instance(port) is True
            # Only this (test) process listens on own_port
            assert main.is_port_of_running_instance(own_port) is False
        finally:
            own.close()
    finally:
        child.stdin.close()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(10)
        child.stdout.close()
