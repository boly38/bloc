"""Daemon workers and cooperative cancellation for bounded bridge shutdown."""

import queue
import sys
import threading


_CONTEXT = threading.local()


class ShutdownRequested(BaseException):
    """Stop background work without treating shutdown as a request failure."""


def check_shutdown():
    stop = getattr(_CONTEXT, "stop", None)
    if stop is not None and stop.is_set():
        raise ShutdownRequested()


def wait(seconds):
    stop = getattr(_CONTEXT, "stop", None)
    if stop is None:
        threading.Event().wait(seconds)
    elif stop.wait(seconds):
        raise ShutdownRequested()


class RequestWorkers:
    """Run requests without Python's executor exit hook waiting on blocked sockets."""

    def __init__(self, max_workers=4):
        self._tasks = queue.Queue(maxsize=32)
        for index in range(max_workers):
            threading.Thread(
                target=self._work,
                name=f"telegram-request-{index}",
                daemon=True,
            ).start()

    def submit(self, function, *args):
        check_shutdown()
        self._tasks.put_nowait((getattr(_CONTEXT, "stop", None), function, args))

    def _work(self):
        while True:
            stop, function, args = self._tasks.get()
            _CONTEXT.stop = stop
            try:
                function(*args)
            except ShutdownRequested:
                pass
            except Exception as error:
                print(f"request_worker_failed error_type={type(error).__name__}", file=sys.stderr, flush=True)
            finally:
                _CONTEXT.stop = None
                self._tasks.task_done()


def run_until_stopped(stop, function, *args):
    """Keep the main thread responsive even when background HTTP calls cannot be interrupted."""
    outcome = queue.Queue(maxsize=1)

    def run():
        _CONTEXT.stop = stop
        try:
            function(*args)
        except ShutdownRequested:
            outcome.put(None)
        except BaseException as error:
            outcome.put(error)
        else:
            outcome.put(None)

    thread = threading.Thread(target=run, name="telegram-polling", daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            try:
                error = outcome.get(timeout=0.1)
            except queue.Empty:
                continue
            if error is not None:
                raise error
            return
    finally:
        stop.set()
        # Allow cooperative cleanup, but never wait for a stalled network request.
        thread.join(timeout=0.2)
