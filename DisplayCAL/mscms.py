"""Provides a thread-safe interface for the Windows Color System API.

This isolates the actual communication with WinAPI to separate process.
Windows only.

References and useful info:

According to https://learn.microsoft.com/en-us/previous-versions/troubleshoot/windows/win32/geticmprofile-might-leak-one-more-handles-windows-10 and
https://learn.microsoft.com/en-us/troubleshoot/windows/win32/geticmprofile-might-leak-one-or-more-handles-on-windows10
(in case one of those breaks eventually) Microsoft couldn't be bothered to fix
leaking handles in mscms.dll in Windows 10. That causes issues for applications
which do frequent calls to the leaking API calls:

   + GetICMProfile
   + EnumICMProfiles
   + WcsGetDefaultColorProfile
   + WcsGetDefaultColorProfileSize
   + WcsGetUsePerUserProfiles

Other mitigation tactics possible:

 + closing suspicious reg handles to specific keys
   (https://gist.github.com/AySz88/7a233d84632498a8de382c1199f859f2 and
   DisplayCal has its own implementation in _win10_1903_close_leaked_regkey_handles()).
 + using said APIs less (caching, etc)
 + restarting once in a while (profile loader, according to the changelog 3.8.7,
   restarts each day at 4.00 due to leaks in SetDeviceGammaRamp).
 + calling such APIs in separate processes

"""  # noqa: E501

from __future__ import annotations

import atexit
import builtins
import contextlib
import logging
import multiprocessing
import sys
import threading
import uuid
from functools import wraps
from logging.handlers import QueueHandler, QueueListener
from multiprocessing import Process, Queue
from queue import Empty
from threading import Lock, Thread
from time import sleep
from typing import (
    Any,
    Callable,
    Literal,
    TextIO,
    TypeVar,
    Union,
)

import psutil

if sys.version_info >= (3, 11):
    from typing import NotRequired, ParamSpec, TypedDict
else:
    from typing_extensions import NotRequired, ParamSpec, TypedDict


from DisplayCAL.mscms_types import (
    COLORPROFILESUBTYPE,
    COLORPROFILETYPE,
    WCS_PROF_SCOPE,
    dwDeviceClass,
)

if sys.platform == "win32":
    from DisplayCAL.mscms_wrapper import WCS
else:

    class WCS:
        """Placeholder class for non-Windows platforms."""

        def __getattribute__(self, name: str) -> None:
            """Placeholder method."""
            raise NotImplementedError("Windows only")

        def __setattr__(self, name: str, value: Any) -> None:  # noqa: ANN401
            """Placeholder method."""
            raise NotImplementedError("Windows only")

        def __delattr__(self, name: str) -> None:
            """Placeholder method."""
            raise NotImplementedError("Windows only")

        def __call__(self, *args, **kwargs) -> None:
            """Placeholder method."""
            raise NotImplementedError("Windows only")


default_logging_level = logging.INFO
logger = logging.getLogger(__name__ + ".manager")
logger.setLevel(default_logging_level)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _configure_logger(
    manager_logger: logging.Logger, stream: None | TextIO = None
) -> None:
    """Configure where the WCS manager's log records go.

    Only attach a real stderr handler when stderr is an interactive console.
    A frozen windowed exe (py2exe) replaces sys.stderr with a lazily-opening
    log-file shim that pops up an "Errors in ..." dialog on exit for ANY
    write, even plain INFO messages, so don't write to stderr there (see
    #804). Instead, let the records propagate to the application's (root)
    logger, which writes them to DisplayCAL's own log file.

    Args:
        manager_logger (logging.Logger): The logger to configure.
        stream (None | TextIO, optional): The stderr stream to check.
            Defaults to sys.stderr.
    """
    if stream is None:
        stream = sys.stderr
    if hasattr(stream, "isatty") and stream.isatty():
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        manager_logger.addHandler(handler)
        manager_logger.propagate = False
    else:
        manager_logger.propagate = True


_configure_logger(logger)


class _DispatchToLogger(logging.Handler):
    """Hand log records received from the worker process to the local logger.

    Used by the WCS manager's QueueListener, so that records logged by the
    worker process end up wherever records of a logger of the same name in
    this process would go (normally the application's log file).
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Pass the record to the logger it was logged with.

        Args:
            record (logging.LogRecord): The record received from the worker.
        """
        logging.getLogger(record.name).handle(record)

check_handles_call_num = 100  # check leaking handles every <num> of calls
open_handles_threshold = 3000  # worker process open handle limit

FILE_NOT_FOUND_ERRNO = 2


class RequestType(TypedDict):
    """Represents a request sent to the worker process."""

    id: str
    method: str
    args: Any
    kwargs: Any


class ErrorType(TypedDict):
    """Represents an error response from the worker process."""

    type: str
    message: str
    errno: NotRequired[Any]


class SuccessResponseType(TypedDict):
    """Represents a successful response from the worker process."""

    type: Literal["resp_success"]
    id: str
    result: Any


class FailureResponseType(TypedDict):
    """Represents an error response from the worker process."""

    type: Literal["resp_error"]
    id: str
    error: ErrorType


ResponseType = Union[SuccessResponseType, FailureResponseType]


class PendingType(TypedDict):
    """Represents a pending request in the WCSManager."""

    event: threading.Event
    result: NotRequired[Any]
    error: NotRequired[ErrorType]


class WCSError(Exception):
    """Base class for WCS-related errors."""


class WCSManagerShutdownError(WCSError):
    """Error type for operations attempted while WCSManager is shutting down."""


class WCSManagerRestartingError(WCSManagerShutdownError):
    """The worker process is being restarted (retried, unlike a shutdown)."""


class WCSWorkerError(WCSError):
    """Generic error type for exceptions raised during remote execution."""


F_Spec = ParamSpec("F_Spec")
F_Return = TypeVar("F_Return")


def _format_exitcode(exitcode: None | int) -> str:
    """Format a process exit code for logging.

    Windows status codes are easier to recognize in hexadecimal, e.g. an
    access violation is logged as ``3221225477 (0xC0000005)``.

    Args:
        exitcode (None | int): The exit code (None if still running).

    Returns:
        str: The formatted exit code.
    """
    if exitcode is not None and exitcode > 255:
        return f"{exitcode} (0x{exitcode:08X})"
    return str(exitcode)


def retry(
    retries: int = 5,
    base_delay: float = 0.1,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[F_Spec, F_Return]], Callable[F_Spec, F_Return]]:
    """A simple retry decorator with exponential backoff for specified exceptions."""

    def real_retry(func: Callable[F_Spec, F_Return]) -> Callable[F_Spec, F_Return]:
        @wraps(func)
        def wrapper(*args: F_Spec.args, **kwargs: F_Spec.kwargs) -> F_Return:
            if retries < 0:
                raise ValueError("Retries must be non-negative")

            if base_delay < 0:
                raise ValueError("Base delay must be non-negative")

            attempt = 0
            last_exception: None | Exception = None
            while attempt <= retries:
                if attempt > 0:  # logging as retry after first attempt
                    logger.debug(
                        f"Retrying {func.__name__}: "
                        f"attempt {attempt}/{retries} after {last_exception}"
                    )

                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    attempt += 1
                    last_exception = e

                    if attempt <= retries:
                        delay = base_delay * (
                            2 ** (attempt - 1)
                        )  # exponential backoff with no jitter
                        logger.debug(
                            f"Retryable error caught in {func.__name__}: {e}. "
                            f"Retrying in {delay:.3f}s..."
                        )
                        sleep(delay)
                    else:
                        raise last_exception from e
                except Exception as e:
                    logger.debug(f"Stopping retry in {func.__name__} on exception: {e}")
                    raise
            raise RuntimeError("General retry decorator error")

        return wrapper

    return real_retry


def _wcs_worker_process(
    request_queue: Queue[RequestType | None],
    response_queue: Queue[ResponseType],
    log_queue: Queue[Any],
) -> None:
    wcs_instance = None

    log_queue_handler = QueueHandler(log_queue)
    logger = logging.getLogger(__name__ + ".wcsworker")
    logger.setLevel(default_logging_level)
    logger.addHandler(log_queue_handler)
    logger.propagate = False

    logger.info("WCSWorker process started")

    parent_gone = False
    try:
        wcs_instance = WCS()
        logger.debug("WCS instance initialized in worker")

        while True:
            try:
                request: None | RequestType = request_queue.get(timeout=1.0)
            except Empty:
                parent = multiprocessing.parent_process()
                if parent is not None and not parent.is_alive():
                    # Our parent died without telling us to exit (e.g. it
                    # crashed or was killed). Don't linger as an orphan,
                    # which would also block the next loader start (it
                    # matches the loader's executable name).
                    parent_gone = True
                    break
                continue

            if request is None:
                logger.info("WCSWorker received poison pill")
                break

            request_id = request.get("id")
            method_name: str = request.get("method")
            args = request.get("args", ())
            kwargs = request.get("kwargs", {})

            logger.debug(f"WCSWorker processing request ID {request_id}: {method_name}")

            try:
                method = getattr(wcs_instance, method_name)
                result = method(*args, **kwargs)
                response: ResponseType = {
                    "type": "resp_success",
                    "id": request_id,
                    "result": result,
                }
            except Exception as e:
                response: ResponseType = {
                    "type": "resp_error",
                    "id": request_id,
                    "error": {
                        "type": type(e).__name__,
                        "message": str(e),
                        "errno": getattr(e, "errno", None),  # if present
                    },
                }

            try:
                response_queue.put(response)
                logger.debug(f"WCSWorker sent response for request ID {request_id}")
            except Exception as e:
                logger.error(f"Failed to send response for request {request_id}: {e}")

    except Exception as e:
        logger.critical(f"Unexpected error in WCSWorker process: {e}", exc_info=True)
    finally:
        if parent_gone:
            # Nobody reads our outgoing queues anymore. Don't wait for their
            # feeder threads to flush at exit (Queue._finalize_join would
            # block forever if there is a backlog), and don't log anything.
            for outgoing_queue in (response_queue, log_queue):
                with contextlib.suppress(Exception):
                    outgoing_queue.cancel_join_thread()
        else:
            logger.info("WCSWorker process shutting down")
        if wcs_instance:
            del wcs_instance
        # Potential logger reuse handling
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


class WCSManager:
    """WCSManager class.

    Note: can only be initialized once.

    Args:
        handle_threshold (int, optional): Maximum open handles limit.
            Defaults to 10000.
        request_timeout (float, optional): Timeout for an individual request
            (note the retry logic and compound requests). Defaults to 10.0.
    """

    def __init__(
        self,
        handle_threshold: int = open_handles_threshold,  # will restart once in a while
        request_timeout: float = 10.0,
    ) -> None:
        if hasattr(self, "_initialized"):
            return

        self.handle_threshold = handle_threshold
        self.request_timeout = request_timeout

        self._lock = Lock()
        self._worker_process: None | Process = None
        self._request_queue: None | RequestType = Queue()
        self._response_queue: None | ResponseType = Queue()
        self._log_queue: Queue[Any] = Queue()
        self._log_listener: None | QueueListener = None
        self._pending_requests: dict[str, PendingType] = {}
        self._shutdown_event = threading.Event()
        # Set by shutdown(): distinguishes closing for good from the brief
        # stop/start of a worker restart (when _shutdown_event is also set)
        self._closing = threading.Event()
        self._restarting = threading.Event()
        # Serializes worker restarts (separate from _lock, which _stop_worker
        # and _start_worker acquire themselves)
        self._restart_lock = Lock()
        self._wcs_calls_made = 0

        self._response_listener_thread: None | Thread = None
        self._listener_stop: None | threading.Event = None

        handlers = logger.handlers if logger.handlers else [_DispatchToLogger()]
        self._log_listener = QueueListener(
            self._log_queue, *handlers, respect_handler_level=True
        )
        self._log_listener.start()
        logger.debug("QueueListener for worker logs started")

        self._start_worker()
        self._initialized = True
        atexit.register(self._atexit_shutdown)
        logger.info("WCSManager initialized")

    def _start_worker(self) -> None:
        """Start the worker process and response listener thread."""
        with self._lock:
            if self._worker_process and self._worker_process.is_alive():
                logger.warning("Worker is already running")
                return

            if self._worker_process is not None and self._worker_process.exitcode:
                # The previous worker did not exit cleanly (it crashed or was
                # terminated), possibly while holding the request queue's
                # reader lock, which would starve its successor. Use a fresh
                # request queue.
                self._replace_request_queue()

            self._shutdown_event.clear()

            self._worker_process = multiprocessing.Process(
                target=_wcs_worker_process,
                args=(self._request_queue, self._response_queue, self._log_queue),
            )
            self._worker_process.start()
            logger.info(
                f"WCSWorker process started with PID {self._worker_process.pid}"
            )

            # Stop the previous response listener (after a restart without a
            # preceding _stop_worker, e.g. of a dead worker, nothing else
            # would) and start one for the new worker
            if self._listener_stop is not None:
                self._listener_stop.set()
            self._listener_stop = threading.Event()
            self._response_listener_thread = Thread(
                target=self._listen_for_responses,
                args=(self._listener_stop,),
                daemon=True,
            )
            self._response_listener_thread.start()
            logger.debug("Response listener thread started")

    def _replace_request_queue(self) -> None:
        """Replace the request queue with a new one (call with _lock held)."""
        old_queue = self._request_queue
        self._request_queue = Queue()
        # Requests still in the old queue are lost: their callers time out
        # and retry. Don't let the old queue's feeder thread block our exit.
        with contextlib.suppress(Exception):
            old_queue.cancel_join_thread()
            old_queue.close()

    def _restart_worker(self, reason: str, if_dead: bool = False) -> None:
        """Restart the worker process, unless the manager is shutting down.

        Args:
            reason (str): Why the worker is restarted (for the log).
            if_dead (bool, optional): Only restart the worker if it is not
                running (checked again under the restart lock, so concurrent
                callers restart it only once). Defaults to False.
        """
        with self._restart_lock:
            if self._closing.is_set():
                return
            worker = self._worker_process
            if if_dead:
                if worker is None or worker.is_alive():
                    return
                reason = (
                    f"WCSWorker process PID {worker.pid} is not running, exit "
                    f"code {_format_exitcode(worker.exitcode)}"
                )
                logger.error(f"{reason}. Restarting it")
            else:
                logger.info(f"Restarting WCSWorker process: {reason}")
            self._restarting.set()
            try:
                self._stop_worker()
                self._start_worker()
            finally:
                self._restarting.clear()

    def _ensure_worker_alive(self) -> None:
        """Restart the worker process if it died (e.g. crashed or was killed).

        Without this, every request to a dead worker would time out (with
        retries, after about a minute) and the worker would never come back.
        """
        worker = self._worker_process
        if worker is not None and not worker.is_alive():
            self._restart_worker("worker not running", if_dead=True)

    def _stop_worker(self) -> None:
        """Stop the worker process with timeout and forceful termination if needed."""
        with self._lock:
            if not self._worker_process or not self._worker_process.is_alive():
                logger.debug("Worker is not running")
                return

            logger.info("Sending shutdown signal to WCSWorker")
            try:
                # Poison Pill
                self._request_queue.put(None)
            except Exception as e:
                logger.error(f"Failed to send shutdown signal: {e}")

            # Waiting for process completion
            self._worker_process.join(timeout=5.0)
            if self._worker_process.is_alive():
                logger.warning(
                    "WCSWorker did not terminate gracefully, terminating forcefully."
                )
                self._worker_process.terminate()
                self._worker_process.join(timeout=2.0)

            if self._worker_process.is_alive():
                logger.error("Failed to terminate WCSWorker process")
            else:
                logger.info("WCSWorker process terminated")

            self._shutdown_event.set()  # Closing down listener and call functionality

    def _handle_check_worker(self) -> None:
        logger.debug("Performing worker process open handle check")

        p = None
        num_handles = 0

        try:
            if self._worker_process:
                p = psutil.Process(self._worker_process.pid)
                num_handles = p.num_handles()
            else:
                logger.warning("No running worker process discovered")

        except Exception as e:
            logger.warning(
                "Failed to get handle count for process "
                f"{p.pid if p else 'unknown'}: {e}"
            )
            return

        if num_handles > self.handle_threshold:
            self._restart_worker(
                "Open handle number exceeded: "
                f"{num_handles}>{self.handle_threshold}"
            )
            logger.info("WCSWorker restarted successfully")
        else:
            logger.debug(f"Current child process open handles: {num_handles}")

    def _listen_for_responses(self, stop_event: None | threading.Event = None) -> None:
        logger.debug("Response listener thread main loop started")
        while not self._shutdown_event.is_set() and not (
            stop_event and stop_event.is_set()
        ):
            try:
                # Short timeout to be able to stop the thread
                response = self._response_queue.get(timeout=1.0)
            except Empty:
                continue
            except Exception as e:
                if not self._shutdown_event.is_set():
                    logger.error(f"Error getting response from queue: {e}")
                continue

            # Regular path
            request_id = response.get("id")
            logger.debug(f"Received response for request ID {request_id}")

            with self._lock:
                if request_id in self._pending_requests:
                    req_data = self._pending_requests.pop(request_id)
                    event = req_data.get("event")
                    if event:
                        req_data["result"] = response.get("result")
                        if "error" in response:
                            req_data["error"] = response.get("error")
                        self._pending_requests[request_id] = req_data
                        event.set()  # signal _call_wcs_method
                else:
                    logger.warning(
                        f"Received response for unknown request ID {request_id}"
                    )

        logger.debug("Response listener thread loop finished")

    @retry(retry_on=(RuntimeError, TimeoutError, WCSManagerRestartingError))
    def _call_wcs_method(self, method_name: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if self._shutdown_event.is_set():
            if self._restarting.is_set() and not self._closing.is_set():
                raise WCSManagerRestartingError("WCSWorker process is restarting")
            raise WCSManagerShutdownError("WCSManager is set to shut down")

        self._ensure_worker_alive()

        request_id = str(uuid.uuid4())
        request: RequestType = {
            "id": request_id,
            "method": method_name,
            "args": args,
            "kwargs": kwargs,
        }

        event = threading.Event()
        with self._lock:
            logger.debug(f"Request ID {request_id} added to the pending requests")
            self._pending_requests[request_id] = {"event": event}

        try:
            self._request_queue.put(request)
            logger.debug(f"Sent request ID {request_id} for method {method_name}")
        except Exception as e:
            with self._lock:
                self._pending_requests.pop(request_id, None)
                logger.debug(
                    f"Request ID {request_id} removed from pending requests: "
                    "cancellation"
                )
            raise RuntimeError(f"Failed to send request to worker: {e}") from e

        with self._lock:
            self._wcs_calls_made += 1

            if self._wcs_calls_made % check_handles_call_num == 0:
                logger.debug("Worker process open handle check triggered")
                # Launch in other thread to not block the listener
                threading.Thread(target=self._handle_check_worker, daemon=True).start()

        # Waiting for an answer to be received
        if not event.wait(timeout=self.request_timeout):
            with self._lock:
                self._pending_requests.pop(request_id, None)
                logger.debug(
                    f"Request ID {request_id} removed from pending requests: timeout"
                )
            raise TimeoutError(f"Timeout waiting for response to request {request_id}")

        # Processing the result
        with self._lock:
            req_data = self._pending_requests.pop(request_id, {})
            logger.debug(
                f"Request ID {request_id} removed from pending requests: success"
            )

        if "error" in req_data:  # error path
            error_response = req_data.get("error")
            if error_response:
                error_type = error_response.get("type")
                error_message = error_response.get("message")
                error_errno = error_response.get("errno")

                # Recreating exceptions
                try:
                    exc_class = getattr(builtins, error_type)
                except Exception:
                    exc_class = WCSWorkerError

                if issubclass(exc_class, OSError):
                    exc = exc_class(error_message)
                    if error_errno is not None:
                        exc.errno = error_errno
                    raise exc
                # Something else might have happened
                raise exc_class(f"{error_type}: {error_message}")

        # normal path
        return req_data.get("result")

    def shutdown(self) -> None:
        """Shutdowns the worker process and listener thread."""
        if self._closing.is_set():
            logger.debug("WCSManager is already set to shut down")
            return
        logger.info("Shutting down WCSManager...")
        self._closing.set()

        # Let a worker restart in progress finish first
        with self._restart_lock:
            self._shutdown_event.set()

            logger.debug("Stopping worker process and threads")
            self._stop_worker()

        if self._response_listener_thread and self._response_listener_thread.is_alive():
            self._response_listener_thread.join(timeout=2.0)
        logger.info("WCSManager shut down complete")

    def _atexit_shutdown(self) -> None:
        """Shutdown handler for atexit to ensure cleanup on normal interpreter exit."""
        if not self._closing.is_set():
            logger.info("WCSManager: atexit triggered shutdown")
            self.shutdown()

    def associate_color_profile_with_device(
        self, scope: WCS_PROF_SCOPE, profile_name: str, device_key: str
    ) -> None:
        """Associate a specified WCS color profile with a specified device.

        This API does not support "advanced color" profiles for HDR monitors.

        Note: this API makes the added profile also be the default one.

        Args:
            scope (WCS_PROF_SCOPE): specifies the scope of this profile management
                operation, which could be system-wide or for the current user.
            profile_name (str): file name of the profile to associate.
            device_key (str): device key of the device with which to associate
                              the profile.

        Raises:
            OSError: in case of Win API errors.
            TimeoutError: if timeout occurs while waiting for worker response.
            WCSManagerShutdownError: if manager is shutting down.
            WCSWorkerError: in case of some unspecified error during remote execution.
            RuntimeError: in case of unrecoverable IPC errors.
        """
        self._call_wcs_method(
            "AssociateColorProfileWithDevice", scope, profile_name, device_key
        )

    def disassociate_color_profile_from_device(
        self, scope: WCS_PROF_SCOPE, profile_name: str, device_key: str
    ) -> None:
        """Disassociate a specified WCS color profile from a specified device.

        This API does not support "advanced color" profiles for HDR monitors.

        Note: very unreliable due to quirks, the actual result should be
        double-checked with profile listing.

        Args:
            scope (WCS_PROF_SCOPE): specifies the scope of this profile
                management operation, which could be system-wide or for the
                current user.
            profile_name (str): file name of the profile to disassociate.
            device_key (str): device key of the device from which to
                disassociate the profile.

        Raises:
            OSError: in case of Win API errors.
            TimeoutError: if timeout occurs while waiting for worker response.
            WCSManagerShutdownError: if manager is shutting down.
            WCSWorkerError: in case of some unspecified error during remote execution.
            RuntimeError: in case of unrecoverable IPC errors.
        """
        self._call_wcs_method(
            "DisassociateColorProfileFromDevice", scope, profile_name, device_key
        )

    def get_device_color_profile_list(
        self,
        scope: WCS_PROF_SCOPE,
        device_key: str,
        device_class: dwDeviceClass = dwDeviceClass.CLASS_MONITOR,
    ) -> list[str]:
        """Enumerate color profiles associated with a device.

        This API does not support "advanced color" profiles for HDR monitors.

        Args:
            scope (WCS_PROF_SCOPE): specifies the scope of this profile
                management operation, which could be system-wide or for the
                current user.
            device_key (str): device key of the device.
            device_class (dwDeviceClass, optional): device class. Defaults to
                dwDeviceClass.CLASS_MONITOR.

        Raises:
            OSError: in case of Win API errors.l
            TimeoutError: if timeout occurs while waiting for worker response.
            WCSManagerShutdownError: if manager is shutting down.
            WCSWorkerError: in case of some unspecified error during remote execution.
            RuntimeError: in case of unrecoverable IPC errors.
            ValueError: on parsing errors.

        Returns:
            list[str]: array of profile names
        """
        prof_list: list[str] = self._call_wcs_method(
            "getDeviceColorProfileList", scope, device_key, device_class
        )
        return prof_list

    def get_calibration_management_state(self) -> bool:
        """Determine if system management of the display calibration state is enabled.

        Raises:
            OSError: in case of Win API errors.
            TimeoutError: if timeout occurs while waiting for worker response.
            WCSManagerShutdownError: if manager is shutting down.
            WCSWorkerError: in case of some unspecified error during remote execution.
            RuntimeError: in case of unrecoverable IPC errors.

        Returns:
            bool: True if system management of the display calibration state is
                enabled; otherwise, False
        """
        return self._call_wcs_method("GetCalibrationManagementState")

    def set_calibration_management_state(self, new_state: bool) -> None:
        """Enable or disables system management of the display calibration state.

        Args:
            new_state (bool): True to enable system management of the display
                calibration state. False to disable it.

        Raises:
            OSError: in case of Win API errors.
            TimeoutError: if timeout occurs while waiting for worker response.
            WCSManagerShutdownError: if manager is shutting down.
            WCSWorkerError: in case of some unspecified error during remote execution.
            RuntimeError: in case of unrecoverable IPC errors.
        """
        return self._call_wcs_method("SetCalibrationManagementState", new_state)

    def get_default_color_profile(
        self,
        scope: WCS_PROF_SCOPE,
        device_key: str,
        c_prof_type: COLORPROFILETYPE = COLORPROFILETYPE.CPT_ICC,
        c_prof_subtype: COLORPROFILESUBTYPE = COLORPROFILESUBTYPE.CPST_NONE,
        profile_id: int = 0,
    ) -> None | str:
        """Retrieve the default color profile for a device.

        This API does not support "advanced color" profiles for HDR monitors.

        Note: If HDR is enabled on a device, it causes OSError.

        Args:
            scope (WCS_PROF_SCOPE): specifies the scope of this profile
                management operation, which could be system-wide or for the
                current user.
            device_key (str): device key of the device for which the default
                color profile is obtained. If empty string, a device-independent
                default is obtained.
            c_prof_type (COLORPROFILETYPE, optional): value specifying the
                color profile type. Defaults to `COLORPROFILETYPE.CPT_ICC`.
            c_prof_subtype (COLORPROFILESUBTYPE, optional): Value specifying
                the color profile subtype. Defaults to `COLORPROFILESUBTYPE.CPST_NONE`.
            profile_id (int, optional): ID of the color space that the color
                profile represents. Defaults to 0.

        Raises:
            OSError: If a Win API error occurs.
            TimeoutError: If a timeout occurs while waiting for worker response.
            WCSManagerShutdownError: If the manager is shutting down.
            WCSWorkerError: In case of some unspecified error during remote execution.
            RuntimeError: If unrecoverable IPC errors occur.

        Returns:
            str: the name of the default color profile for the device (or None
                if not set)
        """
        try:
            size = self._call_wcs_method(
                "GetDefaultColorProfileSize",
                scope,
                device_key,
                c_prof_type,
                c_prof_subtype,
                profile_id,
            )
            return self._call_wcs_method(
                "GetDefaultColorProfile",
                scope,
                device_key,
                size,
                c_prof_type,
                c_prof_subtype,
                profile_id,
            )
        except FileNotFoundError:  # no default profile
            pass
        return None

    def set_default_color_profile(
        self,
        scope: WCS_PROF_SCOPE,
        device_key: str,
        profile_name: str,
        c_prof_type: COLORPROFILETYPE = COLORPROFILETYPE.CPT_ICC,
        c_prof_subtype: COLORPROFILESUBTYPE = COLORPROFILESUBTYPE.CPST_NONE,
        profile_id: int = 0,
    ) -> None:
        """Set the default color profile name for the specified profile type.

        This API does not support "advanced color" profiles for HDR monitors.

        Args:
            scope (WCS_PROF_SCOPE): Specifies the scope of this profile management
                operation, which could be system-wide or for the current user.
            device_key (str): Device key of the device for which the default
                color profile is to be set. If empty string, a device-independent
                default profile is set.
            profile_name (str): File name of the profile.
            c_prof_type (COLORPROFILETYPE, optional): Value specifying the color
                profile type. Defaults to `COLORPROFILETYPE.CPT_ICC`.
            c_prof_subtype (COLORPROFILESUBTYPE, optional): Value specifying
                the color profile subtype. Defaults to
                `COLORPROFILESUBTYPE.CPST_NONE`.
            profile_id (int, optional): ID of the color space that the color
                profile represents. Defaults to 0.

        Raises:
            OSError: If Win API error is raised.
            TimeoutError: If timeout occurs while waiting for worker response.
            WCSManagerShutdownError: If manager is shutting down.
            WCSWorkerError: If some unspecified error occurs during remote execution.
            RuntimeError: If unrecoverable IPC errors occur.
        """
        self._call_wcs_method(
            "SetDefaultColorProfile",
            scope,
            device_key,
            profile_name,
            c_prof_type,
            c_prof_subtype,
            profile_id,
        )

    def get_use_per_user_profiles(
        self, device_key: str, device_class: dwDeviceClass = dwDeviceClass.CLASS_MONITOR
    ) -> bool:
        """Determine if per-user profile association is enabled for the device.

        Args:
            device_key (str): Device key of the device.
            device_class (dwDeviceClass, optional): The class of the device.
                Defaults to `dwDeviceClass.CLASS_MONITOR`.

        Raises:
            OSError: If Win API error is raised.
            TimeoutError: If timeout occurs while waiting for worker response.
            WCSManagerShutdownError: If manager is shutting down.
            WCSWorkerError: If some unspecified error occurs during remote execution.
            RuntimeError: If unrecoverable IPC errors occur.

        Returns:
            bool: True if the user chose to use a per-user profile association
                list for the specified device; otherwise False
        """
        return self._call_wcs_method("GetUsePerUserProfiles", device_key, device_class)

    def set_use_per_user_profiles(
        self,
        device_key: str,
        new_state: bool,
        device_class: dwDeviceClass = dwDeviceClass.CLASS_MONITOR,
    ) -> None:
        """Enable or disable per-user profile association for a device.

        Args:
            device_key (str): Device key of the device.
            new_state (bool): True if the user wants to use a per-user profile
                association list for the specified device; otherwise False.
            device_class (dwDeviceClass, optional): The class of the device.
                Defaults to dwDeviceClass.CLASS_MONITOR

        Raises:
            OSError: If Win API error is raised.
            TimeoutError: If timeout occurs while waiting for worker response.
            WCSManagerShutdownError: If manager is shutting down.
            WCSWorkerError: If some unspecified error occurs during remote execution.
            RuntimeError: If unrecoverable IPC errors occur.
        """
        self._call_wcs_method(
            "SetUsePerUserProfiles", device_key, new_state, device_class
        )


class WCSManagerProxy:
    """Proxy class for WCSManager to ensure single instance and lazy initialization."""

    _instance = None
    _lock = Lock()

    def _ensure_instance(self) -> None:
        """Ensure that the WCSManager instance is created."""
        if WCSManagerProxy._instance is None:
            with WCSManagerProxy._lock:
                if WCSManagerProxy._instance is None:
                    WCSManagerProxy._instance = WCSManager()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Ensure instance exists before getting attributes."""
        self._ensure_instance()
        return getattr(WCSManagerProxy._instance, name)

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: ANN401
        """Ensure instance exists before setting attributes."""
        self._ensure_instance()
        setattr(WCSManagerProxy._instance, name, value)

    def __delattr__(self, name: str) -> None:
        """Ensure instance exists before deleting attributes."""
        self._ensure_instance()
        delattr(WCSManagerProxy._instance, name)

    def __call__(self, *args, **kwargs) -> WCSManager:
        """Make the proxy callable."""
        self._ensure_instance()
        if callable(WCSManagerProxy._instance):
            return WCSManagerProxy._instance(*args, **kwargs)
        raise TypeError(
            f"'{type(WCSManagerProxy._instance).__name__}' object is not callable"
        )

    def __repr__(self) -> str:
        """String representation of the proxy instance."""
        if WCSManagerProxy._instance is None:
            return "<WCSManagerProxy (not initialized)>"
        return f"<WCSManagerProxy wrapping {WCSManagerProxy._instance!r}>"
