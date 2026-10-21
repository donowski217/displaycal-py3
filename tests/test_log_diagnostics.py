"""Tests for the diagnostics side of DisplayCAL.log.

These tests never call setup_logging(): it would attach handlers to the root
logger (interfering with pytest's own logging capture and keeping files open,
which breaks tmp_path cleanup on Windows). Instead, log.LOGGER is replaced by a
private logger with a UTF-8 file handler for the duration of each test.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from DisplayCAL import log


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


@pytest.fixture
def app_log(tmp_path, monkeypatch):
    """Use a private logger with a UTF-8 file handler as the app logger."""
    logger = logging.getLogger(f"test_log_diagnostics.app.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    path = tmp_path / "app.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    monkeypatch.setattr(log, "LOGGER", logger)
    monkeypatch.setattr(log, "_LOGDIR", str(tmp_path))
    yield logger, path
    _close_logger(logger)


@pytest.fixture
def file_loggers():
    """Collect loggers created by get_file_logger() and close them afterwards."""
    loggers = []
    yield loggers
    for logger in loggers:
        _close_logger(logger)


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# get_file_logger() must not replace the application logger


def test_session_logfile_does_not_hijack_app_log(app_log, tmp_path, file_loggers):
    """LOG() keeps writing to the app log while and after a session log exists."""
    app_logger, app_log_path = app_log
    log.LOG("line before session")
    session = log.LogFile(_unique_name("MyProfile"), str(tmp_path / "session"))
    file_loggers.append(session._logger)
    try:
        assert log.LOGGER is app_logger
        log.LOG("line during session")
        session.write("session only line")
    finally:
        session.close()
    log.LOG("line after session")

    assert log.LOGGER is app_logger
    app_text = app_log_path.read_text(encoding="utf-8")
    assert "line before session" in app_text
    assert "line during session" in app_text
    assert "line after session" in app_text
    assert "session only line" not in app_text
    session_text = (
        tmp_path / "session" / f"{session.filename}.log"
    ).read_text(encoding="utf-8")
    assert "session only line" in session_text
    assert "line during session" not in session_text


def test_named_file_logger_does_not_hijack_app_log(app_log, tmp_path, file_loggers):
    """get_file_logger('interact') etc. returns its logger without rebinding LOGGER."""
    app_logger, app_log_path = app_log
    name = _unique_name("interact")
    interact = log.get_file_logger(name)
    file_loggers.append(interact)

    assert interact is logging.getLogger(name)
    assert log.LOGGER is app_logger
    log.LOG("app line")
    interact.info("interact line")
    assert "app line" in app_log_path.read_text(encoding="utf-8")
    interact_text = (tmp_path / f"{name}.log").read_text(encoding="utf-8")
    assert "interact line" in interact_text
    assert "app line" not in interact_text
