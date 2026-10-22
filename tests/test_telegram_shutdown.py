"""Exercise real signal delivery and process exit with deliberately blocked I/O."""

import fcntl
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bot" / "telegram"
sys.path.insert(0, str(BRIDGE))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "unit-test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge_runtime, http_api
    from bloc_bridge.telegram import polling as telegram_polling


SCENARIO = textwrap.dedent("""
    import runpy
    import os
    import socket
    import sys
    import threading
    from unittest.mock import patch

    from bloc_bridge import bridge, http_api, request_store, session_state
    from bloc_bridge.opencode import api as opencode_api
    from bloc_bridge.bridge_config import TG

    request_store.REQUESTS_PATH = os.environ["TELEGRAM_TEST_REQUESTS_PATH"]

    scenario = sys.argv[1]
    blocked = threading.Event()
    active = threading.Event()
    polls = 0
    network_http = http_api._http

    if scenario == "socket":
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def stall_response():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                print("READY", flush=True)
                blocked.wait(60)

        threading.Thread(target=stall_response, daemon=True).start()

    def block():
        print("READY", flush=True)
        blocked.wait(60)
        raise AssertionError("Blocking call unexpectedly returned")

    def request(url, payload=None, timeout=None):
        global polls
        if url == TG + "getUpdates":
            polls += 1
            if scenario == "socket":
                return network_http(f"http://127.0.0.1:{server.getsockname()[1]}", timeout=60)
            if scenario == "poll":
                return block()
            if polls == 1:
                count = 8 if scenario == "queued" else 1
                updates = [
                    {
                        "update_id": i,
                        "message": {"chat": {"id": 123}, "text": "/xx" if scenario == "command" else "Hello"},
                    }
                    for i in range(count)
                ]
                return {"ok": True, "result": updates}
            if scenario in ("worker", "queued", "command"):
                if scenario == "command":
                    bridge._REQUEST_EXECUTOR._tasks.join()
                if not active.wait(3):
                    raise AssertionError("Worker did not start")
                return block()
            return blocked.wait(60)
        if url == TG + "sendMessage":
            if scenario == "ack":
                return block()
            return {"ok": True, "result": {"message_id": 456}}
        if url == TG + "editMessageText" and scenario == "command":
            active.set()
            return {"ok": True, "result": True}
        raise AssertionError("Unexpected HTTP call")

    def status(timeout):
        active.set()
        blocked.wait(60)
        raise AssertionError("Blocking worker unexpectedly returned")

    with patch.object(http_api, "_http", side_effect=request), \\
         patch.object(http_api, "acquire_polling_lock"), \\
         patch.object(session_state, "load_sessions", return_value={"123": "session-1"}), \\
         patch.object(opencode_api, "session_tree_messages", return_value=[]), \\
         patch.object(opencode_api, "submit_async"), \\
         patch.object(opencode_api, "session_status", side_effect=status), \\
         patch.object(opencode_api, "iter_events", return_value=iter(())):
        runpy.run_module("bloc_bridge", run_name="__main__")
    print("STOPPED", flush=True)
""")


class TelegramShutdownTests(unittest.TestCase):
    def test_sigterm_exits_under_two_seconds_with_blocked_network_and_queued_requests(self):
        for scenario in ("poll", "socket", "ack", "worker", "queued", "command"):
            with self.subTest(scenario=scenario):
                self.assert_clean_shutdown(scenario)

    def assert_clean_shutdown(self, scenario):
        request_directory = tempfile.TemporaryDirectory()
        environment = dict(
            os.environ,
            PYTHONPATH=str(BRIDGE),
            TELEGRAM_BOT_TOKEN="unit-test-token",
            TELEGRAM_ALLOWED_CHAT_IDS="123",
            NO_PROXY="127.0.0.1",
            no_proxy="127.0.0.1",
            TELEGRAM_TEST_REQUESTS_PATH=str(Path(request_directory.name) / "requests.sqlite3"),
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", SCENARIO, scenario],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "Bridge did not reach blocked I/O")
                ready = process.stdout.readline().strip()
                if ready != "READY":
                    stdout, stderr = process.communicate(timeout=2)
                    self.fail(f"Bridge failed before blocked I/O: {ready} {stdout} {stderr}")
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=2)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("Received SIGTERM", stderr)
            self.assertIn("STOPPED", stdout)
            if scenario == "command":
                self.assertIn("request_completed", stderr)
            else:
                self.assertNotIn("request_completed", stderr)
            if scenario == "queued":
                self.assertEqual(stderr.count("request_accepted"), 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            request_directory.cleanup()

    def test_polling_preserves_offset_and_does_not_dispatch_updates_after_shutdown(self):
        stop = threading.Event()
        handler = Mock()
        update = {"update_id": 10}
        offsets = []

        def poll(method, payload):
            self.assertEqual(method, "getUpdates")
            offsets.append(payload["offset"])
            if len(offsets) == 2:
                stop.set()
            return [update]

        with patch.object(http_api, "telegram", side_effect=poll):
            telegram_polling.poll_updates(stop, handler, {})

        self.assertEqual(offsets, [0, 11])
        handler.assert_called_once_with(update, {})

    def test_shutdown_interrupts_polling_lock_wait(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "polling.lock")
            with open(path, "w") as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                timer = threading.Timer(0.1, stop.set)
                timer.start()
                try:
                    started = time.monotonic()
                    with patch.object(http_api, "POLLING_LOCK_PATH", path):
                        self.assertIsNone(http_api.acquire_polling_lock(stop))
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    timer.cancel()
                    timer.join()

    def test_supervisor_propagates_polling_failure(self):
        stop = threading.Event()
        failure = Mock(side_effect=RuntimeError("test polling failure"))
        with self.assertRaisesRegex(RuntimeError, "test polling failure"):
            bridge_runtime.run_until_stopped(stop, failure)
        self.assertTrue(stop.is_set())

    def test_cooperative_shutdown_prevents_new_http_calls(self):
        stop = threading.Event()

        def work():
            stop.set()
            http_api.http("http://unused.invalid")

        with patch.object(http_api, "_http") as request:
            bridge_runtime.run_until_stopped(stop, work)
        request.assert_not_called()

    def test_cooperative_shutdown_stops_processing_after_http_returns(self):
        stop = threading.Event()
        after_request = Mock()

        def request(*_args):
            stop.set()
            return {}

        def work():
            http_api.http("http://unused.invalid")
            after_request()

        with patch.object(http_api, "_http", side_effect=request):
            bridge_runtime.run_until_stopped(stop, work)
        after_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
