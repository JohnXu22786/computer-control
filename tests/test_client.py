"""Client layer tests: StdioClient and HttpClient round-trips against a real
(dry-run) server.

These tests spawn the actual plugin server process and speak the line-delimited
protocol through client.py, exactly like the demo script and embedded harnesses
do. They guard the transport contract: envelope shape, events on the wire,
confirmation flow and the "call before session.start" error path.
"""

import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

from computer_control.client import ClientError, HttpClient, StdioClient


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class BaseServerCase(unittest.TestCase):
    command = None  # None -> default `python -m computer_control serve`

    def setUp(self):
        self.client = StdioClient(command=self.command)

    def tearDown(self):
        self.client.close()


class TestStdioClient(BaseServerCase):
    def start_session(self, overrides=None):
        params = {"platform": {"name": "dry-run"}}
        if overrides:
            for key, value in overrides.items():
                params[key] = value
        return self.client.call("session.start", {"config": params})

    def test_session_lifecycle(self):
        result = self.start_session()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["state"], "ready")
        self.assertTrue(result["result"]["capabilities"]["capture"])
        stop = self.client.call("session.stop")
        self.assertTrue(stop["ok"])

    def test_call_before_start_returns_not_started(self):
        result = self.client.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "not_started")

    def test_tool_call_round_trip(self):
        self.start_session()
        result = self.client.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["waited_ms"], 1)
        self.assertEqual(result["meta"]["tool"], "wait.pause")

    def test_capture_returns_data_url(self):
        self.start_session()
        result = self.client.call("tools.call",
                                  {"tool": "screen.capture", "arguments": {"scale": 0.5}})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(result["result"]["width"], int(1920 * 0.5))

    def test_confirmation_flow(self):
        result = self.start_session({"safety": {"confirm_threshold": "moderate"}})
        self.assertTrue(result["ok"], result)
        combo = self.client.call("tools.call", {"tool": "keyboard.combo",
                                                "arguments": {"keys": ["win", "r"]}})
        self.assertTrue(combo["ok"])
        self.assertEqual(combo["result"]["status"], "awaiting_confirmation")
        request_id = combo["result"]["request_id"]
        denied = self.client.call("session.confirm", {"request_id": request_id, "approve": False})
        self.assertTrue(denied["ok"])
        self.assertEqual(denied["result"]["status"], "denied")

    def test_batch_call(self):
        self.start_session()
        batch = self.client.call("tools.call_batch", {"items": [
            {"tool": "wait.pause", "arguments": {"ms": 1}},
            {"tool": "pointer.move", "arguments": {"x": 400, "y": 300}},
        ]})
        self.assertTrue(batch["ok"], batch)
        self.assertEqual(batch["result"]["status"], "completed")
        self.assertTrue(all(item["ok"] for item in batch["result"]["items"]))

    def test_unknown_method_unavailable(self):
        # tools.panic is not a protocol method -> JSON-RPC error surfaces as ClientError
        with self.assertRaises(ClientError):
            self.client.call("tools.panic", {})


class EventCollector(threading.Thread):
    """Pulls events off a client in the background into a shared list."""

    def __init__(self, client, storage):
        super().__init__(daemon=True)
        self._client = client
        self._storage = storage
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        import queue as queue_module

        while not self._stop.is_set():
            try:
                self._storage.append(self._client.events(block=True, timeout=0.1))
            except queue_module.Empty:
                continue
            except Exception:
                return


class TestRequestIds(unittest.TestCase):
    """Request ids must be unique across client instances so two clients
    talking to the same server never collide (round-3 regression)."""

    def test_ids_differ_across_instances(self):
        from computer_control.client import _BaseClient

        first = _BaseClient.__new__(_BaseClient)
        second = _BaseClient.__new__(_BaseClient)
        self.assertNotEqual(first._next_id(), second._next_id())

    def test_ids_are_monotonic_within_an_instance(self):
        from computer_control.client import _BaseClient

        client = _BaseClient.__new__(_BaseClient)
        ids = {client._next_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_stdio_call_honors_explicit_zero_and_default_timeout(self):
        from computer_control.client import _BaseClient

        class FakeEvent:
            def __init__(self):
                self.wait_timeout = None

            def wait(self, timeout):
                self.wait_timeout = timeout
                return False

        class FakeStdin:
            def write(self, _payload):
                pass

            def flush(self):
                pass

        class FakeProcess:
            stdin = FakeStdin()

        for requested_timeout, expected_timeout in ((0, 0), (None, 0.02)):
            with self.subTest(requested_timeout=requested_timeout):
                client = StdioClient.__new__(StdioClient)
                _BaseClient.__init__(client, timeout=0.02)
                client._process = FakeProcess()
                client._responses = {}
                client._pending = {}
                client._lock = threading.Lock()
                event = FakeEvent()
                with patch("computer_control.client.threading.Event", return_value=event):
                    with self.assertRaisesRegex(ClientError, "no response within"):
                        client.call("test", timeout=requested_timeout)
                self.assertEqual(event.wait_timeout, expected_timeout)

    def test_stdio_discards_response_after_request_times_out(self):
        class FakeStdout:
            def __init__(self):
                self.lines = queue.Queue()
                self.eof_read = threading.Event()

            def readline(self):
                line = self.lines.get(timeout=1)
                if not line:
                    self.eof_read.set()
                return line

            def close(self):
                pass

        class FakeStdin:
            def write(self, _payload):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProcess:
            def __init__(self):
                self.stdout = FakeStdout()
                self.stdin = FakeStdin()

        from computer_control.client import _BaseClient

        client = StdioClient.__new__(StdioClient)
        _BaseClient.__init__(client, timeout=0.01)
        client._process = FakeProcess()
        client._responses = {}
        client._pending = {}
        client._lock = threading.Lock()
        client._reader = threading.Thread(target=client._read_loop, daemon=True)
        client._reader.start()
        request = {"jsonrpc": "2.0", "id": 123, "method": "test"}

        try:
            with self.assertRaisesRegex(ClientError, "no response within"):
                client._transact(request, timeout=0.01)

            response = {"jsonrpc": "2.0", "id": 123, "result": {"ok": True}}
            client._process.stdout.lines.put(json.dumps(response).encode("utf-8") + b"\n")
            client._process.stdout.lines.put(b"")
            self.assertTrue(client._process.stdout.eof_read.wait(timeout=1))
            self.assertEqual(client._responses, {})
        finally:
            client._reader.join(timeout=1)


class TestEventsTransport(unittest.TestCase):
    def test_stdio_events_collected_by_thread(self):
        collected = []
        collector = None
        client = None
        try:
            client = StdioClient()
            collector = EventCollector(client, collected)
            collector.start()
            result = client.call("session.start", {"config": {"platform": {"name": "dry-run"}}})
            self.assertTrue(result["ok"], result)
            client.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
            self.assertTrue(wait_for(lambda: any(e.get("type") == "action.finished" for e in collected)))
        finally:
            if collector is not None:
                collector.stop()
            if client is not None:
                client.close()


class TestHttpClient(unittest.TestCase):
    def setUp(self):
        from computer_control.protocol import Router
        from computer_control.server import make_http_server
        from computer_control.drivers.dummy import NullDriver

        self.router = Router(driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        self.server = make_http_server(self.router, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_http_round_trip_and_sse_events(self):
        collected = []
        client = HttpClient("http://127.0.0.1:%d" % self.port, timeout=5)
        collector = EventCollector(client, collected)
        collector.start()
        try:
            result = client.call("session.start", {"platform": {"name": "dry-run"}})
            self.assertTrue(result["ok"], result)
            call = client.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
            self.assertTrue(call["ok"], call)
            self.assertTrue(wait_for(lambda: any(e.get("type") == "action.finished" for e in collected)))
        finally:
            collector.stop()
            client.close()

    def test_http_call_honors_explicit_zero_and_default_timeout(self):
        from computer_control.client import _BaseClient

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self):
                return b'{"jsonrpc":"2.0","result":{"ok":true}}'

        client = HttpClient.__new__(HttpClient)
        _BaseClient.__init__(client, timeout=0.02)
        client._base_url = "http://example.test"
        with patch("computer_control.client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            for requested_timeout, expected_timeout in ((0, 0), (None, 0.02)):
                with self.subTest(requested_timeout=requested_timeout):
                    self.assertEqual(client.call("test", timeout=requested_timeout), {"ok": True})
                    self.assertEqual(urlopen.call_args.kwargs["timeout"], expected_timeout)
                    urlopen.reset_mock()

    def test_http_client_close_terminates_sse_thread(self):
        client = HttpClient("http://127.0.0.1:%d" % self.port, timeout=5)
        self.assertTrue(client._sse_thread.is_alive())
        client.close()
        self.assertFalse(client._sse_thread.is_alive())

    def test_http_client_retries_failed_sse_connection(self):
        import urllib.error

        class EventResponse:
            def __init__(self):
                self.data = iter(b'data: {"type": "reconnected"}\n\n')
                self.fp = self

            def read(self, size):
                return bytes([next(self.data)]) if self.data else b""

            def close(self):
                pass

        attempts = []

        def open_events(*args, **kwargs):
            attempts.append(None)
            if len(attempts) == 1:
                raise urllib.error.URLError("not ready")
            return EventResponse()

        client = None
        try:
            with patch("computer_control.client.urllib.request.urlopen", side_effect=open_events):
                client = HttpClient("http://example.test", timeout=1)
                self.assertEqual(client.events(timeout=2)["type"], "reconnected")
                self.assertGreaterEqual(len(attempts), 2)
        finally:
            if client is not None:
                client.close()

    def test_http_client_retries_sse_read_failure(self):
        class EventResponse:
            def __init__(self, fail_first_read=False):
                self.data = iter(b'data: {"type": "resumed"}\n\n')
                self.fail_first_read = fail_first_read
                self.fp = self

            def read(self, size):
                if self.fail_first_read:
                    self.fail_first_read = False
                    raise OSError("stream dropped")
                return bytes([next(self.data)]) if self.data else b""

            def close(self):
                pass

        attempts = []

        def open_events(*args, **kwargs):
            attempts.append(None)
            return EventResponse(fail_first_read=len(attempts) == 1)

        client = None
        try:
            with patch("computer_control.client.urllib.request.urlopen", side_effect=open_events):
                client = HttpClient("http://example.test", timeout=1)
                self.assertEqual(client.events(timeout=2)["type"], "resumed")
                self.assertGreaterEqual(len(attempts), 2)
        finally:
            if client is not None:
                client.close()

    def test_http_client_delivers_crlf_framed_sse_event(self):
        class EventResponse:
            def __init__(self):
                self.data = iter(b'data: {"type": "crlf"}\r\n\r\n')
                self.fp = self

            def read(self, size):
                return bytes([next(self.data)]) if self.data else b""

            def close(self):
                pass

        client = None
        try:
            with patch("computer_control.client.urllib.request.urlopen", return_value=EventResponse()):
                client = HttpClient("http://example.test", timeout=1)
                self.assertEqual(client.events(timeout=2)["type"], "crlf")
        finally:
            if client is not None:
                client.close()

    def test_http_client_close_stops_sse_retries(self):
        import urllib.error

        attempts = []

        def open_events(*args, **kwargs):
            attempts.append(None)
            raise urllib.error.URLError("not ready")

        with patch("computer_control.client.urllib.request.urlopen", side_effect=open_events):
            client = HttpClient("http://example.test", timeout=1)
            self.assertTrue(wait_for(lambda: len(attempts) >= 2))
            client.close()
            count_at_close = len(attempts)
            time.sleep(0.3)
            self.assertEqual(len(attempts), count_at_close)
            self.assertFalse(client._sse_thread.is_alive())

    def test_http_client_close_during_connection_attempt(self):
        from computer_control.client import HttpClient

        opening = threading.Event()

        class EventResponse:
            def __init__(self):
                self.fp = self

            def close(self):
                pass

        def open_events(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 1.0)
            opening.set()
            time.sleep(0.2)  # Simulate an in-flight connection attempt.
            return EventResponse()

        with patch("computer_control.client.urllib.request.urlopen", side_effect=open_events):
            client = HttpClient("http://example.test", timeout=1)
            self.assertTrue(opening.wait(1))
            started = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertFalse(client._sse_thread.is_alive())

    def test_http_client_connection_failure_raises_client_error(self):
        # Port 1 is almost certainly closed/unreachable
        client = HttpClient("http://127.0.0.1:1", timeout=1.0)
        try:
            with self.assertRaises(ClientError) as ctx:
                client.call("system.status")
            self.assertIn("connection failed", str(ctx.exception).lower())
        finally:
            client.close()

    def test_base_client_malformed_response_raises_client_error(self):
        from computer_control.client import _BaseClient

        class DummyClient(_BaseClient):
            def __init__(self, fake_resp):
                super().__init__()
                self._fake_resp = fake_resp

            def _transact(self, request, timeout):
                return self._fake_resp

        client1 = DummyClient("not a dict")
        with self.assertRaises(ClientError):
            client1.call("system.status")

        client2 = DummyClient({"jsonrpc": "2.0", "id": 1})  # missing result & error
        with self.assertRaises(ClientError):
            client2.call("system.status")


if __name__ == "__main__":
    unittest.main()
