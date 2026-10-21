"""Logging utilities for file-based, rotating, and safe Unicode logging."""

from __future__ import annotations

import atexit
import contextlib
import logging
import logging.handlers
import os
import re
import sys
import threading
import warnings
import weakref
from codecs import EncodedFile
from hashlib import md5
from io import BytesIO
from time import localtime, strftime, time
from typing import TYPE_CHECKING, Callable, TextIO

from DisplayCAL.meta import NAME as APPNAME
from DisplayCAL.meta import script2pywname
from DisplayCAL.multiprocess import mp
from DisplayCAL.options import DEBUG
from DisplayCAL.safe_print import SafePrinter
from DisplayCAL.safe_print import safe_print as _safe_print
from DisplayCAL.util_os import safe_glob

if TYPE_CHECKING:
    import wx  # noqa: TC004

logging.raiseExceptions = 0
logging._warnings_showwarning = warnings.showwarning


LOGLEVEL = logging.DEBUG if DEBUG else logging.INFO


LOGGER = None
_LOGDIR = None


def showwarning(
    message: Warning,
    category: Warning,
    filename: str,
    lineno: int,
    file: None | TextIO = None,
    line: str = "",
) -> None:
    # Adapted from _showwarning in Python2.7/lib/logging/__init__.py
    """Implementation of `showwarnings` which redirects to logging.

    It will first check to see if the file parameter is None. If a file is
    specified, it will delegate to the original warnings implementation of
    showwarning. Otherwise, it will call warnings.formatwarning and will log
    the resulting string to a warnings logger named "py.warnings" with level
    logging.WARNING.

    Unlike the default implementation, the line is omitted from the warning,
    and the warning does not end with a newline.

    Args:
        message (Warning): The warning message.
        category (Warning): The warning category.
        filename (str): The filename where the warning occurred.
        lineno (int): The line number where the warning occurred.
        file (None | TextIO, optional): The file to write the warning to.
        line (str, optional): The line of code that caused the warning.
    """
    if file is not None:
        if logging._warnings_showwarning is not None:
            logging._warnings_showwarning(
                message, category, filename, lineno, file, line
            )
    else:
        s = warnings.formatwarning(message, category, filename, lineno, line)
        logger = logging.getLogger("py.warnings")
        if not logger.handlers:
            if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
                handler = logging.StreamHandler()  # Logs to stderr by default
            else:
                handler = logging.NullHandler()
            logger.addHandler(handler)
        LOG(s.strip(), fn=logger.warning)


warnings.showwarning = showwarning

LOGBUFFER = EncodedFile(BytesIO(), "UTF-8", errors="replace")


def wx_log(logwindow: wx.Window, msg: str) -> None:
    """Log a message to the wxPython log window.

    Args:
        logwindow (wx.Window): The wxPython log window to log to.
        msg (str): The message to log.
    """
    if logwindow.IsShownOnScreen() and LOGBUFFER.tell():
        # Check if log buffer has been emptied or not.
        # If it has, our log message is already included.
        logwindow.Log(msg)


class DummyLogger:
    """Dummy logger class.

    This is used when logging is disabled or not available.
    """

    def critical(self, msg: str, *args, **kwargs) -> None:
        """Log a critical message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def debug(self, msg: str, *args, **kwargs) -> None:
        """Log a debug message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def error(self, msg: str, *args, **kwargs) -> None:
        """Log an error message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def exception(self, msg: str, *args, **kwargs) -> None:
        """Log an exception message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def info(self, msg: str, *args, **kwargs) -> None:
        """Log an info message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def log(self, level: int, msg: str, *args, **kwargs) -> None:
        """Log a message with the specified level.

        Args:
            level (int): The logging level (e.g., logging.INFO).
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """

    def warning(self, msg: str, *args, **kwargs) -> None:
        """Log a warning message.

        Args:
            msg (str): The message to log.
            *args: Additional arguments to format the message.
            **kwargs: Additional keyword arguments for logging.
        """


class Log:
    """Log class.

    This is a wrapper around the logging module.
    """

    def __call__(self, msg: str, fn: None | Callable = None) -> None:
        """Log a message.

        Optionally use function 'fn' instead of logging.info.

        Args:
            msg (str): The message to log.
            fn (None | Callable, optional): Function to use for logging. If None,
                it will use logging.info if available, otherwise it will
                default to a dummy logger.
        """
        global LOGGER
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")

        msg = msg.replace("\r\n", "\n").replace("\r", "")
        if fn is None and LOGGER and LOGGER.handlers:
            fn = LOGGER.info
        if fn:
            for line in msg.split("\n"):
                fn(line)
        # If wxPython itself calls warnings.warn on import, it is not yet fully
        # imported at the point our showwarning() function calls log().
        # Check for presence of our wx_fixes module and if it has an attribute
        # "wx", in which case wxPython has finished importing.
        wx_fixes = sys.modules.get(f"{APPNAME}.wx_fixes")
        # wx_fixes = sys.modules.get("wx_fixes")
        if (
            wx_fixes
            and hasattr(wx_fixes, "wx")
            and mp.current_process().name == "MainProcess"
        ):
            wx = wx_fixes.wx
            if (
                wx.GetApp() is not None
                and hasattr(wx.GetApp(), "frame")
                and hasattr(wx.GetApp().frame, "infoframe")
            ):
                wx.CallAfter(wx_log, wx.GetApp().frame.infoframe, msg)

    def flush(self) -> None:
        """Flush the log."""

    def write(self, msg: str) -> None:
        """Write a message to the log.

        Args:
            msg (str): The message to write to the log.
        """
        self(msg.rstrip())


LOG = Log()


class LogFile:
    """Logfile class. Default is to not rotate.

    Args:
        filename (str): The name of the log file.
        logdir (str): The directory where the log file will be stored.
        when (str, optional): When to rotate the log file. Defaults to "never".
        backup_count (int, optional): Number of backup files to keep. Defaults
            to 0
    """

    def __init__(
        self,
        filename: str,
        logdir: str,
        when: str = "never",
        backup_count: int = 0,
    ) -> None:
        self.filename = filename
        self._logger = get_file_logger(
            md5(filename.encode()).hexdigest(),  # noqa: S324
            when=when,
            backup_count=backup_count,
            logdir=logdir,
            filename=filename,
        )

    def close(self) -> None:
        """Close the log file."""
        for handler in reversed(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)

    def flush(self) -> None:
        """Flush the log file."""
        for handler in self._logger.handlers:
            handler.flush()

    def write(self, msg: str) -> None:
        """Write a message to the log file.

        Args:
            msg (str): The message to write to the log file.
        """
        for line in msg.rstrip().replace("\r\n", "\n").replace("\r", "").split("\n"):
            self._logger.info(line)


class SafeLogger(SafePrinter):
    """Safely print and log, avoiding Unicode errors.

    Args:
        log (bool, optional): Whether to log the messages. Defaults to True.
        print_ (None | bool, optional): Whether to print the messages. If None,
            it will check if sys.stdout is a TTY.
    """

    def __init__(self, log: bool = True, print_: None | bool = None) -> None:
        SafePrinter.__init__(self)
        self.log = log
        if print_ is None:
            print_ = (
                sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
            )
        self.print_ = print_

    def write(self, *args, **kwargs) -> None:
        """Write the given arguments to the stream, formatted and encoded."""
        if kwargs.get("print_", self.print_):
            _safe_print(*args, **kwargs)
        if kwargs.get("log", self.log):
            kwargs.update(fn=LOG, encoding=None)
            _safe_print(*args, **kwargs)


safe_log = SafeLogger(print_=False)
safe_print = SafeLogger()


def get_file_logger(
    name: str,
    level: int = LOGLEVEL,
    when: str = "midnight",
    backup_count: int = 5,
    logdir: None | str = None,
    filename: None | str = None,
    confighome: None | str = None,
) -> logging.Logger:
    """Return logger object.

    A TimedRotatingFileHandler or FileHandler (if when == "never") will be used.

    Args:
        name (str): Name of the logger.
        level (int, optional): Logging level. Defaults to LOGLEVEL.
        when (str, optional): When to rotate the log file. Defaults to
            "midnight".
        backup_count (int, optional): Number of backup files to keep. Defaults
            to 5
        logdir (str, optional): Directory where log files will be stored.
        filename (None | str, optional): Name of the log file. If None, it will
            use the name parameter.
        confighome (None | str, optional): Configuration home directory, used
            for unique log file names. If None, it will not use a unique name.

    Returns:
        logging.Logger: The logger object.
    """
    # Only setup_logging() sets the application logger (LOGGER). Session,
    # interact, adjust etc. loggers must not replace it, otherwise LOG() and
    # safe_print() would write to e.g. a profile's session log, and drop
    # messages once that log is closed.
    if logdir is None:
        logdir = _LOGDIR
    logger = logging.getLogger(name)
    if not filename:
        filename = name
    mode = "a"
    if confighome:
        when, filename, mode = update_filename_for_confighome(
            when,
            logdir,
            filename,
            confighome,
            mode,
        )
    logfile = os.path.join(logdir, filename + ".log")
    for handler in logger.handlers:
        if isinstance(
            handler, logging.FileHandler
        ) and handler.baseFilename == os.path.abspath(logfile):
            return logger
    logger.propagate = 0
    logger.setLevel(level)
    if not os.path.exists(logdir):
        try:
            os.makedirs(logdir)
        except Exception as exception:
            print(
                f"Warning - log directory '{logdir}' could not be created: {exception}"
            )
    elif when != "never" and os.path.exists(logfile):
        rotate_log_files(backup_count, logdir, logfile)
    if os.path.exists(logdir):
        # Always write UTF-8. With the default (locale) encoding, e.g. cp1252,
        # any record containing a character outside that code page (box
        # drawing characters, arrows, non-Latin display or profile names...)
        # would raise in emit() and be dropped silently, because
        # logging.raiseExceptions is off.
        try:
            if when != "never":
                filehandler = logging.handlers.TimedRotatingFileHandler(
                    logfile,
                    when=when,
                    backupCount=backup_count,
                    encoding="utf-8",
                    errors="backslashreplace",
                )
            else:
                filehandler = logging.FileHandler(
                    logfile, mode, encoding="utf-8", errors="backslashreplace"
                )
            fileformatter = logging.Formatter("%(asctime)s %(message)s")
            filehandler.setFormatter(fileformatter)
            logger.addHandler(filehandler)
        except Exception as exception:
            print(f"Warning - logging to file '{logfile}' not possible: {exception}")
    return logger


def is_multiprocessing_child() -> bool:
    """Return whether this process is a multiprocessing child process.

    The process name alone is not enough: in a frozen Windows build, a
    spawned child runs the entry script, which may import DisplayCAL and set
    up logging, before multiprocessing.freeze_support() names the process.
    The "--multiprocessing-fork" argument which the spawn start method passes
    to the child is there from the start. (It is replaced by the parent's
    arguments once the child is prepared, so check this early, e.g. while
    setting up logging, and do not cache the result.)

    Returns:
        bool: True if this is a multiprocessing child process.
    """
    return mp.current_process().name != "MainProcess" or (
        "--multiprocessing-fork" in (getattr(sys, "argv", None) or [])
    )


def update_filename_for_confighome(
    when: str,
    logdir: str,
    filename: str,
    confighome: str,
    mode: str,
) -> None:
    """Update the filename for the log file based on the configuration home.

    Args:
        when (str): When to rotate the log file.
        logdir (str): Directory where log files will be stored.
        filename (str): Name of the log file.
        confighome (str): Configuration home directory, used for unique log
            file names.
        mode (str): Mode for opening the log file.

    Returns:
        tuple[str, str, str]: A tuple containing the updated 'when',
            'filename', and 'mode'.
    """
    # Use different logfile name (append number) for each additional instance
    is_main_process = not is_multiprocessing_child()
    if os.path.basename(confighome).lower() == "dispcalgui":
        lockbasename = filename.replace(APPNAME, "dispcalGUI")
    else:
        lockbasename = filename
    lockfilepath = os.path.join(confighome, f"{lockbasename}.lock")
    if os.path.isfile(lockfilepath):
        filename = update_filename_with_lockfile(logdir, filename, lockfilepath)
    if is_main_process:
        for lockfilepath in safe_glob(
            os.path.join(confighome, f"{lockbasename}.mp-worker-*.lock")
        ):
            with contextlib.suppress(Exception):
                os.remove(lockfilepath)
    else:
        # Running as child from multiprocessing under Windows
        lockbasename = f"{lockbasename}.mp-worker-"
        process_num = 1
        while os.path.isfile(
            os.path.join(confighome, f"{lockbasename}{process_num}.lock")
        ):
            process_num += 1
        lockfilepath = os.path.join(confighome, f"{lockbasename}{process_num}.lock")
        try:
            with open(lockfilepath, "w"):
                pass
        except Exception:
            pass
        else:
            atexit.register(os.remove, lockfilepath)
        when = "never"
        filename = f"{filename}.mp-worker-{process_num}"
        mode = "w"

    return when, filename, mode


def update_filename_with_lockfile(
    logdir: str,
    filename: str,
    lockfilepath: str,
) -> str:
    """Update the filename based on the lock file.

    Args:
        logdir (str): Directory where log files will be stored.
        filename (str): Name of the log file.
        lockfilepath (str): Path to the lock file.
    """
    is_main_process = not is_multiprocessing_child()
    try:
        with open(lockfilepath) as lockfile:
            instances = len(lockfile.read().splitlines())
    except Exception:
        return filename

    if not is_main_process:
        # Running as child from multiprocessing under Windows
        instances -= 1
    if not instances:
        return filename

    filenames = [filename]
    filename = f"{filename}.{instances}"
    filenames.append(filename)
    if not filenames[0].endswith("-apply-profiles"):
        return filename
    # Running the profile loader always sends a close
    # request to an already running instance, so there
    # will be at most two logfiles, and we want to use
    # the one not currently in use.
    mtimes = {}
    for filename in filenames:
        logfile = os.path.join(logdir, f"{filename}.log")
        if not os.path.isfile(logfile):
            mtimes[0] = filename
            continue
        try:
            logstat = os.stat(logfile)
        except Exception as exception:
            print(f"Warning - os.stat('{logfile}') failed: {exception}")
        else:
            mtimes[logstat.st_mtime] = filename
    if mtimes:
        filename = mtimes[sorted(mtimes.keys())[0]]
    return filename


def rotate_log_files(backup_count: int, logdir: str, logfile: str) -> None:
    """Rotate log files if needed.

    Args:
        backup_count (int): Number of backup files to keep.
        logdir (str): Directory where log files are stored.
        logfile (str): Path to the current log file.
    """
    try:
        logstat = os.stat(logfile)
    except Exception as exception:
        print(f"Warning - os.stat('{logfile}') failed: {exception}")
        return

    # rollover needed?
    now = localtime()
    mtime = get_log_mtime(logstat, now)
    if now[:3] <= mtime[:3]:
        return

    # do rollover
    logbackup = logfile + strftime(".%Y-%m-%d", mtime)
    validate_and_rename_logfile(logfile, logbackup)
    ext_match = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    base_name = os.path.basename(logfile)
    try:
        file_names = os.listdir(logdir)
    except Exception as exception:
        print(
            f"Warning - log directory '{logdir}' "
            f"listing failed during rollover: {exception}"
        )
        return

    result = []
    prefix = base_name + "."
    plen = len(prefix)
    for file_name in file_names:
        if file_name[:plen] != prefix:
            continue
        suffix = file_name[plen:]
        if ext_match.match(suffix):
            result.append(os.path.join(logdir, file_name))
    result.sort()
    if len(result) <= backup_count:
        return
    for logbackup in result[: len(result) - backup_count]:
        try:
            os.remove(logbackup)
        except Exception as exception:
            print(
                f"Warning - logfile backup '{logbackup}' "
                f"could not be removed during rollover: {exception}"
            )


def get_log_mtime(
    logstat: os.stat_result,
    now: tuple[int, int, int, int, int, int],
) -> tuple[int, int, int, int, int, int]:
    """Get the modification time of the log file.

    Args:
        logstat (os.stat_result): The stat result of the log file.
        now (tuple[int, int, int, int, int, int]): The current time as a tuple
            of (year, month, day, hour, minute, second).

    Returns:
        tuple[int, int, int, int, int, int]: The modification time as a
            tuple of (year, month, day, hour, minute, second).
    """
    t = logstat.st_mtime
    try:
        mtime = localtime(t)
    except ValueError:
        # This can happen on Windows because localtime() is buggy on
        # that platform. See:
        # http://stackoverflow.com/questions/4434629/zipfile-module-in-python-runtime-problems
        # http://bugs.python.org/issue1760357
        # To overcome this problem, we ignore the real modification
        # date and force a rollover
        t = time() - 60 * 60 * 24
        mtime = localtime(t)
        # Deal with DST

    dst_now = now[-1]
    dst_then = mtime[-1]
    if dst_now != dst_then:
        addend = 3600 if dst_now else -3600
        mtime = localtime(t + addend)

    return mtime


def validate_and_rename_logfile(logfile: str, logbackup: str) -> None:
    """Validate and rename the log file to a backup.

    Args:
        logfile (str): Path to the current log file.
        logbackup (str): Path to the backup log file.
    """
    if os.path.exists(logbackup):
        try:
            os.remove(logbackup)
        except Exception as exception:
            print(
                f"Warning - logfile backup '{logbackup}' "
                f"could not be removed during rollover: {exception}"
            )

    try:
        os.rename(logfile, logbackup)
    except Exception as exception:
        print(
            f"Warning - logfile '{logfile}' could not be renamed to "
            f"'{os.path.basename(logbackup)}' during rollover: {exception}"
        )
        # Adapted from Python 2.6's
        # logging.handlers.TimedRotatingFileHandler.getFilesToDelete


def open_fault_log(path: str, label: str, max_size: int = 1024 * 1024) -> TextIO:
    """Open the faulthandler log for appending and write a start header.

    The file is appended to rather than truncated, so the crash dump of a
    previous run, or of another instance that is still running, is kept. If
    the file is larger than max_size, it is first moved to "<path>.prev"
    (this is skipped if it fails, e.g. while another instance holds the file
    open).

    Note that faulthandler never records a Py_FatalError (CPython disables
    faulthandler before it aborts); that text only goes to the C-level stderr.

    Args:
        path (str): Path of the fault log file.
        label (str): Identifies the process in the header, e.g. the
            application name and version.
        max_size (int, optional): Size in bytes above which the existing file
            is moved aside. Defaults to 1 MiB.

    Returns:
        TextIO: The open file, to be passed to faulthandler.enable().
    """
    try:
        if os.path.getsize(path) > max_size:
            os.replace(path, f"{path}.prev")
    except OSError:
        pass
    fault_log = open(path, "a", encoding="utf-8")  # noqa: SIM115
    fault_log.write(
        f"=== {strftime('%Y-%m-%d %H:%M:%S')} {label} PID {os.getpid()} started ===\n"
    )
    fault_log.flush()
    return fault_log


def write_fault_log_footer(fault_log: TextIO, label: str) -> None:
    """Write an exit footer to the fault log. Never raises.

    Meant to run at exit, so a start header without a matching footer means
    that the process did not exit normally (e.g. it crashed or was killed).
    Also tells real crashes apart from handled first-chance exceptions which
    faulthandler logs on Windows as well.

    Args:
        fault_log (TextIO): The file returned by open_fault_log().
        label (str): The label that was passed to open_fault_log().
    """
    with contextlib.suppress(Exception):
        fault_log.write(
            f"=== {strftime('%Y-%m-%d %H:%M:%S')} {label} PID {os.getpid()} "
            "exited ===\n"
        )
        fault_log.flush()


def setup_logging(
    logdir: str,
    name: str = APPNAME,
    ext: str = ".py",
    backup_count: int = 5,
    confighome: None | str = None,
) -> None:
    """Setup the logging facility.

    Args:
        logdir (str): Directory where log files will be stored.
        name (str, optional): Name of the application or script.
        ext (str, optional): File extension for the log file.
        backup_count (int, optional): Number of backup log files to keep.
        confighome (None | str, optional): Configuration home directory, used
            for unique log file names.
    """
    global _LOGDIR, LOGGER
    _LOGDIR = logdir
    name = script2pywname(name)
    if name.startswith((APPNAME, "dispcalGUI")) or ext in (".app", ".exe", ".pyw"):
        LOGGER = get_file_logger(
            None,
            LOGLEVEL,
            "midnight",
            backup_count,
            filename=name,
            confighome=confighome,
        )
        if name in (APPNAME, "dispcalGUI"):
            streamhandler = logging.StreamHandler(LOGBUFFER)
            streamformatter = logging.Formatter("%(asctime)s %(message)s")
            streamhandler.setFormatter(streamformatter)
            LOGGER.addHandler(streamhandler)
        redirect_stdio_to_log()


# Per-thread flag that is set while a record is being emitted to the app log
# file handlers. Anything written to a redirected standard stream while it is
# set (e.g. by a handler reporting its own error to sys.stderr) is only passed
# through to the original stream, so emitting can never recurse.
_EMITTING = threading.local()
# StdioToLog instances installed by redirect_stdio_to_log()
_STDIO_LOG_STREAMS: list[StdioToLog] = []


def _app_file_handlers() -> list[logging.FileHandler]:
    """Return the file handlers of the application logger.

    Returns:
        list[logging.FileHandler]: The file handlers of LOGGER (no
            LOGBUFFER/stream handlers), or an empty list if there are none.
    """
    logger = LOGGER
    if logger is None:
        return []
    return [h for h in list(logger.handlers) if isinstance(h, logging.FileHandler)]


def _emit_to_app_log(level: int, msg: str) -> bool:
    """Emit msg as one record directly to the application log file handlers.

    This bypasses LOG(), LOGBUFFER and the wx log window, so it is safe to use
    from exception hooks and from any thread. No lock of ours is held while
    the handlers run. Never raises.

    Args:
        level (int): The log level of the record.
        msg (str): The message. May span multiple lines.

    Returns:
        bool: True if the record was handed to at least one application log
            file handler, False if there is none (or if called re-entrantly
            while emitting).
    """
    if getattr(_EMITTING, "active", False):
        return False
    try:
        handlers = _app_file_handlers()
        if not handlers:
            return False
        _EMITTING.active = True
        try:
            record = logging.LogRecord(
                LOGGER.name, level, __file__, 0, msg, None, None
            )
            for handler in handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)
        finally:
            _EMITTING.active = False
    except Exception:
        return False
    return True


def log_error(text: str) -> bool:
    """Record an error (e.g. a traceback) in the application log file.

    The text is written as one ERROR record directly to the application log
    file handlers, bypassing LOG(), LOGBUFFER and the wx log window, so this
    is safe to call from exception hooks and from any thread. If there is no
    application log file handler (e.g. before setup_logging() ran), the text
    is written to sys.stderr instead. Never raises.

    Args:
        text (str): The text to record. May span multiple lines.

    Returns:
        bool: True if the text was recorded in the application log file,
            False if it was written to sys.stderr instead (or could not be
            written at all).
    """
    try:
        text = str(text).rstrip()
        if _emit_to_app_log(logging.ERROR, text):
            return True
        stream = sys.stderr
        if stream is not None:
            stream.write(f"{text}\n")
            stream.flush()
    except Exception:
        pass
    return False


class _LineBuffer:
    """Pending partial line of one thread for one StdioToLog stream."""

    __slots__ = ("__weakref__", "pending")

    def __init__(self) -> None:
        self.pending = ""


class StdioToLog:
    """Standard stream replacement that sends whole lines to the app log.

    Installed by redirect_stdio_to_log() in place of a standard output stream
    which would otherwise discard everything written to it: py2exe's
    ``Blackhole`` (sys.stdout) and ``Stderr`` (sys.stderr) in the windowed
    frozen builds, or None (pythonw). Each whole line becomes one record of
    the given level in the application log file. This also catches the
    output of the default threading.excepthook and sys.unraisablehook.

    Partial lines are buffered per thread, and no lock is held while a
    record is emitted, so there is no lock-order inversion with the handlers'
    locks. Consecutive identical lines (like the profile loader's monitoring
    thread "Acquired lock" message every few seconds) are collapsed into
    "(last message repeated N times)", which is still written at least every
    ``repeat_summary_interval`` seconds while the repetition goes on, so the
    log keeps showing that the process was alive.

    Like py2exe's Blackhole, this deliberately has no ``isatty()`` and no
    ``fileno()``: e.g. Worker.exec_cmd checks ``hasattr(sys.stdout,
    "isatty")`` to decide how to capture subprocess output, and that decision
    must not change.

    Args:
        level (int): The log level for lines written to this stream.
        original (None | TextIO): The stream object that is replaced.
        passthrough (bool, optional): Also write everything to the original
            stream. Text is always passed through to the original stream if
            there is no application log file handler. Defaults to False.
    """

    is_log_stream = True
    encoding = "utf-8"
    repeat_summary_interval = 600.0
    max_line_length = 64 * 1024

    def __init__(
        self,
        level: int,
        original: None | TextIO = None,
        passthrough: bool = False,
    ) -> None:
        self.level = level
        self.original = original
        self.passthrough = passthrough
        self._local = threading.local()
        # Only protects the bookkeeping below. Never held while emitting.
        self._lock = threading.Lock()
        self._buffers = weakref.WeakSet()
        self._last_line = None
        self._repeat_count = 0
        self._repeat_time = 0.0

    def write(self, text: str | bytes) -> int:
        """Write text. Whole lines are sent to the application log.

        Args:
            text (str | bytes): The text to write.

        Returns:
            int: The number of characters written.
        """
        try:
            if isinstance(text, (bytes, bytearray)):
                text = bytes(text).decode("utf-8", "backslashreplace")
            elif not isinstance(text, str):
                text = str(text)
        except Exception:
            return 0
        try:
            if (
                getattr(_EMITTING, "active", False)
                or sys.is_finalizing()
                or not _app_file_handlers()
            ):
                self._write_original(text)
                return len(text)
            if self.passthrough:
                self._write_original(text)
            self._log_lines(self._take_lines(text))
        except Exception:
            pass
        return len(text)

    def writelines(self, lines: list[str]) -> None:
        """Write a sequence of strings.

        Args:
            lines (list[str]): The strings to write.
        """
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Flush the original stream."""
        original = self.original
        if original is not None:
            with contextlib.suppress(Exception):
                original.flush()

    def flush_pending(self) -> None:
        """Log every thread's pending partial line and any repeat summary.

        Called at exit, so the last words of the process are not lost.
        """
        try:
            if (
                getattr(_EMITTING, "active", False)
                or sys.is_finalizing()
                or not _app_file_handlers()
            ):
                return
            with self._lock:
                messages = []
                if self._repeat_count:
                    messages.append(self._take_repeat_summary(time()))
                for buffer in list(self._buffers):
                    pending, buffer.pending = buffer.pending, ""
                    if pending.strip():
                        messages.append(pending.rstrip())
            for message in messages:
                _emit_to_app_log(self.level, message)
        except Exception:
            pass

    def _write_original(self, text: str) -> None:
        """Write text to the original stream, ignoring errors.

        Args:
            text (str): The text to write.
        """
        original = self.original
        if original is not None:
            with contextlib.suppress(Exception):
                original.write(text)

    def _take_lines(self, text: str) -> list[str]:
        """Add text to the calling thread's buffer and take the whole lines.

        Args:
            text (str): The text to add.

        Returns:
            list[str]: The complete lines, without line endings.
        """
        buffer = getattr(self._local, "buffer", None)
        if buffer is None:
            buffer = self._local.buffer = _LineBuffer()
            with self._lock:
                self._buffers.add(buffer)
        text = (buffer.pending + text).replace("\r\n", "\n").replace("\r", "\n")
        *lines, pending = text.split("\n")
        if len(pending) > self.max_line_length:
            lines.append(pending)
            pending = ""
        buffer.pending = pending
        return lines

    def _log_lines(self, lines: list[str]) -> None:
        """Send lines to the log, collapsing consecutive identical lines.

        Args:
            lines (list[str]): The lines to log.
        """
        messages = []
        now = time()
        with self._lock:
            for line in lines:
                line = line.rstrip()  # noqa: PLW2901
                if not line:
                    continue
                if line == self._last_line:
                    self._repeat_count += 1
                    if now - self._repeat_time >= self.repeat_summary_interval:
                        messages.append(self._take_repeat_summary(now))
                    continue
                if self._repeat_count:
                    messages.append(self._take_repeat_summary(now))
                self._last_line = line
                self._repeat_time = now
                messages.append(line)
        # Emit without holding the lock
        for message in messages:
            _emit_to_app_log(self.level, message)

    def _take_repeat_summary(self, now: float) -> str:
        """Return the repeat summary message and reset the repeat count.

        Must be called with the lock held.

        Args:
            now (float): The current time.

        Returns:
            str: The repeat summary message.
        """
        count = self._repeat_count
        self._repeat_count = 0
        self._repeat_time = now
        return f"(last message repeated {count} time{'' if count == 1 else 's'})"


def _discards_output(stream: None | TextIO, shim_name: str) -> bool:
    """Return whether everything written to a standard stream is lost.

    Args:
        stream (None | TextIO): The stream (sys.stdout or sys.stderr).
        shim_name (str): Class name of the py2exe replacement for the stream:
            "Blackhole" for sys.stdout, "Stderr" for sys.stderr.

    Returns:
        bool: True if the stream is None (pythonw) or py2exe's replacement.
    """
    if stream is None:
        return True
    if getattr(stream, "is_log_stream", False):
        return False
    return type(stream).__name__ == shim_name


def _flush_stdio_log_streams() -> None:
    """Log pending partial lines and repeat summaries of all redirections."""
    for stream in list(_STDIO_LOG_STREAMS):
        stream.flush_pending()


def redirect_stdio_to_log() -> bool:
    """Send output of discarded standard streams to the application log.

    In the windowed py2exe builds, sys.stdout is py2exe's Blackhole, which
    throws away everything written to it, i.e. every print() (most of
    DisplayCAL's diagnostic output), and sys.stderr is py2exe's Stderr shim,
    an untimestamped log file in %APPDATA%. Under pythonw both are None. Only
    such streams are replaced with StdioToLog: sys.stdout with level INFO,
    sys.stderr with level ERROR (still also written to the py2exe shim, so
    its log and its "Errors in..." message at exit keep working). Console
    runs, pytest and Linux/macOS terminals are not affected.

    Does nothing if there is no application log file handler (yet), and
    nothing for a stream that has already been redirected.

    Returns:
        bool: True if at least one stream was redirected.
    """
    if not _app_file_handlers():
        return False
    redirected = []
    if _discards_output(sys.stdout, "Blackhole"):
        sys.stdout = StdioToLog(logging.INFO, sys.stdout)
        redirected.append(sys.stdout)
    if _discards_output(sys.stderr, "Stderr"):
        sys.stderr = StdioToLog(logging.ERROR, sys.stderr, passthrough=True)
        redirected.append(sys.stderr)
    if redirected:
        if not _STDIO_LOG_STREAMS:
            atexit.register(_flush_stdio_log_streams)
        _STDIO_LOG_STREAMS.extend(redirected)
    return bool(redirected)
