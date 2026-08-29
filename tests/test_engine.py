"""Unit tests for the engine: execution, coordinate mapping, policy interaction,
confirmation flow, batch semantics and snapshot staleness."""

import time
import unittest

from computer_control.engine import Engine
from computer_control.geometry import Surface
from computer_control.policy import EmitRecorder, SafetyGate
from computer_control.config import from_dict
from computer_control.drivers.dummy import NullDriver


def make_engine(overrides=None):
    cfg = from_dict(overrides or {})
    driver = NullDriver(enable_a11y=True)
    surface = Surface.from_physical(cfg.capture.default_width, 0, 0, 3840, 2160)
    emit = EmitRecorder()
    gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
    engine = Engine(driver, surface, gate, emit, lambda: cfg)
    # tests resolve confirmations inline on the calling thread
    gate.on_approved = engine._on_confirmation_approved
    return engine, driver, gate, emit, cfg


def wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestCapture(unittest.TestCase):
    def test_capture_returns_data_url(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("screen.capture", {})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(r["result"]["width"], 1920)
        self.assertEqual(r["result"]["height"], 1080)
        self.assertEqual(r["result"]["frame"], 1)
        r2 = engine.run_tool("screen.capture", {})
        self.assertEqual(r2["result"]["frame"], 2)

    def test_capture_region_maps_through_surface(self):
        engine, driver, _, _, _ = make_engine()
        engine.run_tool("screen.capture", {"region": {"x": 100, "y": 50, "width": 10, "height": 20}})
        log = driver.log()
        capture = [e for e in log if e["kind"] == "capture"][0]
        # scale is 2.0 (3840/1920)
        self.assertEqual(capture["bbox"], (200, 100, 220, 140))

    def test_capture_format_jpeg(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("screen.capture", {"format": "jpeg"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["result"]["data_url"].startswith("data:image/jpeg;base64,"))


class TestPointerMapping(unittest.TestCase):
    def test_click_coordinates_scaled(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("pointer.click", {"x": 960, "y": 540})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["position"], {"x": 960, "y": 540})
        # the engine moves first, then clicks; both in physical pixels
        moves = [e for e in driver.log() if e["kind"] == "move"]
        self.assertEqual(moves[0]["position"], (1920.0, 1080.0))
        clicks = [e for e in driver.log() if e["kind"] == "click"]
        self.assertEqual(clicks[0]["button"], "left")

    def test_move_clamped_out_of_canvas(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("pointer.move", {"x": 99999, "y": 5000})
        self.assertTrue(r["ok"], r)
        moves = [e for e in driver.log() if e["kind"] == "move"]
        # clamp in model space (1919, 1079) then scale by 2
        self.assertEqual(moves[0]["position"], (3838.0, 2158.0))

    def test_drag_maps_both_ends(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("pointer.drag", {"from": {"x": 0, "y": 0}, "to": {"x": 100, "y": 100}})
        self.assertTrue(r["ok"], r)
        drags = [e for e in driver.log() if e["kind"] == "drag"]
        self.assertEqual(drags[0]["origin"], (0, 0))
        self.assertEqual(drags[0]["target"], (200, 200))


class TestErrors(unittest.TestCase):
    def test_unknown_tool(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("no.such", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "unknown_tool")

    def test_invalid_arguments(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("keyboard.type", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_arguments")
        self.assertGreater(len(r["error"]["data"]["issues"]), 0)

    def test_policy_denied(self):
        engine, _, _, _, _ = make_engine({
            "safety": {"rules": [{"match": {"tool": "pointer.click"}, "effect": "deny"}]}
        })
        r = engine.run_tool("pointer.click", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "policy_denied")
        self.assertIn("rule", r["error"]["data"])

    def test_a11y_backend_unavailable(self):
        cfg = from_dict({})
        driver = NullDriver(enable_a11y=False)
        surface = Surface.from_physical(1920, 0, 0, 3840, 2160)
        emit = EmitRecorder()
        gate = SafetyGate(cfg.safety, emit=emit, driver_hotkey_probe=lambda keys: False)
        engine = Engine(driver, surface, gate, emit, lambda: cfg)
        r = engine.run_tool("a11y.snapshot", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "backend_unavailable")

    def test_stale_snapshot_rejected(self):
        engine, _, _, _, _ = make_engine()
        first = engine.run_tool("a11y.snapshot", {})
        second = engine.run_tool("a11y.snapshot", {})
        self.assertNotEqual(first["result"]["snapshot_id"], second["result"]["snapshot_id"])
        r = engine.run_tool("a11y.activate", {
            "snapshot_id": first["result"]["snapshot_id"],
            "node_id": 1,
        })
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "stale_snapshot")

    def test_a11y_activate_current_snapshot(self):
        engine, driver, _, _, _ = make_engine()
        snap = engine.run_tool("a11y.snapshot", {"level": "skeleton"})
        r = engine.run_tool("a11y.activate", {
            "snapshot_id": snap["result"]["snapshot_id"],
            "node_id": 1,
        })
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["method_used"], "invoke")

    def test_a11y_activate_unknown_node(self):
        engine, _, _, _, _ = make_engine()
        snap = engine.run_tool("a11y.snapshot", {})
        r = engine.run_tool("a11y.activate", {
            "snapshot_id": snap["result"]["snapshot_id"],
            "node_id": 999999,
        })
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_arguments")


class TestConfirmation(unittest.TestCase):
    def test_awaiting_confirmation_then_approve(self):
        engine, driver, gate, emit, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        self.assertIn("safety.confirmation_requested", emit.types())
        pending = gate.resolve(rid, approve=True)
        self.assertEqual(pending.status, "approved")
        # engine wires its own approval callback: execution happened inline
        combos = [e for e in driver.log() if e["kind"] == "combo"]
        self.assertEqual(combos, [{"kind": "combo", "keys": ["win", "r"]}])

    def test_deny_does_not_execute(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        gate.resolve(r["result"]["request_id"], approve=False)
        time.sleep(0.05)
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])

    def test_expired_confirmation_approval_errors(self):
        engine, driver, gate, _, _ = make_engine({
            "safety": {"confirm_threshold": "moderate", "confirm_timeout_s": 0.05}
        })
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        time.sleep(0.12)
        with self.assertRaises(Exception) as ctx:
            gate.resolve(r["result"]["request_id"], approve=True)
        self.assertEqual(getattr(ctx.exception, "code", None), "confirmation_expired")
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])


class TestWait(unittest.TestCase):
    def test_wait_pause(self):
        engine, _, _, _, _ = make_engine()
        t0 = time.monotonic()
        r = engine.run_tool("wait.pause", {"ms": 50})
        elapsed = time.monotonic() - t0
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["result"]["waited_ms"], 50)
        self.assertGreaterEqual(elapsed, 0.04)


class TestBatch(unittest.TestCase):
    def test_batch_stops_on_error(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "no.such.tool", "arguments": {}},
                {"tool": "wait.pause", "arguments": {"ms": 1}},
            ],
            "continue_on_error": False,
        })
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["status"], "aborted")
        self.assertTrue(r["result"]["items"][0]["ok"])
        self.assertFalse(r["result"]["items"][1]["ok"])
        self.assertEqual(len(r["result"]["items"]), 2)

    def test_batch_continues_on_error(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "no.such.tool", "arguments": {}},
                {"tool": "wait.pause", "arguments": {"ms": 1}},
            ],
            "continue_on_error": True,
        })
        self.assertEqual(r["result"]["status"], "completed")
        self.assertEqual(len(r["result"]["items"]), 3)

    def test_batch_policy_denial_midway(self):
        engine, _, _, _, _ = make_engine({
            "safety": {"rules": [{"match": {"tool": "pointer.click"}, "effect": "deny"}]}
        })
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "pointer.click", "arguments": {}},
                {"tool": "wait.pause", "arguments": {"ms": 1}},
            ],
            "continue_on_error": True,
        })
        self.assertEqual(r["result"]["status"], "completed")
        self.assertFalse(r["result"]["items"][1]["ok"])
        self.assertEqual(r["result"]["items"][1]["error"]["code"], "policy_denied")

    def test_batch_confirmation_whole_batch(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}},
            ],
        })
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        gate.resolve(rid, approve=True)
        combos = [e for e in driver.log() if e["kind"] == "combo"]
        self.assertEqual(len(combos), 1)
        self.assertIn("batch.finished", emit_types(engine))

    def test_batch_confirmation_preserves_subsequent_items(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "wait.pause", "arguments": {"ms": 1}},
                {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}},
                {"tool": "pointer.move", "arguments": {"x": 500, "y": 300}},
            ],
        })
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        gate.resolve(rid, approve=True)
        combos = [e for e in driver.log() if e["kind"] == "combo"]
        self.assertEqual(len(combos), 1)
        moves = [e for e in driver.log() if e["kind"] == "move"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["position"], (1000.0, 600.0))

    def test_batch_with_invalid_subsequent_item_and_continue_on_error(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}},
                {"tool": "unknown.tool.name", "arguments": {}},
                {"tool": "pointer.move", "arguments": {"x": 100, "y": 100}},
            ],
            "continue_on_error": True,
        })
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        gate.resolve(rid, approve=True)
        combos = [e for e in driver.log() if e["kind"] == "combo"]
        moves = [e for e in driver.log() if e["kind"] == "move"]
        self.assertEqual(len(combos), 1)
        self.assertEqual(len(moves), 1)

    def test_direct_run_batch_confirmation_preserves_all_items(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_batch([
            {"tool": "wait.pause", "arguments": {"ms": 1}},
            {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}},
            {"tool": "pointer.move", "arguments": {"x": 500, "y": 300}},
        ])
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        gate.resolve(rid, approve=True)
        combos = [e for e in driver.log() if e["kind"] == "combo"]
        self.assertEqual(len(combos), 1)
        moves = [e for e in driver.log() if e["kind"] == "move"]
        self.assertEqual(len(moves), 1)

    def test_batch_validation_error(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("batch.execute", {"items": []})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_arguments")


def emit_types(engine):
    return engine._emit.types()


class TestStoppedState(unittest.TestCase):
    def test_actions_blocked_while_stopped(self):
        engine, _, gate, _, _ = make_engine()
        gate.trigger_panic(source="hotkey")
        r = engine.run_tool("screen.capture", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "safety_stopped")
        gate.release_panic(source="protocol")
        r = engine.run_tool("screen.capture", {})
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
