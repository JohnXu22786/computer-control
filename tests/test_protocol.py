"""End-to-end protocol tests: router dispatch, JSON-RPC framing, session lifecycle,
confirmation flow, panic and events."""

import json
import threading
import time
import unittest

from computer_control.protocol import Router, ProtocolError
from computer_control.server import serve_stdio
from computer_control.drivers.dummy import NullDriver
from computer_control.session import Session


class FakeReader:
    def __init__(self, lines):
        self.lines = list(lines)
        self.closed = False

    def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)


class FakeWriter:
    def __init__(self):
        self.buffer = b""

    def write(self, data):
        self.buffer += data
        return len(data)

    def flush(self):
        pass


class RouterFixture(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.event_lock = threading.Lock()
        self.driver = NullDriver(enable_a11y=True)

        def make_session():
            session = Session(emit=self._on_event, driver_factory=lambda cfg: self.driver)
            return session

        self.router = Router(make_session)

    def _on_event(self, event_type, payload=None):
        with self.event_lock:
            self.events.append((event_type, payload))

    def event_types(self):
        with self.event_lock:
            return [t for t, _ in self.events]

    def call(self, method, params=None):
        request = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            request["params"] = params
        response = self.router.handle(request)
        return response

    def start_session(self, params=None):
        return self.call("session.start", params or {})

    def driver_log(self):
        return self.driver.log()


class TestLifecycle(RouterFixture):
    def test_unknown_method(self):
        response = self.call("no.such.method")
        self.assertEqual(response["error"]["code"], -32601)

    def test_call_before_start(self):
        response = self.call("tools.call", {"tool": "screen.capture", "arguments": {}})
        self.assertFalse(response["result"]["ok"])
        self.assertEqual(response["result"]["error"]["code"], "not_started")

    def test_start_stop_cycle(self):
        r = self.start_session()
        self.assertTrue(r["result"]["ok"], r)
        self.assertEqual(r["result"]["result"]["state"], "ready")
        self.assertIn("session.started", self.event_types())

        r = self.call("system.status")
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(r["result"]["result"]["surface"]["display_width_px"], 1920)

        r = self.call("session.stop")
        self.assertTrue(r["result"]["ok"])
        self.assertIn("session.stopped", self.event_types())

        r = self.call("tools.call", {"tool": "screen.capture", "arguments": {}})
        self.assertEqual(r["result"]["error"]["code"], "not_started")

    def test_double_start_rejected(self):
        self.start_session()
        r = self.start_session()
        self.assertFalse(r["result"]["ok"])
        self.assertEqual(r["result"]["error"]["code"], "already_started")

    def test_restart_after_stop(self):
        self.start_session()
        self.call("session.stop")
        r = self.start_session()
        self.assertTrue(r["result"]["ok"])

    def test_configure_deny_rule(self):
        self.start_session()
        r = self.call("session.configure", {
            "safety": {"rules": [{"match": {"tool": "pointer.click"}, "effect": "deny"}]}
        })
        self.assertTrue(r["result"]["ok"])
        r = self.call("tools.call", {"tool": "pointer.click", "arguments": {}})
        self.assertEqual(r["result"]["error"]["code"], "policy_denied")


class TestTools(RouterFixture):
    def test_list_tools_advertises_capabilities(self):
        self.start_session()
        r = self.call("tools.list")
        tools = {t["name"]: t for t in r["result"]["result"]["tools"]}
        self.assertIn("screen.capture", tools)
        self.assertTrue(tools["a11y.snapshot"]["available"])  # dummy driver has a11y
        self.assertIn("risk", tools["keyboard.combo"])
        self.assertGreaterEqual(len(tools), 13)

    def test_call_and_events(self):
        self.start_session()
        r = self.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(r["result"]["result"]["waited_ms"], 1)
        types = self.event_types()
        self.assertIn("action.started", types)
        self.assertIn("action.finished", types)

    def test_call_batch(self):
        self.start_session()
        r = self.call("tools.call_batch", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "screen.capture", "arguments": {}},
            ],
        })
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(r["result"]["result"]["status"], "completed")
        self.assertEqual(len(r["result"]["result"]["items"]), 2)

    def test_validation_error_surface(self):
        self.start_session()
        r = self.call("tools.call", {"tool": "keyboard.type", "arguments": {}})
        self.assertFalse(r["result"]["ok"])  # envelope-level failure
        self.assertEqual(r["result"]["error"]["code"], "invalid_arguments")
        self.assertIn("issues", r["result"]["error"]["data"])

    def test_unknown_tool(self):
        self.start_session()
        r = self.call("tools.call", {"tool": "mystery", "arguments": {}})
        self.assertEqual(r["result"]["error"]["code"], "unknown_tool")


class TestConfirmationOverProtocol(RouterFixture):
    def test_full_flow_approve(self):
        self.start_session({"safety": {"confirm_threshold": "moderate"}})
        r = self.call("tools.call", {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}})
        self.assertTrue(r["result"]["ok"])
        result = r["result"]["result"]
        self.assertEqual(result["status"], "awaiting_confirmation")
        rid = result["request_id"]
        self.assertIn("safety.confirmation_requested", self.event_types())

        self.assertEqual(self.driver_log(), [])  # nothing executed yet

        r = self.call("session.confirm", {"request_id": rid, "approve": True})
        self.assertTrue(r["result"]["ok"])
        self.assertTrue(wait_for(lambda: any(k["kind"] == "combo" for k in self.driver_log())))
        self.assertIn("safety.confirmation_resolved", self.event_types())
        finished = [e for e, p in self.events if e == "action.finished"]
        self.assertGreaterEqual(len(finished), 1)

    def test_deny_flow(self):
        self.start_session({"safety": {"confirm_threshold": "moderate"}})
        r = self.call("tools.call", {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}})
        rid = r["result"]["result"]["request_id"]
        r = self.call("session.confirm", {"request_id": rid, "approve": False})
        self.assertTrue(r["result"]["ok"])
        self.assertFalse(any(k["kind"] == "combo" for k in self.driver_log()))

    def test_unknown_confirmation_id(self):
        self.start_session()
        r = self.call("session.confirm", {"request_id": "bogus", "approve": True})
        self.assertFalse(r["result"]["ok"])
        self.assertEqual(r["result"]["error"]["code"], "confirmation_not_found")

    def test_batch_confirmation(self):
        self.start_session({"safety": {"confirm_threshold": "moderate"}})
        r = self.call("tools.call_batch", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "keyboard.combo", "arguments": {"keys": ["win", "l"]}},
            ],
        })
        result = r["result"]["result"]
        self.assertEqual(result["status"], "awaiting_confirmation")
        rid = result["request_id"]
        self.call("session.confirm", {"request_id": rid, "approve": True})
        self.assertTrue(wait_for(lambda: any(k["kind"] == "combo" for k in self.driver_log())))
        self.assertIn("batch.finished", self.event_types())

    def test_busy_while_confirming(self):
        self.start_session({"safety": {"confirm_threshold": "moderate"}})
        self.call("tools.call", {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}})
        r = self.call("tools.call", {"tool": "wait.pause", "arguments": {"ms": 1}})
        self.assertEqual(r["result"]["error"]["code"], "busy")


class TestPanic(RouterFixture):
    def test_panic_via_protocol(self):
        self.start_session()
        r = self.call("control.panic", {"on": True})
        self.assertTrue(r["result"]["ok"])
        self.assertIn("safety.panic_triggered", self.event_types())
        r = self.call("tools.call", {"tool": "screen.capture", "arguments": {}})
        self.assertEqual(r["result"]["error"]["code"], "safety_stopped")

        r = self.call("control.panic", {"on": False})
        self.assertTrue(r["result"]["ok"])
        self.assertIn("safety.panic_released", self.event_types())
        r = self.call("tools.call", {"tool": "screen.capture", "arguments": {}})
        self.assertTrue(r["result"]["ok"])

    def test_resume_method(self):
        self.start_session()
        self.call("control.panic", {"on": True})
        r = self.call("session.resume")
        self.assertTrue(r["result"]["ok"])
        r = self.call("tools.call", {"tool": "screen.capture", "arguments": {}})
        self.assertTrue(r["result"]["ok"])


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestFraming(unittest.TestCase):
    def test_serve_stdio_roundtrip(self):
        session_holder = {}

        def make_session():
            session = Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True))
            session_holder["session"] = session
            return session

        router = Router(make_session)
        reader = FakeReader([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}}).encode("utf-8") + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "system.status", "params": {}}).encode("utf-8") + b"\n",
        ])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        lines = [json.loads(line) for line in writer.buffer.decode("utf-8").strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["id"], 1)
        self.assertTrue(lines[0]["result"]["ok"])
        self.assertEqual(lines[1]["id"], 2)
        self.assertEqual(lines[1]["result"]["result"]["state"], "ready")

    def test_parse_error_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b"{this is not json}\n"])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        lines = writer.buffer.decode("utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertEqual(response["error"]["code"], -32700)

    def test_missing_id_notification_gets_no_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"2.0","method":"system.status","params":{}}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        self.assertEqual(writer.buffer, b"")

    def test_bad_params_gets_error_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"2.0","id":3,"method":"tools.call","params":"not-an-object"}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        response = json.loads(writer.buffer.decode("utf-8").strip())
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_method_over_stream(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"2.0","id":4,"method":"mystery","params":{}}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        response = json.loads(writer.buffer.decode("utf-8").strip())
        self.assertEqual(response["error"]["code"], -32601)

    def test_non_object_json_lines_handled_gracefully(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b"123\n", b"[1, 2, 3]\n", b'"just a string"\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        lines = [json.loads(line) for line in writer.buffer.decode("utf-8").strip().splitlines()]
        self.assertEqual(len(lines), 3)
        for resp in lines:
            self.assertEqual(resp["error"]["code"], -32600)
            self.assertIsNone(resp["id"])

    def test_invalid_jsonrpc_version_preserves_id(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"1.0","id":5,"method":"system.status"}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        response = json.loads(writer.buffer.decode("utf-8").strip())
        self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(response["id"], 5)


class TestProtocolHelpers(unittest.TestCase):
    def test_standard_error_codes(self):
        from computer_control.protocol import (
            PARSE_ERROR,
            INVALID_REQUEST,
            METHOD_NOT_FOUND,
            INVALID_PARAMS,
            INTERNAL_ERROR,
            SERVER_ERROR,
        )
        self.assertEqual(PARSE_ERROR, -32700)
        self.assertEqual(INVALID_REQUEST, -32600)
        self.assertEqual(METHOD_NOT_FOUND, -32601)
        self.assertEqual(INVALID_PARAMS, -32602)
        self.assertEqual(INTERNAL_ERROR, -32603)
        self.assertEqual(SERVER_ERROR, -32000)

    def test_protocol_error_shapes(self):
        err = ProtocolError("boom", data={"x": 1})
        self.assertEqual(err.code, -32603)
        self.assertEqual(err.message, "boom")
        self.assertEqual(err.data, {"x": 1})
        self.assertEqual(err.as_jsonrpc(), {"code": -32603, "message": "boom", "data": {"x": 1}})


if __name__ == "__main__":
    unittest.main()
