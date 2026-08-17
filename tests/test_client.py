"""Client layer tests: StdioClient and HttpClient round-trips against a real
(dry-run) server.

These tests spawn the actual plugin server process and speak the line-delimited
protocol through client.py, exactly like the demo script and embedded harnesses
do. They guard the transport contract: envelope shape, events on the wire,
confirmation flow and the "call before session.start" error path.
"""

import json
import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()