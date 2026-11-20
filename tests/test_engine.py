"""Unit tests for the engine: execution, coordinate mapping, policy interaction,
confirmation flow, batch semantics and snapshot staleness."""

import os
import base64
import io

from PIL import Image
import subprocess
import sys
import textwrap
import threading
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
    def test_default_and_false_grid_preserve_legacy_capture(self):
        engine, driver, _, _, _ = make_engine()
        original_capture = driver.capture_with_dimensions
        calls = []

        # Legacy drivers do not accept a grid keyword or advertise grid support.
        def legacy_capture(bbox, canvas_width, canvas_height, format, quality, grayscale):
            calls.append((bbox, canvas_width, canvas_height, format, quality, grayscale))
            return original_capture(bbox, canvas_width, canvas_height, format, quality, grayscale)

        driver.supports_capture_grid = False
        driver.supports_capture_action_frame = False
        driver.capture_with_dimensions = legacy_capture
        args = {"region": {"x": 100, "y": 50, "width": 400, "height": 200},
                "scale": 0.5, "format": "jpeg", "quality": 73, "grayscale": True}
        omitted = engine.run_tool("screen.capture", args)
        explicit = engine.run_tool("screen.capture", {**args, "show_grid": False})
        self.assertTrue(omitted["ok"], omitted)
        self.assertTrue(explicit["ok"], explicit)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][3:], ("jpeg", 73, True))
        first, second = omitted["result"], explicit["result"]
        self.assertNotIn("grid", first)
        self.assertNotIn("grid", second)
        self.assertEqual(first["data_url"], second["data_url"])
        self.assertEqual(first["width"], second["width"])
        self.assertEqual(first["height"], second["height"])
        self.assertEqual(first["canvas"], second["canvas"])
        # The legacy driver's original unannotated bytes are passed through.
        payload = original_capture(*calls[0])
        self.assertEqual(base64.b64decode(first["data_url"].split(",", 1)[1]), payload.bytes)

    def test_grid_rejects_legacy_dimension_driver(self):
        engine, driver, _, _, _ = make_engine()
        driver.supports_capture_grid = False
        driver.capture_with_dimensions = lambda bbox, canvas_width, canvas_height, format, quality, grayscale: None
        result = engine._do_screen_capture({}, grid=True)
        self.assertFalse(result["ok"])
        self.assertIn("backend_unavailable", str(result))

    def test_grid_failure_preserves_latest_capture(self):
        engine, _, _, _, _ = make_engine()
        first = engine._do_screen_capture({})
        surface = engine._action_surface
        failed = engine._do_screen_capture({"scale": 0.001}, grid=True)
        self.assertFalse(failed["ok"])
        self.assertIn("small", str(failed).lower())
        self.assertIs(engine._action_surface, surface)
        self.assertEqual(engine._frame, first["result"]["frame"])

    def test_grid_output_matches_capture_frame(self):
        engine, driver, _, _, _ = make_engine()
        for fmt, grayscale in (("png", False), ("jpeg", True)):
            args = {"show_grid": True, "scale": 0.5, "format": fmt, "grayscale": grayscale,
                    "region": {"x": 100, "y": 50, "width": 400, "height": 200}}
            result = engine.run_tool("screen.capture", args)
            self.assertTrue(result["ok"], result)
            data = result["result"]
            image = Image.open(io.BytesIO(base64.b64decode(data["data_url"].split(",", 1)[1])))
            self.assertEqual(image.size, (data["width"], data["height"]))
            self.assertEqual(image.format, fmt.upper() if fmt == "png" else "JPEG")
            self.assertEqual(image.mode, "L" if grayscale else "RGB")
            interval = data["grid"]["interval"]
            self.assertNotEqual(image.getpixel((interval["x"], 53)), image.getpixel((interval["x"] + 3, 53)))
            self.assertEqual(data["grid"]["present"], True)
            self.assertEqual(interval, {"x": 100, "y": 50})
            self.assertEqual(data["grid"]["action_frame"], data["capture_geometry"]["action_frame"])
            engine.run_tool("pointer.move", {"x": 40, "y": 30})
            self.assertEqual([e for e in driver.log() if e["kind"] == "move"][-1]["position"], (200 + 40 * 800 / 960, 100 + 30 * 800 / 960))
        plain = engine.run_tool("screen.capture", {})["result"]
        self.assertNotIn("grid", plain)

    def test_grid_unknown_frame_preserves_active_capture(self):
        engine, driver, _, _, _ = make_engine()
        first = engine.run_tool("screen.capture", {})
        surface = engine._action_surface
        driver.supports_capture_action_frame = False
        result = engine.run_tool("screen.capture", {"show_grid": True})
        self.assertFalse(result["ok"])
        self.assertIs(engine._action_surface, surface)
        self.assertEqual(engine._frame, first["result"]["frame"])

    def test_grid_requires_driver_confirmation(self):
        engine, driver, _, _, _ = make_engine()
        first = engine.run_tool("screen.capture", {})
        surface = engine._action_surface
        original = driver.capture_with_dimensions
        def no_confirmation(*args, **kwargs):
            payload = original(*args, **kwargs)
            payload.grid_interval = None
            return payload
        driver.capture_with_dimensions = no_confirmation
        result = engine.run_tool("screen.capture", {"show_grid": True})
        self.assertFalse(result["ok"])
        self.assertIs(engine._action_surface, surface)
        self.assertEqual(engine._frame, first["result"]["frame"])

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

    def test_scaled_capture_canvas_coordinates_map_to_matching_physical_point(self):
        engine, driver, _, _, _ = make_engine()
        result = engine.run_tool("screen.capture", {"scale": 0.5})

        self.assertTrue(result["ok"], result)
        canvas = result["result"]["canvas"]
        self.assertEqual(canvas["display_width_px"], 960)
        self.assertEqual(canvas["display_height_px"], 540)
        self.assertEqual(canvas["scale"], 4.0)

        click = engine.run_tool("pointer.click", {"x": 480, "y": 270})
        self.assertTrue(click["ok"], click)
        moves = [event for event in driver.log() if event["kind"] == "move"]
        self.assertEqual(moves[-1]["position"], (1920.0, 1080.0))

    def test_capture_geometry_describes_actual_frame_and_pointer_mapping(self):
        engine, driver, _, _, _ = make_engine({"capture": {"max_area": 1024}})
        engine._surface = Surface.from_physical(100, -150, -50, 150, 90)
        for args, bounds in (
            ({"scale": 0.5}, {"x": -150, "y": -50, "width": 150, "height": 90}),
            ({"scale": 0.5, "region": {"x": 90.2, "y": 10.2, "width": 20, "height": 15}},
             {"x": -15, "y": -35, "width": 15, "height": 23}),
        ):
            with self.subTest(args=args):
                result = engine.run_tool("screen.capture", args)["result"]
                geometry = result["capture_geometry"]
                self.assertEqual(geometry["physical_bounds"], bounds)
                self.assertEqual(geometry["image"], {"width": result["width"], "height": result["height"], "units": "pixels"})
                action = geometry["action_frame"]
                self.assertEqual(action["frame"], result["frame"])
                self.assertEqual(action["type"], "capture_relative")
                self.assertEqual(action["target"], "virtual_desktop")
                self.assertEqual(action["image_to_action_scale"], {"x": 1, "y": 1})
                scale = bounds["width"] / result["width"]
                self.assertEqual(action["physical_pixels_per_unit"], scale)
                point = {"x": min(3, result["width"] - 1), "y": min(2, result["height"] - 1)}
                engine.run_tool("pointer.move", point)
                move = [event for event in driver.log() if event["kind"] == "move"][-1]
                self.assertEqual(move["position"], (bounds["x"] + point["x"] * scale,
                                                    bounds["y"] + point["y"] * scale))

    def test_capture_rejects_non_object_arguments_before_driver_call(self):
        for arguments in ([], None):
            with self.subTest(arguments=arguments):
                engine, driver, _, _, _ = make_engine()
                result = engine.run_tool("screen.capture", arguments)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertFalse(any(event["kind"] == "capture" for event in driver.log()))

    def test_capture_region_maps_through_surface(self):
        engine, driver, _, _, _ = make_engine()
        engine.run_tool("screen.capture", {"region": {"x": 100, "y": 50, "width": 10, "height": 20}})
        log = driver.log()
        capture = [e for e in log if e["kind"] == "capture"][0]
        # scale is 2.0 (3840/1920)
        self.assertEqual(capture["bbox"], (200, 100, 220, 140))

    def test_capture_rejects_nonnumeric_region_fields(self):
        invalid_regions = (
            {"x": "0", "y": 0, "width": 10, "height": 10},
            {"x": 0, "y": "0", "width": 10, "height": 10},
            {"x": 0, "y": 0, "width": "10", "height": 10},
            {"x": 0, "y": 0, "width": 10, "height": "10"},
        )
        for region in invalid_regions:
            with self.subTest(region=region):
                engine, driver, _, _, _ = make_engine()
                result = engine.run_tool("screen.capture", {"region": region})

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertFalse(any(event["kind"] == "capture" for event in driver.log()))

    def test_capture_rejects_fully_offscreen_region(self):
        engine, driver, _, _, _ = make_engine()
        result = engine.run_tool("screen.capture", {
            "region": {"x": 999999, "y": 0, "width": 10, "height": 10}
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("does not intersect", result["error"]["message"])
        self.assertFalse(any(event["kind"] == "capture" for event in driver.log()))

    def test_capture_bounds_full_desktop_with_negative_origin_and_area_limit(self):
        engine, driver, _, _, _ = make_engine({"capture": {"max_area": 1024}})
        engine._surface = Surface.from_physical(1920, -1920, -100, 3840, 1080)
        result = engine.run_tool("screen.capture", {"scale": 0.5})
        self.assertTrue(result["ok"], result)
        self.assertLessEqual(result["result"]["width"] * result["result"]["height"], 1024)
        self.assertEqual(engine._action_surface.physical_bbox, (-1920, -100, 1920, 980))
        self.assertEqual([e for e in driver.log() if e["kind"] == "capture"][-1]["bbox"],
                         engine._action_surface.physical_bbox)

    def test_capture_bounds_clipping_rounding_and_failed_capture(self):
        engine, driver, _, _, _ = make_engine()
        engine._surface = Surface.from_physical(100, -150, -50, 150, 90)
        result = engine.run_tool("screen.capture", {"scale": 0.5, "region": {
            "x": 99, "y": 1, "width": 4, "height": 3}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(engine._action_surface.physical_bbox, (-2, -49, 0, -44))
        self.assertEqual([e for e in driver.log() if e["kind"] == "capture"][-1]["bbox"],
                         engine._action_surface.physical_bbox)
        click = engine.run_tool("pointer.click", {"x": 0, "y": 0})
        self.assertTrue(click["ok"], click)
        self.assertEqual([e for e in driver.log() if e["kind"] == "move"][-1]["position"],
                         (-2.0, -49.0))
        rejected = engine.run_tool("screen.capture", {"region": {
            "x": 200, "y": 0, "width": 1, "height": 1}})
        self.assertFalse(rejected["ok"])
        self.assertEqual(engine._action_surface.physical_bbox, (-2, -49, 0, -44))

    def test_capture_format_jpeg(self):
        engine, _, _, _, _ = make_engine()
        r = engine.run_tool("screen.capture", {"format": "jpeg"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["result"]["data_url"].startswith("data:image/jpeg;base64,"))


class TestCallbackLockInversion(unittest.TestCase):
    def test_panic_callback_reconfigure_completes_with_admitted_action(self):
        script = textwrap.dedent("""
            import threading

            from computer_control.drivers.dummy import NullDriver
            from computer_control.session import Session

            callback_entered = threading.Event()
            callback_finished = threading.Event()
            dispatch_entered = threading.Event()
            action_finished = threading.Event()
            panic_finished = threading.Event()
            errors = []
            action_results = []
            session = None

            def emit(event_type, payload=None):
                if event_type == "safety.panic_triggered":
                    callback_entered.set()
                    session.configure({"safety": {"confirm_threshold": "high"}})
                    callback_finished.set()

            session = Session(emit, driver_factory=lambda cfg: NullDriver(enable_a11y=True))
            session.start({})
            engine = session._engine

            def dispatch(tool, arguments):
                dispatch_entered.set()
                if not callback_entered.wait(2.0):
                    raise AssertionError("panic callback did not start during dispatch")
                return {"ok": True, "result": {}, "error": None, "meta": {}}

            engine._dispatch = dispatch

            def run_action():
                try:
                    action_results.append(engine._execute("screen.capture", {}))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    action_finished.set()

            def trigger_panic():
                try:
                    session._gate.trigger_panic("test")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    panic_finished.set()

            action_thread = threading.Thread(target=run_action, daemon=True)
            action_thread.start()
            assert dispatch_entered.wait(2.0), "action did not enter dispatch"
            panic_thread = threading.Thread(target=trigger_panic, daemon=True)
            panic_thread.start()

            action_thread.join(2.0)
            panic_thread.join(2.0)
            assert action_finished.is_set(), "admitted action deadlocked at note_activity"
            assert panic_finished.is_set(), "panic callback could not finish session.configure"
            assert callback_finished.is_set(), "session.configure did not return"
            assert errors == [], errors
            assert action_results and action_results[0]["ok"], action_results
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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

    def test_click_rejects_partial_coordinates_without_clicking(self):
        for args in ({"x": 100}, {"y": 100}):
            with self.subTest(args=args):
                engine, driver, _, _, _ = make_engine()
                result = engine.run_tool("pointer.click", args)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertFalse(any(event["kind"] in ("move", "click")
                                     for event in driver.log()))

    def test_scroll_rejects_partial_coordinates_without_scrolling(self):
        for args in ({"amount": 1, "x": 100}, {"amount": 1, "y": 100}):
            with self.subTest(args=args):
                engine, driver, _, _, _ = make_engine()
                result = engine.run_tool("pointer.scroll", args)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertFalse(any(event["kind"] in ("move", "scroll")
                                     for event in driver.log()))

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

    def test_not_authorized_includes_auth_profile(self):
        # the plain tools.call path reports the active profile in error.data,
        # matching the batch path and the protocol docs
        engine, _, _, _, _ = make_engine({"safety": {"auth_profile": "read_only"}})
        r = engine.run_tool("pointer.click", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "not_authorized")
        self.assertEqual(r["error"]["data"]["auth_profile"], "read_only")
        engine, _, _, _, _ = make_engine({"safety": {"auth_profile": "revoked"}})
        r = engine.run_tool("keyboard.type", {"text": "hi"})
        self.assertEqual(r["error"]["code"], "not_authorized")
        self.assertEqual(r["error"]["data"]["auth_profile"], "revoked")

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

    def test_dummy_a11y_snapshot_rects_use_right_and_bottom_edges(self):
        engine, _, _, _, _ = make_engine()
        snap = engine.run_tool("a11y.snapshot", {"level": "full"})
        self.assertTrue(snap["ok"], snap)

        children = snap["result"]["tree"]["children"]
        self.assertEqual(children[1]["rect"], [220, 10, 280, 38])
        self.assertEqual(children[2]["children"][0]["rect"], [10, 50, 310, 70])
        self.assertEqual(children[2]["children"][1]["rect"], [10, 72, 310, 92])

    def test_dummy_a11y_pointer_activation_uses_button_center(self):
        engine, _, _, _, _ = make_engine()
        snap = engine.run_tool("a11y.snapshot", {})
        result = engine.run_tool("a11y.activate", {
            "snapshot_id": snap["result"]["snapshot_id"],
            "node_id": 3,
            "method": "pointer",
        })

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["position"], {"x": 250.0, "y": 24.0})


class TestConfirmation(unittest.TestCase):
    def test_padded_win_alias_requires_confirmation_at_default_threshold(self):
        engine, driver, _, emit, _ = make_engine()

        result = engine.run_tool("keyboard.combo", {"keys": [" WIN ", "r"]})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        self.assertEqual(result["result"]["risk"], "high")
        self.assertIn("safety.confirmation_requested", emit.types())
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])

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

    def test_mutating_caller_arguments_does_not_change_approved_action(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        keys = ["win", "r"]
        arguments = {"keys": keys}

        result = engine.run_tool("keyboard.combo", arguments)
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        keys[:] = ["ctrl", "c"]
        arguments["keys"] = ["alt", "f4"]

        gate.resolve(result["result"]["request_id"], approve=True)

        combos = [e for e in driver.log() if e["kind"] == "combo"]
        self.assertEqual(combos, [{"kind": "combo", "keys": ["win", "r"]}])

    def test_deny_does_not_execute(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        gate.resolve(r["result"]["request_id"], approve=False)
        # denial is synchronous: nothing runs and no wait is needed
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])

    def test_expired_confirmation_approval_errors(self):
        engine, driver, gate, _, _ = make_engine(
            {"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        # Expire deterministically instead of sleeping past the timeout:
        # resolve() purges on entry and raises confirmation_expired.
        with gate._lock:
            gate._pending.expires_at = time.monotonic() - 1.0
        with self.assertRaises(Exception) as ctx:
            gate.resolve(r["result"]["request_id"], approve=True)
        self.assertEqual(getattr(ctx.exception, "code", None), "confirmation_expired")
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])


class TestApprovalRace(unittest.TestCase):
    """An approval collected while an action was still legal must not execute
    after the authorization profile was narrowed or revoked afterwards."""

    def _captured(self, overrides):
        engine, driver, gate, emit, _ = make_engine(overrides)
        approved = []
        gate.on_approved = approved.append  # capture instead of executing
        return engine, driver, gate, emit, approved

    def test_approved_action_refused_after_downgrade(self):
        engine, driver, gate, emit, approved = self._captured(
            {"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        gate.resolve(r["result"]["request_id"], approve=True)
        gate.set_grant("read_only")  # downgrade lands after the approval
        engine._on_confirmation_approved(approved[0])
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])
        # the refusal closes the approval out on the event stream with a full
        # started/finished pair - no orphan finished without its started
        started = [p for t, p in emit.dump() if t == "action.started"]
        self.assertEqual([p["tool"] for p in started], ["keyboard.combo"])
        finished = [p for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual([p.get("error_code") for p in finished], ["not_authorized"])

    def test_approved_action_refused_after_revoke(self):
        engine, driver, gate, emit, approved = self._captured(
            {"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("keyboard.combo", {"keys": ["win", "r"]})
        gate.resolve(r["result"]["request_id"], approve=True)
        gate.set_grant("revoked")  # revoke lands after the approval
        engine._on_confirmation_approved(approved[0])
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])
        started = [p for t, p in emit.dump() if t == "action.started"]
        self.assertEqual([p["tool"] for p in started], ["keyboard.combo"])
        finished = [p for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual([p.get("error_code") for p in finished], ["not_authorized"])

    def test_approved_batch_emits_outer_action_envelope(self):
        # An approved batch must emit the same envelope a direct batch does:
        # started(batch.execute) -> per-item pairs -> batch.finished ->
        # finished(batch.execute). It used to skip the outer pair entirely.
        engine, driver, gate, emit, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {"items": [
            {"tool": "pointer.click", "arguments": {"button": "left"}},
            {"tool": "keyboard.combo", "arguments": {"keys": ["ctrl", "c"]}},
        ]})
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        pre = len(emit.types())
        self.assertNotIn("action.started", emit.types())
        gate.resolve(r["result"]["request_id"], approve=True)

        post = emit.types()[pre:]
        # resolve() announces its outcome first; the execution envelope follows
        self.assertEqual(post[0], "safety.confirmation_resolved")
        post = post[1:]
        self.assertEqual(post[0], "action.started")
        self.assertEqual(post[-1], "action.finished")
        self.assertEqual(post.index("batch.finished"), len(post) - 2)
        started_tools = [p["tool"] for t, p in emit.dump() if t == "action.started"]
        self.assertEqual(started_tools, ["batch.execute", "pointer.click", "keyboard.combo"])
        finished_tools = [p["tool"] for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual(finished_tools, ["pointer.click", "keyboard.combo", "batch.execute"])

    def test_approved_batch_refused_after_downgrade_emits_envelope(self):
        # The approval was collected while the batch was legal; a downgrade to
        # read_only lands first. The batch must be refused as a whole - with a
        # full started/finished pair and no per-item events or batch.finished.
        engine, driver, gate, emit, approved = self._captured(
            {"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {"items": [
            {"tool": "pointer.click", "arguments": {"button": "left"}},
            {"tool": "keyboard.combo", "arguments": {"keys": ["ctrl", "c"]}},
        ]})
        gate.resolve(r["result"]["request_id"], approve=True)
        gate.set_grant("read_only")  # the whole batch is no longer authorized
        engine._on_confirmation_approved(approved[0])
        self.assertEqual(driver.log(), [])
        self.assertNotIn("batch.finished", emit.types())
        started_tools = [p["tool"] for t, p in emit.dump() if t == "action.started"]
        self.assertEqual(started_tools, ["batch.execute"])
        finished = [p for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual([p.get("error_code") for p in finished], ["not_authorized"])

    def test_approved_batch_rechecks_authorization_after_started_callback(self):
        engine, driver, gate, emit, approved = self._captured(
            {"safety": {"confirm_threshold": "moderate"}})

        def downgrade_on_batch_start(event, payload):
            emit(event, payload)
            if event == "action.started" and payload.get("tool") == "batch.execute":
                gate.set_grant("read_only")

        engine._emit = downgrade_on_batch_start
        result = engine.run_tool("batch.execute", {"items": [
            {"tool": "pointer.click", "arguments": {"button": "left"}},
            {"tool": "keyboard.combo", "arguments": {"keys": ["ctrl", "c"]}},
        ]})
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        gate.resolve(result["result"]["request_id"], approve=True)
        engine._on_confirmation_approved(approved[0])

        self.assertEqual(driver.log(), [])
        self.assertNotIn("batch.finished", emit.types())
        started = [payload["tool"] for event, payload in emit.dump()
                   if event == "action.started"]
        self.assertEqual(started, ["batch.execute"])
        finished = [(payload["tool"], payload.get("error_code"))
                    for event, payload in emit.dump() if event == "action.finished"]
        self.assertEqual(finished, [("batch.execute", "not_authorized")])

    def test_approved_batch_item_refused_after_downgrade(self):
        engine, driver, gate, _, approved = self._captured(
            {"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "keyboard.combo", "arguments": {"keys": ["win", "r"]}},
                {"tool": "wait.pause", "arguments": {"ms": 1}},
            ],
        })
        gate.resolve(r["result"]["request_id"], approve=True)
        gate.set_grant("read_only")
        result = engine._run_batch_payload(approved[0].payload)
        items = result["result"]["items"]
        self.assertEqual(items[0]["error"]["code"], "not_authorized")
        self.assertEqual(items[0]["error"]["data"]["auth_profile"], "read_only")
        self.assertEqual([e for e in driver.log() if e["kind"] == "combo"], [])

    def test_downgrade_cancels_pending_batch_confirmation_deep(self):
        # A mixed batch pending at full must not survive a downgrade to
        # limited just because batch.execute itself is not a keyboard tool.
        engine, driver, gate, emit, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        r = engine.run_tool("batch.execute", {
            "items": [
                {"tool": "pointer.click", "arguments": {"button": "left"}},
                {"tool": "keyboard.combo", "arguments": {"keys": ["ctrl", "c"]}},
            ],
        })
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        rid = r["result"]["request_id"]
        gate.set_grant("limited")
        self.assertIsNone(gate.pending_confirmation())
        self.assertIn("safety.confirmation_resolved", emit.types())
        with self.assertRaises(Exception) as ctx:
            gate.resolve(rid, approve=True)
        self.assertEqual(getattr(ctx.exception, "code", None), "confirmation_not_found")
        self.assertEqual(driver.log(), [])


class TestReentrantAdmissionCallback(unittest.TestCase):
    def test_started_callback_downgrade_refuses_standalone_action(self):
        engine, driver, gate, emit, _ = make_engine()
        callback_lock_states = []

        def downgrade_on_start(event, payload):
            emit(event, payload)
            if event == "action.started" and payload.get("tool") == "pointer.click":
                callback_lock_states.append(gate._admission_lock._is_owned())
                gate.set_grant("read_only")

        engine._emit = downgrade_on_start
        result = engine.run_tool("pointer.click", {"button": "left"})

        self.assertEqual(callback_lock_states, [False])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "not_authorized")
        self.assertEqual(result["error"]["data"]["auth_profile"], "read_only")
        self.assertFalse(any(entry["kind"] == "click" for entry in driver.log()))
        events = emit.dump()
        started = [payload["tool"] for event, payload in events
                   if event == "action.started"]
        finished = [(payload["tool"], payload.get("error_code"))
                    for event, payload in events if event == "action.finished"]
        self.assertEqual(started, ["pointer.click"])
        self.assertEqual(finished, [("pointer.click", "not_authorized")])

    def test_started_callback_downgrade_refuses_keyboard_batch_child(self):
        engine, driver, gate, emit, _ = make_engine()
        callback_lock_states = []

        def downgrade_on_child_start(event, payload):
            emit(event, payload)
            if event == "action.started" and payload.get("tool") == "keyboard.type":
                callback_lock_states.append(gate._admission_lock._is_owned())
                gate.set_grant("limited")

        engine._emit = downgrade_on_child_start
        result = engine.run_tool("batch.execute", {"items": [{
            "tool": "keyboard.type", "arguments": {"text": "x"},
        }]})

        self.assertEqual(callback_lock_states, [False])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["status"], "aborted")
        item = result["result"]["items"][0]
        self.assertFalse(item["ok"], item)
        self.assertEqual(item["error"]["code"], "not_authorized")
        self.assertEqual(item["error"]["data"]["auth_profile"], "limited")
        self.assertFalse(any(entry["kind"] == "type" for entry in driver.log()))
        events = emit.dump()
        started = [payload["tool"] for event, payload in events
                   if event == "action.started"]
        finished = [(payload["tool"], payload.get("error_code"))
                    for event, payload in events if event == "action.finished"]
        self.assertEqual(started, ["batch.execute", "keyboard.type"])
        self.assertEqual(finished, [("keyboard.type", "not_authorized"),
                                    ("batch.execute", None)])
        self.assertEqual(emit.types().count("batch.finished"), 1)


class TestAdmissionAtomicity(unittest.TestCase):
    """The final authorization check and the dispatch are atomic with respect
    to grant changes: a downgrade can never land between them."""

    def test_downgrade_cannot_land_between_check_and_dispatch(self):
        engine, driver, gate, _, _ = make_engine()
        entered = threading.Event()
        release = threading.Event()
        real_dispatch = engine._dispatch

        def blocked_dispatch(tool, args):
            entered.set()
            release.wait(timeout=5.0)
            return real_dispatch(tool, args)

        engine._dispatch = blocked_dispatch
        results = {}
        action_thread = threading.Thread(
            target=lambda: results.update(r=engine.run_tool("pointer.click", {"button": "left"})))
        action_thread.start()
        self.assertTrue(entered.wait(2.0), "action never reached dispatch")

        # Deterministic lock-state oracle, no timing window: dispatch being in
        # flight means the action thread holds the admission lock (an RLock
        # counts per thread; this thread is not the owner, so a non-blocking
        # acquire fails exactly while the action sits between its final check
        # and its dispatch completion). Whatever is queued behind that lock -
        # a set_grant downgrade, a session.configure - cannot slip in.
        self.assertFalse(gate._admission_lock.acquire(blocking=False),
                         "admission lock must be held while dispatch is in flight")
        grant_done = threading.Event()

        def downgrade():
            gate.set_grant("read_only")  # must queue behind the admission lock
            grant_done.set()

        grant_thread = threading.Thread(target=downgrade)
        grant_thread.start()
        release.set()
        action_thread.join(5.0)
        grant_thread.join(5.0)
        self.assertFalse(action_thread.is_alive(), "action thread still running")
        self.assertFalse(grant_thread.is_alive(), "downgrade thread still blocked")
        self.assertTrue(results["r"]["ok"], results["r"])
        self.assertEqual(len([e for e in driver.log() if e["kind"] == "click"]), 1)
        self.assertTrue(grant_done.is_set(), "downgrade never completed")
        # once the downgrade has landed, the same action is refused outright
        r2 = engine.run_tool("pointer.click", {"button": "left"})
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error"]["code"], "not_authorized")
        self.assertEqual(r2["error"]["data"]["auth_profile"], "read_only")

    def test_downgrade_lands_between_batch_items(self):
        # The batch's outer admission must not hold the admission lock across
        # the item loop (same-thread re-entry would hide it): a grant change
        # from another thread must land between items, and the next item's
        # final check must refuse what the new profile blocks.
        engine, driver, gate, emit, _ = make_engine()
        between_items = threading.Event()
        downgrade_done = threading.Event()
        real_execute = engine._execute
        calls = {"n": 0}

        def gated_execute(tool, args, *extra, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:  # 1: batch.execute, 2: item 1, 3: item 2
                # parked between items: the admission lock must be free here,
                # so the main thread's set_grant below can land deterministically
                between_items.set()
                self.assertTrue(downgrade_done.wait(2.0),
                                "downgrade could not land between batch items")
            return real_execute(tool, args, *extra, **kwargs)

        engine._execute = gated_execute
        results = {}
        batch_thread = threading.Thread(target=lambda: results.update(
            r=engine.run_tool("batch.execute", {"items": [
                {"tool": "keyboard.type", "arguments": {"text": "a"}},
                {"tool": "keyboard.type", "arguments": {"text": "b"}},
            ]})))
        batch_thread.start()
        self.assertTrue(between_items.wait(2.0), "batch never reached item 2")

        gate.set_grant("limited")  # free between items: lands immediately
        downgrade_done.set()
        batch_thread.join(5.0)
        self.assertFalse(batch_thread.is_alive(), "batch thread still running")

        r = results["r"]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["status"], "aborted")
        items = r["result"]["items"]
        self.assertTrue(items[0]["ok"], items[0])  # ran before the downgrade
        self.assertFalse(items[1]["ok"], items[1])
        self.assertEqual(items[1]["error"]["code"], "not_authorized")
        self.assertEqual(items[1]["error"]["data"]["auth_profile"], "limited")
        # only item 1 reached the driver
        self.assertEqual(len([e for e in driver.log() if e["kind"] == "type"]), 1)
        # The outer batch and each item get exactly one event pair. The
        # downgraded item is reported as a refusal without dispatching.
        started_tools = [p["tool"] for t, p in emit.dump() if t == "action.started"]
        self.assertEqual(started_tools,
                         ["batch.execute", "keyboard.type", "keyboard.type"])
        finished = [p for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual([p["tool"] for p in finished],
                         ["keyboard.type", "keyboard.type", "batch.execute"])
        self.assertEqual([p.get("error_code") for p in finished],
                         [None, "not_authorized", None])
        self.assertEqual(started_tools.count("batch.execute"), 1)
        self.assertIn("batch.finished", emit.types())
        self.assertEqual(emit.types().count("batch.finished"), 1)

    def test_concurrent_batch_and_single_action_do_not_deadlock(self):
        engine, _, _, _, _ = make_engine()
        batch_at_second_item = threading.Event()
        continue_batch = threading.Event()
        single_waiting_for_execution = threading.Event()
        real_execute = engine._execute

        def pause_before_second_item(tool, args, *extra, **kwargs):
            if tool == "keyboard.type" and args.get("text") == "second":
                batch_at_second_item.set()
                continue_batch.wait(timeout=5.0)
            return real_execute(tool, args, *extra, **kwargs)

        engine._execute = pause_before_second_item

        class ObservedExecutionLock:
            def __init__(self):
                self._lock = threading.RLock()

            def __enter__(self):
                if threading.current_thread().name == "single-action":
                    single_waiting_for_execution.set()
                    if not self._lock.acquire(timeout=1.5):
                        raise TimeoutError("single action blocked on execution lock")
                else:
                    self._lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self._lock.release()

        engine._execution_lock = ObservedExecutionLock()
        results = {}
        batch_thread = threading.Thread(
            name="batch-action",
            target=lambda: results.update(r=engine.run_tool("batch.execute", {"items": [
                {"tool": "keyboard.type", "arguments": {"text": "first"}},
                {"tool": "keyboard.type", "arguments": {"text": "second"}},
            ]})),
        )
        single_thread = threading.Thread(
            name="single-action",
            target=lambda: results.update(r_single=engine.run_tool(
                "pointer.click", {"button": "left"})),
        )

        batch_thread.start()
        try:
            self.assertTrue(batch_at_second_item.wait(timeout=2.0),
                            "batch never reached its second item")
            single_thread.start()
            self.assertTrue(single_waiting_for_execution.wait(timeout=2.0),
                            "single action never attempted execution")
        finally:
            continue_batch.set()

        batch_thread.join(timeout=5.0)
        if single_thread.ident is not None:
            single_thread.join(timeout=5.0)
        self.assertFalse(batch_thread.is_alive(), "batch thread still running")
        self.assertFalse(single_thread.is_alive(), "single action thread still running")
        self.assertTrue(results["r"]["ok"], results["r"])
        self.assertTrue(results["r_single"]["ok"], results["r_single"])

    def test_downgrade_before_batch_prescan_emits_item_refusal_pair(self):
        engine, _, gate, emit, _ = make_engine()
        pre_scan_entered = threading.Event()
        continue_pre_scan = threading.Event()
        real_build_plan = engine._build_batch_plan

        def blocked_build_plan(items):
            pre_scan_entered.set()
            self.assertTrue(continue_pre_scan.wait(timeout=2.0),
                            "batch pre-scan was not released")
            return real_build_plan(items)

        engine._build_batch_plan = blocked_build_plan
        results = {}
        batch_thread = threading.Thread(target=lambda: results.update(r=engine.run_tool(
            "batch.execute", {"items": [
                {"tool": "keyboard.type", "arguments": {"text": "a"}},
            ]})))
        batch_thread.start()
        try:
            self.assertTrue(pre_scan_entered.wait(timeout=2.0),
                            "batch never reached its pre-scan")
            self.assertTrue(gate._admission_lock.acquire(blocking=False),
                            "outer batch admission must release the lock before pre-scan")
            gate._admission_lock.release()
            gate.set_grant("read_only")
        finally:
            continue_pre_scan.set()
        batch_thread.join(timeout=5.0)
        self.assertFalse(batch_thread.is_alive(), "batch thread still running")

        result = results["r"]
        self.assertEqual(result["result"]["items"][0]["error"]["code"], "not_authorized")
        started_tools = [p["tool"] for t, p in emit.dump() if t == "action.started"]
        self.assertEqual(started_tools, ["batch.execute", "keyboard.type"])
        finished = [p for t, p in emit.dump() if t == "action.finished"]
        self.assertEqual([p["tool"] for p in finished], ["keyboard.type", "batch.execute"])
        self.assertEqual([p.get("error_code") for p in finished], ["not_authorized", None])

    def test_downgrade_that_wins_the_lock_intercepts_the_action(self):
        # The other arm of the race: the downgrade lands first (here it wins
        # the admission lock while the action is parked before its final
        # check), and the pending execution is refused instead of running.
        engine, driver, gate, _, _ = make_engine()
        gate._admission_lock.acquire()
        try:
            results = {}
            action_thread = threading.Thread(
                target=lambda: results.update(r=engine.run_tool("pointer.click", {"button": "left"})))
            action_thread.start()
            gate.set_grant("read_only")  # same thread: reentrant, lands while parked
        finally:
            gate._admission_lock.release()
        action_thread.join(5.0)
        self.assertFalse(action_thread.is_alive())
        r = results["r"]
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["error"]["code"], "not_authorized")
        self.assertEqual(r["error"]["data"]["auth_profile"], "read_only")
        self.assertEqual(driver.log(), [])


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
    def test_batch_parent_authorization_is_rechecked_after_downgrade(self):
        engine, driver, gate, emit, _ = make_engine({
            "safety": {"auth_profile": "full"},
        })
        started_captures = 0

        def downgrade_on_second_capture_start(event, payload):
            nonlocal started_captures
            emit(event, payload)
            if event == "action.started" and payload.get("tool") == "screen.capture":
                started_captures += 1
                if started_captures == 2:
                    # Event subscribers may narrow the grant before the
                    # child's final authorization check and dispatch.
                    gate.set_grant("read_only")

        engine._emit = downgrade_on_second_capture_start
        result = engine.run_tool("batch.execute", {"items": [
            {"tool": "screen.capture", "arguments": {}},
            {"tool": "screen.capture", "arguments": {}},
        ]})

        self.assertEqual(sum(entry["kind"] == "capture" for entry in driver.log()), 1)
        items = result["result"]["items"]
        self.assertTrue(items[0]["ok"], items[0])
        self.assertFalse(items[1]["ok"], items[1])
        self.assertEqual(items[1]["error"]["code"], "not_authorized")
        self.assertEqual(items[1]["error"]["data"]["auth_profile"], "read_only")
        started = [payload["tool"] for event, payload in emit.dump()
                   if event == "action.started"]
        self.assertEqual(started, ["batch.execute", "screen.capture", "screen.capture"])
        finished = [(payload["tool"], payload.get("error_code"))
                    for event, payload in emit.dump() if event == "action.finished"]
        self.assertEqual(finished, [
            ("screen.capture", None),
            ("screen.capture", "not_authorized"),
            ("batch.execute", None),
        ])

    def test_nested_batch_refused_if_started_callback_downgrades_parent(self):
        engine, driver, gate, emit, _ = make_engine({
            "safety": {"auth_profile": "full"},
        })
        started_batches = 0

        def downgrade_on_nested_batch_start(event, payload):
            nonlocal started_batches
            emit(event, payload)
            if event == "action.started" and payload.get("tool") == "batch.execute":
                started_batches += 1
                if started_batches == 2:
                    gate.set_grant("read_only")

        engine._emit = downgrade_on_nested_batch_start
        result = engine.run_tool("batch.execute", {"items": [{
            "tool": "batch.execute",
            "arguments": {"items": [{"tool": "screen.capture", "arguments": {}}]},
        }]})

        nested = result["result"]["items"][0]
        self.assertFalse(nested["ok"], nested)
        self.assertEqual(nested["error"]["code"], "not_authorized")
        self.assertEqual(nested["error"]["data"]["auth_profile"], "read_only")
        self.assertFalse(any(entry["kind"] == "capture" for entry in driver.log()))
        started = [payload["tool"] for event, payload in emit.dump()
                   if event == "action.started"]
        self.assertEqual(started, ["batch.execute", "batch.execute"])
        finished = [(payload["tool"], payload.get("error_code"))
                    for event, payload in emit.dump() if event == "action.finished"]
        self.assertEqual(finished, [
            ("batch.execute", "not_authorized"),
            ("batch.execute", None),
        ])
        self.assertEqual(emit.types().count("batch.finished"), 1)

    def test_nested_unknown_child_does_not_cancel_confirmation_on_downgrade(self):
        engine, _, gate, _, _ = make_engine({
            "safety": {"auth_profile": "full", "confirm_threshold": "benign"},
        })
        result = engine.run_tool("batch.execute", {"items": [{
            "tool": "batch.execute",
            "arguments": {"items": [
                {"tool": "keyboard.unknown", "arguments": {}},
                {"tool": "pointer.click", "arguments": {}},
            ]},
        }]})
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        request_id = result["result"]["request_id"]
        pending = gate.pending_confirmation()

        gate.set_grant("limited")

        self.assertIs(gate.pending_confirmation(), pending)
        self.assertEqual(gate.resolve(request_id, approve=False).status, "denied")

    def test_nested_rule_matches_normalized_arguments_before_downgrade(self):
        engine, _, gate, _, _ = make_engine({
            "safety": {
                "auth_profile": "full",
                "confirm_threshold": "benign",
                "rules": [{
                    "match": {
                        "tool": "keyboard.combo",
                        "argument": {
                            "name": "keys",
                            "matcher": "equals",
                            "value": ["ctrl", "c"],
                        },
                    },
                    "effect": "deny",
                }],
            },
        })
        result = engine.run_tool("batch.execute", {"items": [{
            "tool": "batch.execute",
            "arguments": {"items": [
                {"tool": "keyboard.combo", "arguments": {"keys": "ctrl+c"}},
                {"tool": "pointer.click", "arguments": {}},
            ]},
        }]})
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        request_id = result["result"]["request_id"]
        pending = gate.pending_confirmation()

        gate.set_grant("limited")

        self.assertIs(gate.pending_confirmation(), pending)
        self.assertEqual(gate.resolve(request_id, approve=False).status, "denied")

    def test_deeply_nested_batch_returns_validation_error_before_execution(self):
        engine, driver, _, _, _ = make_engine()
        nested = {"tool": "wait.pause", "arguments": {"ms": 1}}
        for _ in range(1200):
            nested = {"tool": "batch.execute", "arguments": {"items": [nested]}}

        result = engine.run_tool("batch.execute", {"items": [nested]})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertTrue(result["error"]["data"]["issues"])
        self.assertEqual(driver.log(), [])

    def test_batch_confirmation_uses_snapshot_of_nested_caller_arguments(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        keys = ["win", "r"]
        items = [{"tool": "keyboard.combo", "arguments": {"keys": keys}}]

        result = engine.run_tool("batch.execute", {"items": items})
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        keys[:] = ["ctrl", "c"]

        gate.resolve(result["result"]["request_id"], approve=True)

        combos = [event for event in driver.log() if event["kind"] == "combo"]
        self.assertEqual(combos, [{"kind": "combo", "keys": ["win", "r"]}])

    def test_direct_batch_confirmation_uses_snapshot_of_nested_arguments(self):
        engine, driver, gate, _, _ = make_engine({"safety": {"confirm_threshold": "moderate"}})
        keys = ["win", "r"]
        items = [{"tool": "keyboard.combo", "arguments": {"keys": keys}}]

        result = engine.run_batch(items)
        self.assertEqual(result["result"]["status"], "awaiting_confirmation")
        keys[:] = ["ctrl", "c"]

        gate.resolve(result["result"]["request_id"], approve=True)

        combos = [event for event in driver.log() if event["kind"] == "combo"]
        self.assertEqual(combos, [{"kind": "combo", "keys": ["win", "r"]}])

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


class TestEngineBoundaryArguments(unittest.TestCase):
    def test_pointer_drag_sanitizes_steps_and_hold(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("pointer.drag", {
            "from": {"x": 10, "y": 10},
            "to": {"x": 100, "y": 100},
            "steps": 1,
            "hold_ms": 0,
        })
        self.assertTrue(r["ok"], r)
        drags = [e for e in driver.log() if e["kind"] == "drag"]
        self.assertEqual(len(drags), 1)
        self.assertEqual(drags[0]["steps"], 1)

    def test_pointer_click_sanitizes_times_and_hold(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("pointer.click", {
            "x": 50, "y": 50, "button": "left", "times": 1, "hold_ms": 0,
        })
        self.assertTrue(r["ok"], r)
        clicks = [e for e in driver.log() if e["kind"] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["times"], 1)

    def test_keyboard_type_sanitizes_interval(self):
        engine, driver, _, _, _ = make_engine()
        r = engine.run_tool("keyboard.type", {"text": "hello", "interval_ms": 0})
        self.assertTrue(r["ok"], r)
        types = [e for e in driver.log() if e["kind"] == "type"]
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0]["interval_ms"], 0)


if __name__ == "__main__":
    unittest.main()
