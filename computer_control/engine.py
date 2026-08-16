"""The engine: executes validated actions against a driver, on top of the
surface geometry and behind the safety gate.

The engine is the only component that talks to drivers, so every action -
single, batch, or confirmation-approved - follows one code path and emits
the same event stream.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Callable, List, Optional

from computer_control import actions as act
from computer_control.config import Config
from computer_control.drivers.base import BaseDriver, DriverError
from computer_control.geometry import Surface
from computer_control.policy import PendingConfirmation, SafetyGate

ResultDict = dict


class Engine:
    def __init__(self, driver: BaseDriver, surface: Surface, gate: SafetyGate,
                 emit: Callable[[str, Optional[dict]], None],
                 config_provider: Callable[[], Config]):
        """``config_provider`` returns the *current* configuration on every
        call, so runtime reconfiguration reaches the engine immediately."""
        self._driver = driver
        self._surface = surface
        self._gate = gate
        self._emit = emit
        self._get_cfg = config_provider
        self._frame = 0
        self._snapshot_counter = 0
        self._current_snapshot_id = None
        self._execution_lock = threading.RLock()
        # NOTE: the approval callback is wired by the session (through the
        # worker queue) so approved actions run serialized with everything
        # else. Engine tests may wire it directly.

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._driver.close()

    # ------------------------------------------------------------ single tool

    def run_tool(self, tool: str, arguments: dict) -> ResultDict:
        spec = act.get_spec(tool)
        if spec is None:
            return self._fail("unknown_tool", "no such tool: %r" % tool, tool=tool)
        arguments = self._apply_config_defaults(tool, arguments)
        try:
            args = act.clean_arguments(tool, arguments)
        except act.ValidationError as exc:
            return self._fail("invalid_arguments", "argument validation failed",
                              tool=tool, data={"issues": exc.issues})

        verdict = self._gate.evaluate(tool, args)
        if verdict.decision == "deny":
            return self._fail("policy_denied", verdict.reason, tool=tool,
                              data={"rule": verdict.data.get("rule")})
        if verdict.decision == "stopped":
            return self._fail("safety_stopped", verdict.reason, tool=tool)
        if verdict.decision == "standby":
            return self._fail("safety_standby", verdict.reason, tool=tool)
        if verdict.decision == "busy":
            return self._fail("busy", verdict.reason, tool=tool)
        if verdict.decision == "confirm":
            if tool == "batch.execute":
                self._stash_batch_payload(args)
            return self._ok({
                "status": "awaiting_confirmation",
                "request_id": verdict.data["request_id"],
                "risk": verdict.risk,
                "reason": verdict.data.get("reason", ""),
            }, tool=tool)

        return self._execute(tool, args)

    def _stash_batch_payload(self, args: dict) -> None:
        """Remember the validated batch items on the pending confirmation so
        the approved run can execute them without re-gating for confirmation."""
        pending = self._gate.pending_confirmation()
        if pending is None:
            return
        plan = [("run", item["tool"], item["arguments"]) for item in (args.get("items") or [])]
        pending.payload = {
            "items": plan,
            "continue_on_error": bool(args.get("continue_on_error")),
            "gap_ms": args.get("gap_ms"),
        }

    def _apply_config_defaults(self, tool: str, arguments: dict) -> dict:
        """Fill capture parameters from configuration when the caller did not
        provide them, so the configured defaults actually take effect."""
        if tool != "screen.capture":
            return arguments
        args = dict(arguments or {})
        cfg = self._get_cfg()
        args.setdefault("format", cfg.capture.default_format)
        args.setdefault("quality", cfg.capture.default_quality)
        args.setdefault("grayscale", cfg.capture.grayscale)
        return args

    def _on_confirmation_approved(self, pending: PendingConfirmation) -> None:
        """Called by the gate when a pending confirmation is approved."""
        if pending.tool == "batch.execute":
            payload = pending.payload
            if payload:
                self._run_batch_payload(payload)
        else:
            self._execute(pending.tool, pending.arguments)

    def _execute(self, tool: str, args: dict) -> ResultDict:
        started = time.monotonic()
        self._emit("action.started", {"tool": tool, "arguments": args})
        try:
            with self._execution_lock:
                result = self._dispatch(tool, args)
            self._gate.note_activity()
        except DriverError as exc:
            result = self._fail(exc.code, exc.message, tool=tool, data=exc.data)
        except Exception as exc:  # defensive: drivers must not take the server down
            result = self._fail("driver_failed", "unexpected driver error: %s" % exc, tool=tool)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit("action.finished", {
            "tool": tool,
            "ok": result.get("ok", False),
            "duration_ms": duration_ms,
            "error_code": (result.get("error") or {}).get("code"),
        })
        self._attach_meta(result, tool, duration_ms)
        return result

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, tool: str, args: dict) -> ResultDict:
        handler = getattr(self, "_do_" + tool.replace(".", "_"), None)
        if handler is None:
            return self._fail("driver_failed", "no handler for %r" % tool, tool=tool)
        return handler(args)

    # screen.capture --------------------------------------------------------

    def _do_screen_capture(self, args: dict) -> ResultDict:
        if not self._driver.capabilities.get("capture"):
            return self._fail("backend_unavailable",
                              "capture backend is not available on this platform", tool="screen.capture")
        scale = args.get("scale") or 1.0
        canvas_width = max(1, int(round(self._surface.canvas_width * scale)))
        canvas_width = self._fit_max_area(canvas_width)
        bbox = self._surface.region_to_physical_bbox(args.get("region"))
        try:
            payload = self._driver.capture(bbox=bbox, canvas_width=canvas_width,
                                           format=args.get("format") or "png",
                                           quality=args.get("quality") or 85,
                                           grayscale=bool(args.get("grayscale")))
        except DriverError as exc:
            return self._fail(exc.code, exc.message, tool="screen.capture", data=exc.data)
        self._frame += 1
        mime = "image/png" if payload.format == "png" else "image/jpeg"
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(payload.bytes).decode("ascii"))
        return self._ok({
            "frame": self._frame,
            "format": payload.format,
            "width": payload.width,
            "height": payload.height,
            "bytes": len(payload.bytes),
            "data_url": data_url,
            "canvas": self._surface.as_dict(),
        }, tool="screen.capture")

    def _fit_max_area(self, canvas_width: int) -> int:
        """Shrink the canvas so the resulting frame stays under the configured
        pixel-area cap (token cost fuse)."""
        max_area = self._get_cfg().capture.max_area
        if max_area <= 0:
            return canvas_width
        aspect = self._surface.physical_height / max(self._surface.physical_width, 1)
        while canvas_width * int(round(canvas_width * aspect)) > max_area and canvas_width > 64:
            canvas_width = max(64, int(canvas_width * 0.9))
        return canvas_width

    # pointer ---------------------------------------------------------------

    def _do_pointer_move(self, args: dict) -> ResultDict:
        x, y = self._surface.to_physical(args["x"], args["y"])
        self._driver.pointer_move(x, y, steps=args.get("steps") or 1)
        return self._ok({"position": {"x": args["x"], "y": args["y"]}}, tool="pointer.move")

    def _do_pointer_click(self, args: dict) -> ResultDict:
        position = None
        if args.get("x") is not None:
            x, y = self._surface.to_physical(args["x"], args["y"])
            self._driver.pointer_move(x, y, steps=1)
            position = {"x": args["x"], "y": args["y"]}
        self._driver.pointer_click(args["button"], args["times"], hold_ms=args.get("hold_ms") or 0)
        return self._ok({"button": args["button"], "times": args["times"], "position": position},
                        tool="pointer.click")

    def _do_pointer_drag(self, args: dict) -> ResultDict:
        fx, fy = self._surface.to_physical(args["from"]["x"], args["from"]["y"])
        tx, ty = self._surface.to_physical(args["to"]["x"], args["to"]["y"])
        self._driver.pointer_drag(fx, fy, tx, ty, args["button"],
                                  steps=args.get("steps") or 24, hold_ms=args.get("hold_ms") or 0)
        return self._ok({"from": {"x": args["from"]["x"], "y": args["from"]["y"]},
                         "to": {"x": args["to"]["x"], "y": args["to"]["y"]}},
                        tool="pointer.drag")

    def _do_pointer_scroll(self, args: dict) -> ResultDict:
        if args.get("x") is not None:
            x, y = self._surface.to_physical(args["x"], args["y"])
            self._driver.pointer_move(x, y, steps=1)
        self._driver.pointer_scroll(args["axis"], args["amount"])
        return self._ok({"axis": args["axis"], "amount": args["amount"]}, tool="pointer.scroll")

    # keyboard --------------------------------------------------------------

    def _do_keyboard_press(self, args: dict) -> ResultDict:
        self._driver.key_press(args["key"])
        return self._ok({"key": args["key"]}, tool="keyboard.press")

    def _do_keyboard_combo(self, args: dict) -> ResultDict:
        self._driver.key_combo(list(args["keys"]))
        return self._ok({"keys": args["keys"]}, tool="keyboard.combo")

    def _do_keyboard_type(self, args: dict) -> ResultDict:
        self._driver.key_type(args["text"], interval_ms=args.get("interval_ms") or 0)
        if args.get("submit"):
            self._driver.key_press("enter")
        return self._ok({"chars": len(args["text"]), "submit": bool(args.get("submit"))},
                        tool="keyboard.type")

    # wait ------------------------------------------------------------------

    def _do_wait_pause(self, args: dict) -> ResultDict:
        ms = int(args["ms"])
        max_ms = self._get_cfg().runtime.max_wait_ms
        if ms > max_ms:
            ms = max_ms
        time.sleep(ms / 1000.0)
        return self._ok({"waited_ms": ms}, tool="wait.pause")

    # accessibility ----------------------------------------------------------

    def _do_a11y_snapshot(self, args: dict) -> ResultDict:
        if not self._driver.capabilities.get("a11y"):
            return self._fail("backend_unavailable",
                              "accessibility backend is not available; install the optional UIA dependency on Windows",
                              tool="a11y.snapshot")
        level = args.get("level") or self._get_cfg().a11y.default_level
        options = {
            "level": level,
            "depth": args.get("depth"),
            "max_nodes": args.get("max_nodes"),
            "include_rects": args.get("include_rects", self._get_cfg().a11y.include_rects),
            "max_name_len": self._get_cfg().a11y.max_name_len,
            "hard_walk_cap": self._get_cfg().a11y.hard_walk_cap,
        }
        try:
            tree = self._driver.a11y_snapshot(options)
        except DriverError as exc:
            return self._fail(exc.code, exc.message, tool="a11y.snapshot", data=exc.data)
        self._snapshot_counter += 1
        self._current_snapshot_id = "snap-%d" % self._snapshot_counter
        tree["snapshot_id"] = self._current_snapshot_id
        return self._ok({
            "snapshot_id": self._current_snapshot_id,
            "node_count": tree.get("node_count", 0),
            "truncated": tree.get("truncated", False),
            "generated_at": tree.get("generated_at"),
            "tree": tree.get("tree", {}),
        }, tool="a11y.snapshot")

    def _do_a11y_activate(self, args: dict) -> ResultDict:
        stale = self._check_snapshot_id(args.get("snapshot_id"))
        if stale is not None:
            return stale
        if not self._driver.capabilities.get("a11y"):
            return self._fail("backend_unavailable",
                              "accessibility backend is not available", tool="a11y.activate")
        try:
            outcome = self._driver.a11y_activate(args["node_id"], method=args.get("method") or "auto")
        except DriverError as exc:
            return self._fail(self._a11y_error_code(exc), exc.message, tool="a11y.activate", data=exc.data)
        return self._ok(outcome, tool="a11y.activate")

    def _do_a11y_input(self, args: dict) -> ResultDict:
        stale = self._check_snapshot_id(args.get("snapshot_id"))
        if stale is not None:
            return stale
        if not self._driver.capabilities.get("a11y"):
            return self._fail("backend_unavailable",
                              "accessibility backend is not available", tool="a11y.input")
        try:
            outcome = self._driver.a11y_set_text(args["node_id"], args["text"])
        except DriverError as exc:
            return self._fail(self._a11y_error_code(exc), exc.message, tool="a11y.input", data=exc.data)
        return self._ok(outcome, tool="a11y.input")

    @staticmethod
    def _a11y_error_code(exc: DriverError) -> str:
        if exc.code == "element_stale":
            return "stale_snapshot"
        if exc.code == "unknown_node":
            return "invalid_arguments"
        return exc.code

    def _check_snapshot_id(self, snapshot_id) -> Optional[ResultDict]:
        if snapshot_id != self._current_snapshot_id:
            return self._fail("stale_snapshot",
                              "snapshot %r is not the current one (%r); take a fresh a11y.snapshot first"
                              % (snapshot_id, self._current_snapshot_id), tool="a11y")
        return None

    # batch -----------------------------------------------------------------

    def _do_batch_execute(self, args: dict) -> ResultDict:
        return self.run_batch(args["items"], continue_on_error=bool(args.get("continue_on_error")),
                              gap_ms=args.get("gap_ms"))

    def run_batch(self, items: List[dict], continue_on_error: bool = False,
                  gap_ms: Optional[int] = None) -> ResultDict:
        """Execute a list of {tool, arguments} items.

        Two phases: a pre-scan validates and gates every item (per-item
        failures become per-item error results, nothing runs yet), then the
        batch either waits for one confirmation (if any item is high-risk) or
        executes sequentially. Runtime failures after that are governed by
        ``continue_on_error``.
        """
        gap_ms = gap_ms if gap_ms is not None else self._get_cfg().runtime.batch_gap_ms

        plan = []
        confirm = None
        for item in items:
            tool = item.get("tool", "")
            spec = act.get_spec(tool)
            if spec is None:
                plan.append(("error", self._fail("unknown_tool", "no such tool: %r" % tool, tool="batch.execute")))
                continue
            try:
                args = act.clean_arguments(tool, self._apply_config_defaults(tool, item.get("arguments")))
            except act.ValidationError as exc:
                plan.append(("error", self._fail(
                    "invalid_arguments", "item %r: %s" % (tool, "; ".join(exc.issues)),
                    tool="batch.execute", data={"issues": exc.issues})))
                continue
            verdict = self._gate.evaluate(tool, args)
            if verdict.decision == "confirm":
                confirm = verdict
                plan.append(("run", tool, args))  # the confirming item runs after approval too
                break  # the whole batch waits; nothing has run yet
            if verdict.decision != "allow":
                plan.append(("error", self._verdict_failure(verdict, tool)))
            else:
                plan.append(("run", tool, args))

        if confirm is not None:
            pending = self._gate.pending_confirmation()
            if pending is not None:
                pending.payload = {
                    "items": plan,
                    "continue_on_error": continue_on_error,
                    "gap_ms": gap_ms,
                }
            return self._ok({
                "status": "awaiting_confirmation",
                "request_id": confirm.data["request_id"],
                "risk": confirm.risk,
                "reason": confirm.data.get("reason", ""),
            }, tool="batch.execute")

        return self._run_batch_payload({"items": plan, "continue_on_error": continue_on_error, "gap_ms": gap_ms})

    def _run_batch_payload(self, payload: dict) -> ResultDict:
        started = time.monotonic()
        plan = payload["items"]
        continue_on_error = bool(payload.get("continue_on_error"))
        gap_ms = payload.get("gap_ms") or 0

        results = []
        status = "completed"
        for index, entry in enumerate(plan):
            if index > 0 and gap_ms > 0:
                time.sleep(gap_ms / 1000.0)
            if entry[0] == "error":
                result = entry[1]
            else:
                _, tool, args = entry
                verdict = self._gate.evaluate_allow_only(tool, args)
                if verdict.decision == "allow":
                    result = self._execute(tool, args)
                else:
                    result = self._verdict_failure(verdict, tool)
            results.append(result)
            if not result.get("ok") and not continue_on_error:
                status = "aborted"
                break
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit("batch.finished", {"status": status, "items": len(results), "duration_ms": duration_ms})
        return self._ok({"status": status, "items": results}, tool="batch.execute")

    def _verdict_failure(self, verdict, tool: str) -> ResultDict:
        mapping = {
            "deny": ("policy_denied", {"rule": verdict.data.get("rule")}),
            "stopped": ("safety_stopped", None),
            "standby": ("safety_standby", None),
            "busy": ("busy", None),
        }
        code, data = mapping.get(verdict.decision, ("policy_denied", None))
        error = {"code": code, "message": verdict.reason}
        if data:
            error["data"] = data
        return {"ok": False, "result": None, "error": error, "meta": {}}

    # ------------------------------------------------------------- helpers

    def _attach_meta(self, result: ResultDict, tool: str, duration_ms: int) -> None:
        meta = result.setdefault("meta", {})
        meta["tool"] = tool
        meta["duration_ms"] = duration_ms
        meta["state"] = self._gate.state
        meta["frame"] = self._frame

    def _ok(self, result, tool: str = "") -> ResultDict:
        return {"ok": True, "result": result, "error": None, "meta": {}}

    def _fail(self, code: str, message: str, tool: str = "", data: Optional[dict] = None) -> ResultDict:
        error = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"ok": False, "result": None, "error": error, "meta": {}}
