"""Registers functions to be called if an exception or signal occurs."""
import functools
import logging
import os
import signal
import traceback

from certbot import errors

logger = logging.getLogger(__name__)


# _SIGNALS stores the signals that will be handled by the ErrorHandler. These
# signals were chosen as their default handler terminates the process and could
# potentially occur from inside Python. Signals such as SIGILL were not
# included as they could be a sign of something devious and we should terminate
# immediately.
_SIGNALS = [signal.SIGTERM]
if os.name != "nt":
    for signal_code in [signal.SIGHUP, signal.SIGQUIT,
                        signal.SIGXCPU, signal.SIGXFSZ]:
        # Adding only those signals that their default action is not Ignore.
        # This is platform-dependent, so we check it dynamically.
        if signal.getsignal(signal_code) != signal.SIG_IGN:
            _SIGNALS.append(signal_code)


class ErrorHandler(object):
    """Context manager for running code that must be cleaned up on failure.

    The context manager allows you to register functions that will be called
    when an exception (excluding SystemExit) or signal is encountered.
    Usage::

        handler = ErrorHandler(cleanup1_func, *cleanup1_args, **cleanup1_kwargs)
        handler.register(cleanup2_func, *cleanup2_args, **cleanup2_kwargs)

        with handler:
            do_something()

    Or for one cleanup function::

        with ErrorHandler(func, args, kwargs):
            do_something()

    If an exception is raised out of do_something, the cleanup functions will
    be called in last in first out order. Then the exception is raised.
    Similarly, if a signal is encountered, the cleanup functions are called
    followed by the previously received signal handler.

    Each registered cleanup function is called exactly once. If a registered
    function raises an exception, it is logged and the next function is called.
    Signals received while the registered functions are executing are
    deferred until they finish.

    """
    def __init__(self, func=None, *args, **kwargs):
        self.body_executed = False
        self.funcs = []
        self.prev_handlers = {}
        self.received_signals = []
        if func is not None:
            self.register(func, *args, **kwargs)

    def __enter__(self):
        self.body_executed = False
        self._set_signal_handlers()

    def __exit__(self, exec_type, exec_value, trace):
        self.body_executed = True
        retval = False
        # SystemExit is ignored to properly handle forks that don't exec
        if exec_type in (None, SystemExit):
            return retval
        elif exec_type is errors.SignalExit:
            logger.debug("Encountered signals: %s", self.received_signals)
            retval = True
        else:
            logger.debug("Encountered exception:\n%s", "".join(
                traceback.format_exception(exec_type, exec_value, trace)))

        self._call_registered()
        self._reset_signal_handlers()
        self._call_signals()
        return retval

    def register(self, func, *args, **kwargs):
        """Sets func to be run with the given arguments during cleanup.

        :param function func: function to be called in case of an error

        """
        self.funcs.append(functools.partial(func, *args, **kwargs))

    def _call_registered(self):
        """Calls all registered functions"""
        logger.debug("Calling registered functions")
        while self.funcs:
            try:
                self.funcs[-1]()
            except Exception as error:  # pylint: disable=broad-except
                logger.error("Encountered exception during recovery")
                logger.exception(error)
            self.funcs.pop()

    def _set_signal_handlers(self):
        """Sets signal handlers for signals in _SIGNALS."""
        for signum in _SIGNALS:
            prev_handler = signal.getsignal(signum)
            # If prev_handler is None, the handler was set outside of Python
            if prev_handler is not None:
                self.prev_handlers[signum] = prev_handler
                signal.signal(signum, self._signal_handler)

    def _reset_signal_handlers(self):
        """Resets signal handlers for signals in _SIGNALS."""
        for signum in self.prev_handlers:
            signal.signal(signum, self.prev_handlers[signum])
        self.prev_handlers.clear()

    def _signal_handler(self, signum, unused_frame):
        """Replacement function for handling received signals.

        Store the received signal. If we are executing the code block in
        the body of the context manager, stop by raising signal exit.

        :param int signum: number of current signal

        """
        self.received_signals.append(signum)
        if not self.body_executed:
            raise errors.SignalExit

    def _call_signals(self):
        """Finally call the deferred signals."""
        for signum in self.received_signals:
            logger.debug("Calling signal %s", signum)
            os.kill(os.getpid(), signum)
