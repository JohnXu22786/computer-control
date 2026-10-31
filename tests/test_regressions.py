"""Regression tests for issues found in review round 1.

Covers: event wiring to transports, notification semantics, EventRing
backpressure, manifest consistency, deny-wins rule ordering, risk alias
classification, immutable config fields, standby/pending interaction,
capture area caps and hotkey behavior from standby.
"""

import http.client
import json
import threading
import time
import unittest
from unittest import mock

from computer_control.config import from_dict
from computer_control.policy import SafetyGate, EmitRecorder
from computer_control.protocol import Router
from computer_control.server import EventRing, make_http_server, serve_stdio
from computer_control.drivers.dummy import NullDriver
from computer_control.session import Session


class FakeReader:
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)


class BlockingReader:
    def __init__(self):
        self.read_started = threading.Event()
        self.finish = threading.Event()

    def readline(self):
        self.read_started.set()
        self.finish.wait(3)
        return b""


class FakeWriter:
    def __init__(self):
        self.buffer = b""

    def write(self, data):
        self.buffer += data
        return len(data)

    def flush(self):
        pass


class BufferedWriter:
    """Expose writes only after flush, like buffered stdio output."""

    def __init__(self):
        self.pending = b""
        self.buffer = b""
        self.flush_count = 0

    def write(self, data):
        self.pending += data
        return len(data)

    def flush(self):
        self.buffer += self.pending
        self.pending = b""
        self.flush_count += 1


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestEventWiring(unittest.TestCase):
    """Events must reach the wire through the router's default factory."""

    def _router(self):
        return Router(driver_factory=lambda cfg: NullDriver(enable_a11y=True))

    def test_stdio_emits_event_notifications(self):
        router = self._router()  # default factory, sink installed by serve_stdio
        reader = FakeReader([
            b'{"jsonrpc":"2.0","id":1,"method":"session.start","params":{}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools.call","params":{"tool":"wait.pause","arguments":{"ms":1}}}\n',
        ])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        lines = [json.loads(l) for l in writer.buffer.decode("utf-8").strip().splitlines()]
        events = [l for l in lines if "method" in l and l["method"] == "event"]
        types = [e["params"]["type"] for e in events]
        self.assertIn("session.started", types)
        self.assertIn("action.started", types)
        self.assertIn("action.finished", types)
        # responses still present for the requests
        self.assertEqual(len([l for l in lines if "id" in l and l.get("id") is not None]), 2)

    def test_confirmation_event_reaches_wire(self):
        router = self._router()
        reader = FakeReader([
            b'{"jsonrpc":"2.0","id":1,"method":"session.start","params":{"safety":{"confirm_threshold":"moderate"}}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools.call","params":{"tool":"keyboard.combo","arguments":{"keys":["win","r"]}}}\n',
        ])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        lines = [json.loads(l) for l in writer.buffer.decode("utf-8").strip().splitlines()]
        events = [l["params"]["type"] for l in lines if l.get("method") == "event"]
        self.assertIn("safety.confirmation_requested", events)
        self.assertNotIn("safety.panic_triggered", events)

    def test_stdio_notification_flushes_buffered_output(self):
        router = self._router()
        reader = BlockingReader()
        writer = BufferedWriter()
        server_thread = threading.Thread(target=serve_stdio, args=(router, reader, writer))
        server_thread.start()

        try:
            self.assertTrue(reader.read_started.wait(2))
            router._event_sink("confirmation.timeout", {"id": "pending"})

            self.assertEqual(writer.pending, b"")
            self.assertEqual(writer.flush_count, 1)
            notification = json.loads(writer.buffer.decode("utf-8"))
            self.assertEqual(notification["method"], "event")
            self.assertEqual(notification["params"]["type"], "confirmation.timeout")
        finally:
            reader.finish.set()
            server_thread.join(timeout=2)

        self.assertFalse(server_thread.is_alive())


class TestNotificationSemantics(unittest.TestCase):
    def test_unknown_method_notification_gets_no_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None,
                                        driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"2.0","method":"mystery","params":{}}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        self.assertEqual(writer.buffer, b"")

    def test_valid_notification_executes_but_no_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None,
                                        driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b'{"jsonrpc":"2.0","method":"system.status","params":{}}\n'])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        self.assertEqual(writer.buffer, b"")

    def test_parse_error_gets_null_id_response(self):
        router = Router(lambda: Session(emit=lambda t, p=None: None,
                                        driver_factory=lambda cfg: NullDriver(enable_a11y=True)))
        reader = FakeReader([b"{not json}\n"])
        writer = FakeWriter()
        serve_stdio(router, reader, writer)
        response = json.loads(writer.buffer.decode("utf-8").strip())
        self.assertEqual(response["id"], None)
        self.assertEqual(response["error"]["code"], -32700)


class TestEventRing(unittest.TestCase):
    def test_slow_subscriber_does_not_block_publish(self):
        ring = EventRing(size=20)
        ring.subscribe()
        # fill the subscriber queue completely (maxsize 500)
        for _ in range(600):
            ring.publish("e", None)
        # the slow subscriber's queue is full; publishing must not block
        started = time.monotonic()
        ring.publish("e", None)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreater(ring.stats()["dropped"], 0)

    def test_ring_bounded(self):
        ring = EventRing(size=5)
        for i in range(20):
            ring.publish("e%d" % i, None)
        self.assertEqual(len(ring.recent()), 5)


class TestSSEReplayLiveBoundary(unittest.TestCase):
    def test_live_event_with_replay_tail_timestamp_is_delivered(self):
        router = Router(driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        server = make_http_server(router, "127.0.0.1", 0)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)

        try:
            with mock.patch("computer_control.server.time.time", return_value=123.0):
                router._event_ring.publish("replay.tail", {"n": 1})

            connection.request("GET", "/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)

            def read_event():
                lines = []
                while True:
                    line = response.readline().decode("utf-8").rstrip("\r\n")
                    if not line:
                        return lines
                    lines.append(line)

            replay = read_event()
            self.assertTrue(any('"type": "replay.tail"' in line for line in replay))

            with mock.patch("computer_control.server.time.time", return_value=123.0):
                router._event_ring.publish("live.equal_timestamp", {"n": 2})

            live = read_event()
            self.assertTrue(any('"type": "live.equal_timestamp"' in line for line in live))
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertFalse(server_thread.is_alive())


class TestManifestConsistency(unittest.TestCase):
    def test_manifest_file_matches_generated(self):
        import os
        from computer_control.manifest import manifest

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifest.json")
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(manifest(), on_disk)

    def test_tool_parameters_have_names(self):
        from computer_control.manifest import manifest_tools

        for tool in manifest_tools():
            for param in tool["parameters"]:
                self.assertIn("name", param, tool["name"])


class TestRuleOrdering(unittest.TestCase):
    def test_deny_wins_regardless_of_order(self):
        # allow listed FIRST must not shadow a later deny
        rules = [
            {"match": {"tool": "keyboard.*"}, "effect": "allow"},
            {"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"},
        ]
        gate = SafetyGate(from_dict({"safety": {"rules": rules}}).safety,
                          emit=EmitRecorder(), driver_hotkey_probe=lambda keys: False)
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["win", "l"]}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["ctrl", "c"]}).decision, "allow")
        self.assertEqual(gate.evaluate("keyboard.type", {"text": "x"}).decision, "allow")

    def test_deny_beats_earlier_allow_and_default(self):
        rules = [
            {"match": {"tool": "pointer.click"}, "effect": "deny"},
            {"match": {"tool": "pointer.*"}, "effect": "allow"},
        ]
        gate = SafetyGate(from_dict({"safety": {"rules": rules}}).safety,
                          emit=EmitRecorder(), driver_hotkey_probe=lambda keys: False)
        self.assertEqual(gate.evaluate("pointer.click", {}).decision, "deny")
        self.assertEqual(gate.evaluate("pointer.move", {"x": 1, "y": 1}).decision, "allow")


class TestRiskAliases(unittest.TestCase):
    def test_alias_keys_cannot_dodge_high_risk(self):
        from computer_control.actions import risk_for

        for keys in (["lwin", "r"], ["rwin", "l"], ["super", "r"], ["meta", "r"], ["ctrl", "lalt", "x"]):
            self.assertEqual(risk_for("keyboard.combo", {"keys": keys}), "high", keys)
        self.assertEqual(risk_for("keyboard.combo", {"keys": ["lctrl", "ralt", "x"]}), "high")
        self.assertEqual(risk_for("keyboard.combo", {"keys": ["ctrl", "shift"]}), "moderate")

    def test_alias_matches_contains_rule(self):
        rules = [{"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"}]
        gate = SafetyGate(from_dict({"safety": {"rules": rules}}).safety,
                          emit=EmitRecorder(), driver_hotkey_probe=lambda keys: False)
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["lwin", "l"]}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["super", "d"]}).decision, "deny")


class TestImmutableConfig(unittest.TestCase):
    def setUp(self):
        self.session = Session(emit=lambda t, p=None: None,
                               driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        self.session.start({})

    def tearDown(self):
        try:
            self.session.stop()
        except Exception:
            pass

    def test_immutable_fields_rejected(self):
        for patch in ({"safety": {"emergency_hotkey": "ctrl+alt+q"}},
                      {"safety": {"panic_file": "C:\\tmp\\panic"}},
                      {"safety": {"visual_indicator": False}},
                      {"capture": {"backend": "pillow"}}):
            from computer_control.session import SessionError

            with self.assertRaises(SessionError) as ctx:
                self.session.configure(patch)
            self.assertEqual(ctx.exception.code, "invalid_config", patch)

    def test_rules_still_configurable(self):
        result = self.session.configure({"safety": {"rules": [{"match": {"tool": "pointer.click"}, "effect": "deny"}]}})
        self.assertTrue(result["ok"])
        r = self.session.call("pointer.click", {})
        self.assertEqual(r["error"]["code"], "policy_denied")


class TestStandbyInteraction(unittest.TestCase):
    def test_watchdog_does_not_enter_standby_while_confirmation_pending(self):
        cfg = from_dict({"safety": {"idle_timeout_s": 0.1, "idle_action": "standby",
                                    "confirm_threshold": "moderate"}})
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        gate.start_watchdog()
        try:
            v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
            self.assertEqual(v.decision, "confirm")
            time.sleep(0.3)  # way past the idle timeout
            self.assertEqual(gate.state, "confirming")
            self.assertNotIn("session.idle", emit.types())
            gate.resolve(v.data["request_id"], approve=False)
        finally:
            gate.close()

    def test_panic_in_standby_reports_stopped(self):
        cfg = from_dict({"safety": {"idle_timeout_s": 0.1, "idle_action": "standby"}})
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        gate.start_watchdog()
        try:
            self.assertTrue(wait_for(lambda: gate.state == "standby"))
            gate.trigger_panic(source="hotkey")
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "stopped")
            gate.release_panic(source="hotkey")
            # releasing the panic returns to ready with fresh activity
            self.assertEqual(gate.state, "ready")
        finally:
            gate.close()

    def test_hotkey_resumes_from_standby(self):
        pressed = {"value": False}

        def probe(keys):
            return pressed["value"]

        cfg = from_dict({"safety": {"idle_timeout_s": 0.1, "idle_action": "standby"}})
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=probe)
        gate.start_watchdog()
        try:
            self.assertTrue(wait_for(lambda: gate.state == "standby"))
            pressed["value"] = True
            gate.poll_hotkey()
            self.assertEqual(gate.state, "ready")
            self.assertIn("session.resumed", emit.types())
        finally:
            gate.close()


class TestCaptureAreaCap(unittest.TestCase):
    def test_max_area_shrinks_canvas(self):
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        cfg = from_dict({"capture": {"max_area": 100_000}})
        driver = NullDriver(enable_a11y=True)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)
        r = engine.run_tool("screen.capture", {})
        self.assertTrue(r["ok"], r)
        area = r["result"]["width"] * r["result"]["height"]
        self.assertLessEqual(area, 100_000)

    def test_max_area_applies_to_narrow_tall_region(self):
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        cfg = from_dict({"capture": {"max_area": 100_000}})
        driver = NullDriver(enable_a11y=True)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)

        result = engine.run_tool("screen.capture", {
            "region": {"x": 0, "y": 0, "width": 1, "height": 1000}
        })

        self.assertTrue(result["ok"], result)
        area = result["result"]["width"] * result["result"]["height"]
        self.assertLessEqual(area, cfg.capture.max_area)

    def test_max_area_applies_when_region_height_rounds_to_one_pixel(self):
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        cfg = from_dict({"capture": {"max_area": 1024}})
        driver = NullDriver(enable_a11y=True)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)

        result = engine.run_tool("screen.capture", {
            "region": {"x": 0, "y": 0, "width": 1920, "height": 0.5}
        })

        self.assertTrue(result["ok"], result)
        area = result["result"]["width"] * result["result"]["height"]
        self.assertLessEqual(area, cfg.capture.max_area)

    def test_max_area_caps_extreme_aspect_region_dimensions(self):
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        cfg = from_dict({"capture": {"max_area": 1024}})
        driver = NullDriver(enable_a11y=True)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)

        result = engine.run_tool("screen.capture", {
            "region": {"x": 0, "y": 0, "width": 1, "height": 1080}
        })

        self.assertTrue(result["ok"], result)
        area = result["result"]["width"] * result["result"]["height"]
        self.assertLessEqual(area, cfg.capture.max_area)

    def test_legacy_capture_driver_keeps_working_when_aspect_height_fits(self):
        from computer_control.drivers.base import BaseDriver, CapturePayload
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        class LegacyCaptureDriver(BaseDriver):
            capabilities = {"capture": True}

            def capture(self, *, bbox, canvas_width, format="png", quality=85, grayscale=False):
                left, top, right, bottom = bbox
                height = max(1, int(round((bottom - top) * canvas_width / max(right - left, 1))))
                return CapturePayload(b"capture", canvas_width, height, format)

        cfg = from_dict({"capture": {"max_area": 100_000}})
        driver = LegacyCaptureDriver()
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)

        result = engine.run_tool("screen.capture", {"region": {"x": 0, "y": 0, "width": 100, "height": 100}})

        self.assertTrue(result["ok"], result)

    def test_legacy_capture_driver_rejects_unrepresentable_cap_cleanly(self):
        from computer_control.drivers.base import BaseDriver, CapturePayload
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        class LegacyCaptureDriver(BaseDriver):
            capabilities = {"capture": True}

            def capture(self, *, bbox, canvas_width, format="png", quality=85, grayscale=False):
                left, top, right, bottom = bbox
                height = max(1, int(round((bottom - top) * canvas_width / max(right - left, 1))))
                return CapturePayload(b"capture", canvas_width, height, format)

        cfg = from_dict({"capture": {"max_area": 1024}})
        driver = LegacyCaptureDriver()
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)

        result = engine.run_tool("screen.capture", {
            "region": {"x": 0, "y": 0, "width": 1, "height": 1080}
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "backend_unavailable")
        self.assertIn("canvas height", result["error"]["message"])

    def test_idle_none_emits_once(self):
        cfg = from_dict({"safety": {"idle_timeout_s": 0.1, "idle_action": "none"}})
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        gate.start_watchdog()
        try:
            time.sleep(0.4)
            self.assertEqual(emit.types().count("session.idle"), 1)
        finally:
            gate.close()


class TestStopSemantics(unittest.TestCase):
    """Round-2: gate.suspend() must actually block, and queued tasks must be
    answered quickly instead of hanging the caller."""

    def setUp(self):
        self.session = Session(emit=lambda t, p=None: None,
                               driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        self.session.start({})

    def tearDown(self):
        try:
            self.session.stop()
        except Exception:
            pass

    def test_queued_task_answered_after_stop(self):
        # The worker is busy with a long first action while a second action
        # waits in the queue; stopping must answer the queued one immediately.
        results = {}
        import threading as _t

        def submit_first():
            results["first"] = self.session.call("wait.pause", {"ms": 600})

        def submit_second():
            results["second"] = self.session.call("wait.pause", {"ms": 200})

        t1 = _t.Thread(target=submit_first)
        t2 = _t.Thread(target=submit_second)
        t1.start()
        time.sleep(0.05)  # first is now running on the worker
        t2.start()
        time.sleep(0.05)  # second is queued behind it
        self.session.stop()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        # the queued task was answered immediately with safety_stopped
        second = results.get("second")
        self.assertIsNotNone(second)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["code"], "safety_stopped")
        # the in-flight task was allowed to finish
        first = results.get("first")
        self.assertIsNotNone(first)
        self.assertTrue(first["ok"])

    def test_gate_suspended_blocks_everything(self):
        gate = self.session._gate
        gate.suspend()
        v = gate.evaluate("screen.capture", {})
        self.assertEqual(v.decision, "stopped")
        v = gate.evaluate_allow_only("screen.capture", {})
        self.assertEqual(v.decision, "stopped")

    def test_stop_cannot_drain_before_submit_enqueues(self):
        session = self.session
        # Keep the failure path short; _submit applies the 0.5s minimum after
        # adding its normal 60s queueing grace.
        session._cfg.runtime.max_wait_ms = -60_000
        first_started = threading.Event()
        release_first = threading.Event()
        enqueue_started = threading.Event()
        release_enqueue = threading.Event()
        first_result = {}
        second_result = {}
        stop_result = {}
        original_run_tool = session._engine.run_tool
        original_put = session._worker_queue.put

        def blocked_run_tool(tool, arguments):
            first_started.set()
            release_first.wait(3)
            return original_run_tool(tool, arguments)

        def blocked_put(task, *args, **kwargs):
            enqueue_started.set()
            release_enqueue.wait(3)
            return original_put(task, *args, **kwargs)

        session._engine.run_tool = blocked_run_tool

        def submit_first():
            first_result["result"] = session.call("wait.pause", {"ms": 1})

        def submit_second():
            second_result["result"] = session.call("wait.pause", {"ms": 1})

        def stop_session():
            stop_result["result"] = session.stop()

        first_thread = threading.Thread(target=submit_first)
        second_thread = threading.Thread(target=submit_second)
        stop_thread = threading.Thread(target=stop_session)
        try:
            first_thread.start()
            self.assertTrue(first_started.wait(2))
            session._worker_queue.put = blocked_put
            second_thread.start()
            self.assertTrue(enqueue_started.wait(2))

            stop_thread.start()
            # With admission serialized by the session lock, stop must wait
            # until this put completes. On the buggy implementation it sets
            # the stop flag and drains before the delayed put is released.
            stop_started_before_admission = session._stop_requested.wait(0.1)
            if stop_started_before_admission:
                release_first.set()
                first_thread.join(timeout=2)
                stop_thread.join(timeout=2)
                release_enqueue.set()
            else:
                release_enqueue.set()
                self.assertTrue(wait_for(session._stop_requested.is_set, timeout=2))
                release_first.set()
            first_thread.join(timeout=2)
            stop_thread.join(timeout=2)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())

            second_thread.join(timeout=2)
            self.assertFalse(second_thread.is_alive())
            self.assertNotEqual(first_result["result"].get("error", {}).get("code"), "timeout")
            self.assertTrue(stop_result["result"]["ok"])
            self.assertEqual(second_result["result"]["error"]["code"], "safety_stopped")
        finally:
            release_first.set()
            release_enqueue.set()
            for thread in (first_thread, second_thread, stop_thread):
                if thread.ident is not None:
                    thread.join(timeout=2)

    def test_restart_does_not_reanimate_timed_out_worker(self):
        session = self.session
        old_worker = session._worker
        old_queue = session._worker_queue
        old_stop_requested = session._stop_requested
        action_started = threading.Event()
        release_action = threading.Event()
        action_thread_id = {}
        original_run_tool = session._engine.run_tool

        def blocked_run_tool(tool, arguments):
            action_started.set()
            release_action.wait(30)
            return original_run_tool(tool, arguments)

        session._engine.run_tool = blocked_run_tool
        first_result = {}
        first_caller = threading.Thread(
            target=lambda: first_result.update(result=session.call("wait.pause", {"ms": 1}))
        )
        first_caller.start()
        self.assertTrue(action_started.wait(2))

        # Model the bounded join expiring without spending 30 seconds in the
        # regression test. The worker remains blocked in its current action.
        with mock.patch.object(old_worker, "join", return_value=None):
            stopped = session.stop()
        self.assertTrue(stopped["ok"])
        self.assertTrue(old_worker.is_alive())

        allow_new_worker = threading.Event()
        task_queued = threading.Event()
        original_thread_start = threading.Thread.start

        def start_gated_worker(worker):
            # Install the queue gate before the restarted worker can call
            # get(); patching after start would leave a race with its first
            # queue read.
            if worker.name == "session-worker":
                worker_queue = worker._args[0]
                original_get = worker_queue.get

                def wait_for_new_worker(timeout=None):
                    # A stale worker using the new queue will not be gated and
                    # will deterministically take any action already queued.
                    if threading.current_thread() is worker:
                        allow_new_worker.wait()
                    return original_get(timeout=timeout)

                worker_queue.get = wait_for_new_worker
            return original_thread_start(worker)

        with mock.patch.object(threading.Thread, "start", start_gated_worker):
            session.start({})
        new_worker = session._worker
        new_queue = session._worker_queue
        new_stop_requested = session._stop_requested
        original_put = new_queue.put

        def record_put(task, *args, **kwargs):
            result = original_put(task, *args, **kwargs)
            task_queued.set()
            return result

        new_queue.put = record_put
        restarted_engine = session._engine
        restarted_run_tool = restarted_engine.run_tool

        def record_action_thread(tool, arguments):
            action_thread_id["id"] = threading.get_ident()
            return restarted_run_tool(tool, arguments)

        restarted_engine.run_tool = record_action_thread
        new_result = {}
        new_caller = threading.Thread(
            target=lambda: new_result.update(result=session.call("wait.pause", {"ms": 1}))
        )
        new_caller.start()
        try:
            self.assertTrue(task_queued.wait(5))

            release_action.set()
            old_worker.join(timeout=2)
            self.assertFalse(old_worker.is_alive())
            allow_new_worker.set()
            new_caller.join(timeout=2)
            first_caller.join(timeout=2)
            self.assertFalse(first_caller.is_alive())
            self.assertFalse(new_caller.is_alive())
            self.assertIn("result", first_result)
            self.assertTrue(new_result["result"]["ok"])
            self.assertEqual(action_thread_id["id"], new_worker.ident)
            self.assertIsNot(old_queue, new_queue)
            self.assertIsNot(old_stop_requested, new_stop_requested)
        finally:
            release_action.set()
            allow_new_worker.set()
            for thread in (old_worker, first_caller, new_caller, new_worker):
                if thread.ident is not None:
                    thread.join(timeout=2)


class TestConfirmEventOrder(unittest.TestCase):
    def test_event_order_after_approval(self):
        events = []
        session = Session(emit=lambda t, p=None: events.append(t),
                          driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        try:
            session.start({"safety": {"confirm_threshold": "moderate"}})
            r = session.call("keyboard.combo", {"keys": ["win", "r"]})
            rid = r["result"]["request_id"]
            events.clear()
            session.confirm(rid, approve=True)
            self.assertTrue(wait_for(lambda: "action.finished" in events))
            ordered = [e for e in events if e in ("safety.confirmation_resolved", "action.started", "action.finished")]
            self.assertEqual(ordered, ["safety.confirmation_resolved", "action.started", "action.finished"])
        finally:
            session.stop()


class TestBaseConfig(unittest.TestCase):
    def test_base_config_applies_to_session_start(self):
        session = Session(emit=lambda t, p=None: None,
                          driver_factory=lambda cfg: NullDriver(enable_a11y=True),
                          base_config={"capture": {"default_width": 640}})
        try:
            r = session.start({})
            self.assertTrue(r["ok"])
            self.assertEqual(r["result"]["surface"]["display_width_px"], 640)
        finally:
            session.stop()

    def test_start_params_override_base_config(self):
        session = Session(emit=lambda t, p=None: None,
                          driver_factory=lambda cfg: NullDriver(enable_a11y=True),
                          base_config={"capture": {"default_width": 640}})
        try:
            r = session.start({"capture": {"default_width": 1280}})
            self.assertEqual(r["result"]["surface"]["display_width_px"], 1280)
        finally:
            session.stop()

    def test_read_surface_degenerate_virtual_screen_fallback(self):
        class ZeroSizeDriver(NullDriver):
            def desktop_info(self):
                return {"platform": "mock", "virtual_screen": {"x": 0, "y": 0, "width": 0, "height": 0}}

        session = Session(emit=lambda t, p=None: None, driver_factory=lambda cfg: ZeroSizeDriver())
        try:
            r = session.start({})
            self.assertTrue(r["ok"])
            self.assertEqual(r["result"]["surface"]["display_width_px"], 1920)
        finally:
            session.stop()

    def test_session_stopped_emits_once(self):
        events = []
        session = Session(emit=lambda t, p=None: events.append(t),
                          driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        session.start({})
        session.stop()
        self.assertEqual(events.count("session.stopped"), 1)


class TestKeyInjectionDetails(unittest.TestCase):
    def test_numpad_enter_resolves_to_return_with_extended_flag(self):
        from computer_control.keys import parse_key, parse_key_full

        self.assertEqual(parse_key("numpad_enter"), 0x0D)
        vk, extended = parse_key_full("numpad_enter")
        self.assertEqual(vk, 0x0D)
        self.assertTrue(extended)
        vk, extended = parse_key_full("enter")
        self.assertFalse(extended)


class TestRegionFinite(unittest.TestCase):
    def test_nan_inf_region_rejected(self):
        from computer_control.actions import ValidationError, clean_arguments

        with self.assertRaises(ValidationError):
            clean_arguments("screen.capture", {"region": {"x": float("nan"), "y": 0, "width": 10, "height": 10}})
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.drag", {"from": {"x": 0, "y": 0}, "to": {"x": float("inf"), "y": 1}})
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.move", {"x": float("inf"), "y": 1})


class TestBatchConfigDefaults(unittest.TestCase):
    def test_batch_capture_uses_config_defaults(self):
        from computer_control.engine import Engine
        from computer_control.geometry import Surface

        cfg = from_dict({"capture": {"default_format": "jpeg", "grayscale": True}})
        driver = NullDriver(enable_a11y=True)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)
        r = engine.run_tool("batch.execute", {
            "items": [{"tool": "screen.capture", "arguments": {}}], "continue_on_error": True,
        })
        self.assertTrue(r["ok"], r)
        capture = [e for e in driver.log() if e["kind"] == "capture"][0]
        self.assertEqual(capture["format"], "jpeg")
        self.assertTrue(capture["grayscale"])


class TestHttpTransport(unittest.TestCase):
    """Round-2: HTTP transport behavior (204 for notifications, /health)."""

    def setUp(self):
        from computer_control.server import make_http_server

        self.router = Router(driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        self.server = make_http_server(self.router, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, body: bytes):
        import urllib.request

        req = urllib.request.Request("http://127.0.0.1:%d/rpc" % self.port,
                                     data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_notification_gets_204(self):
        status, body = self._post(b'{"jsonrpc":"2.0","method":"system.status","params":{}}')
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_call_gets_200(self):
        status, body = self._post(b'{"jsonrpc":"2.0","id":1,"method":"system.status","params":{}}')
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["result"]["result"]["state"], "idle")

    def test_health(self):
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(json.loads(resp.read())["ok"])

    def test_sse_disconnect_tolerated(self):
        import socket

        # Connect via socket and close immediately to simulate abrupt client disconnect
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        s.recv(128)  # Read status line / initial header bytes
        s.close()
        # Server should continue responding normally
        status, body = self._post(b'{"jsonrpc":"2.0","id":2,"method":"system.status","params":{}}')
        self.assertEqual(status, 200)


class TestCallTimeout(unittest.TestCase):
    """Round-3: the per-call timeout must be derived from runtime.max_wait_ms
    instead of a hardcoded 300s floor, so a stuck action fails fast when
    configured so."""

    def test_timeout_is_derived_from_max_wait_ms(self):
        import threading as _t
        from computer_control.config import from_dict

        recorded = {}

        def recording_wait(self, timeout=None):
            if timeout is not None:
                recorded["timeout"] = timeout
            return original_wait(self, timeout)

        original_wait = _t.Event.wait
        _t.Event.wait = recording_wait
        session = Session(emit=lambda t, p=None: None,
                          driver_factory=lambda cfg: NullDriver(enable_a11y=True))
        try:
            session.start({"runtime": {"max_wait_ms": 200}})
            session.call("pointer.move", {"x": 10, "y": 10})
        finally:
            _t.Event.wait = original_wait
            session.stop()
        # 200ms budget + 60s grace; the old code waited at least 300s.
        self.assertAlmostEqual(recorded.get("timeout"), 60.2)


class TestCheckExitCode(unittest.TestCase):
    def test_check_reports_invalid_config_with_nonzero_exit(self):
        import subprocess
        import sys
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write('{"platform": {"name": "amiga"}}')
            proc = subprocess.run(
                [sys.executable, "-m", "computer_control", "check", "--config", bad],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)


class TestCliCommands(unittest.TestCase):
    def test_config_option_before_or_after_command_overrides_environment(self):
        import os
        import tempfile
        from computer_control import cli

        with tempfile.TemporaryDirectory() as d:
            explicit = os.path.join(d, "explicit.json")
            invalid_env = os.path.join(d, "invalid-env.json")
            with open(explicit, "w", encoding="utf-8") as fh:
                json.dump({"platform": {"name": "dry-run"}}, fh)
            with open(invalid_env, "w", encoding="utf-8") as fh:
                fh.write("{invalid json")

            old_env = os.environ.get("COMPUTER_CONTROL_CONFIG")
            os.environ["COMPUTER_CONTROL_CONFIG"] = invalid_env
            try:
                for argv in (("--config", explicit, "check"),
                             ("check", "--config", explicit)):
                    with self.subTest(argv=argv):
                        args = cli._build_parser().parse_args(argv)
                        self.assertEqual(cli._config(args).platform.name, "dry-run")
            finally:
                if old_env is None:
                    os.environ.pop("COMPUTER_CONTROL_CONFIG", None)
                else:
                    os.environ["COMPUTER_CONTROL_CONFIG"] = old_env

    def test_version_flag(self):
        import subprocess
        import sys
        from computer_control.version import __version__

        proc = subprocess.run(
            [sys.executable, "-m", "computer_control", "--version"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(__version__, proc.stdout + proc.stderr)

    def test_list_command(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-m", "computer_control", "list"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("screen.capture", proc.stdout)

    def test_list_json_command(self):
        import subprocess
        import sys
        import json

        proc = subprocess.run(
            [sys.executable, "-m", "computer_control", "list", "--json"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("tools", data)
        self.assertIn("events", data)


class TestNullDriverCompleteness(unittest.TestCase):
    def test_desktop_info(self):
        driver = NullDriver()
        info = driver.desktop_info()
        self.assertEqual(info["platform"], "dry-run")
        self.assertEqual(info["virtual_screen"]["width"], 1920)
        self.assertEqual(info["virtual_screen"]["height"], 1080)

    def test_hotkey_probe_simulation(self):
        driver = NullDriver()
        self.assertFalse(driver.hotkey_probe(["ctrl", "alt", "f12"]))
        driver.simulate_hotkey_down(["ctrl", "alt", "f12"])
        self.assertTrue(driver.hotkey_probe(["ctrl", "alt", "f12"]))
        self.assertTrue(driver.hotkey_probe(["ctrl"]))
        self.assertFalse(driver.hotkey_probe(["ctrl", "shift"]))
        driver.simulate_hotkey_up()
        self.assertFalse(driver.hotkey_probe(["ctrl", "alt", "f12"]))


class TestManifestParity(unittest.TestCase):
    def test_manifest_file_matches_manifest_generator(self):
        import json
        import os
        from computer_control.manifest import manifest

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        manifest_path = os.path.join(repo_root, "manifest.json")
        self.assertTrue(os.path.exists(manifest_path))
        with open(manifest_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        generated = manifest()
        self.assertEqual(on_disk, generated)


if __name__ == "__main__":
    unittest.main()
