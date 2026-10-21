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
