"""The engine: executes validated actions against a driver, on top of the
surface geometry and behind the safety gate.

The engine is the only component that talks to drivers, so every action -
single, batch, or confirmation-approved - follows one code path and emits
the same event stream.
"""

from __future__ import annotations

import base64
import copy
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
        self._action_surface = surface
        self._gate = gate
        self._emit = emit
        self._get_cfg = config_provider
        self._frame = 0
        self._snapshot_counter = 0
        self._current_snapshot_id = None
        self._execution_lock = threading.RLock()
        self._batch_context = threading.local()
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

        pending = None
        with self._gate.admission_guard():
            verdict = self._gate.evaluate(
                tool, args, defer_confirmation_event=True,
                defer_confirmation_publication=True)
            if verdict.decision == "confirm":
                pending = self._gate.pending_confirmation()
                if tool == "batch.execute":
                    self._stash_batch_payload(args)
                request_id = verdict.data["request_id"]
                response = self._gate.build_confirmation_response(
                    request_id,
                    lambda: self._ok({
                        "status": "awaiting_confirmation",
                        "request_id": request_id,
                        "risk": verdict.risk,
                        "reason": verdict.data.get("reason", ""),
                    }, tool=tool),
                )
        if verdict.decision == "confirm":
            if response is None:
                self._gate.discard_confirmation_request(pending)
            elif not self._gate.emit_confirmation_requested(
                    request_id, deferred_pending=pending):
                response = None
        if verdict.decision == "deny":
            return self._fail("policy_denied", verdict.reason, tool=tool,
                              data={"rule": verdict.data.get("rule")})
        if verdict.decision == "stopped":
            return self._fail("safety_stopped", verdict.reason, tool=tool)
        if verdict.decision == "standby":
            return self._fail("safety_standby", verdict.reason, tool=tool)
        if verdict.decision == "busy":
            return self._fail("busy", verdict.reason, tool=tool)
        if verdict.decision == "not_authorized":
            return self._fail("not_authorized", verdict.reason, tool=tool,
                              data={"auth_profile": verdict.data.get("auth_profile")})
        if verdict.decision == "confirm":
            if response is None:
                return self._fail(
                    "confirmation_not_pending",
                    "confirmation is no longer pending",
                    tool=tool,
                    data={"request_id": request_id},
                )
            return response

        return self._execute(tool, args)

    def _stash_batch_payload(self, args: dict) -> None:
        """Remember the validated batch items on the pending confirmation so
        the approved run can execute them without re-gating for confirmation."""
        pending = self._gate.pending_confirmation()
        if pending is None:
            return
        plan, _, _ = self._build_batch_plan(args.get("items") or [])
        pending.payload = {
            "items": copy.deepcopy(plan),
            "continue_on_error": bool(args.get("continue_on_error")),
            "gap_ms": args.get("gap_ms"),
            "batch_authorizations": (copy.deepcopy(args),),
        }

    def _apply_config_defaults(self, tool: str, arguments: dict) -> dict:
        """Fill capture parameters from configuration when the caller did not
        provide them, so the configured defaults actually take effect."""
        if tool != "screen.capture":
            return arguments
        if not isinstance(arguments, dict):
            return arguments
        args = dict(arguments)
        cfg = self._get_cfg()
        args.setdefault("format", cfg.capture.default_format)
        args.setdefault("quality", cfg.capture.default_quality)
        args.setdefault("grayscale", cfg.capture.grayscale)
        return args

    def _on_confirmation_approved(self, pending: PendingConfirmation) -> None:
        """Called by the gate when a pending confirmation is approved."""
        if pending.tool == "batch.execute":
            payload = pending.payload
            if not payload:
                # The stash window was missed (the confirmation was created
                # before the engine could attach the plan). Rebuild from the
                # snapshotted arguments: _build_batch_plan re-validates and
                # re-gates every item, so nothing runs ungated.
                args = pending.arguments or {}
                plan, _, _ = self._build_batch_plan(args.get("items") or [])
                payload = {"items": plan,
                           "continue_on_error": bool(args.get("continue_on_error")),
                           "gap_ms": args.get("gap_ms"),
                           "batch_authorizations": (copy.deepcopy(args),)}
            batch_authorizations = (payload.get("batch_authorizations")
                                   or (pending.arguments or {},))
            started = time.monotonic()
            verdict = self._gate.admit_only(pending.tool, pending.arguments or {})
            self._emit("action.started", {"tool": pending.tool, "arguments": pending.arguments})
            if verdict.decision == "allow":
                with self._gate.admission_guard():
                    parent_verdict = self._batch_authorization_failure(batch_authorizations)
                    if parent_verdict is None:
                        current_verdict = self._gate.evaluate_allow_only(
                            pending.tool, pending.arguments or {})
                        if current_verdict.decision != "allow":
                            parent_verdict = current_verdict
                if parent_verdict is not None:
                    result = self._verdict_failure(parent_verdict, pending.tool)
                else:
                    result = self._run_batch_payload(payload, batch_authorizations)
            else:
                result = self._verdict_failure(verdict, pending.tool)
            self._emit("action.finished", {
                "tool": pending.tool,
                "ok": bool(result.get("ok")),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_code": (result.get("error") or {}).get("code"),
            })
            return
        self._execute(pending.tool, pending.arguments, announce_refusal=True)

    def _execute(self, tool: str, args: dict, announce_refusal: bool = False,
                 batch_authorizations=None) -> ResultDict:
        started = time.monotonic()
        batch_authorizations = tuple(batch_authorizations or ())
        parent_verdict = None
        parent_refused_after_start = False
        started_emitted = False

        def refuse_started_action(verdict):
            nonlocal parent_refused_after_start
            parent_refused_after_start = True
            result = self._verdict_failure(verdict, tool)
            self._emit("action.finished", {
                "tool": tool,
                "ok": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_code": (result.get("error") or {}).get("code"),
            })
            return result

        def admitted():
            nonlocal parent_verdict
            if tool == "batch.execute":
                with self._gate.admission_guard():
                    parent_verdict = self._batch_authorization_failure(batch_authorizations)
                    if parent_verdict is None:
                        current_verdict = self._gate.evaluate_allow_only(tool, args)
                        if current_verdict.decision != "allow":
                            parent_verdict = current_verdict
            elif batch_authorizations:
                # execute_admitted rechecks this tool and its arguments after
                # action.started, then holds admission through this parent
                # check and the dispatch.
                parent_verdict = self._batch_authorization_failure(batch_authorizations)
            if parent_verdict is not None:
                return (refuse_started_action(parent_verdict)
                        if tool == "batch.execute" else None)
            previous_batch_authorizations = getattr(
                self._batch_context, "authorizations", None)
            if tool == "batch.execute":
                self._batch_context.authorizations = batch_authorizations + (args,)
            try:
                result = self._dispatch(tool, args)
                self._gate.note_activity()
            except DriverError as exc:
                result = self._fail(exc.code, exc.message, tool=tool, data=exc.data)
            except Exception as exc:  # defensive: drivers must not take the server down
                result = self._fail("driver_failed", "unexpected driver error: %s" % exc, tool=tool)
            finally:
                if tool == "batch.execute":
                    if previous_batch_authorizations is None:
                        del self._batch_context.authorizations
                    else:
                        self._batch_context.authorizations = previous_batch_authorizations
            return result

        # Every execution - plain, batch-internal, or confirmation-approved -
        # passes the gate again right here. For single actions the final check
        # and the dispatch run under the gate's admission lock, so a
        # session.configure / grant_profile cannot land a downgrade between
        # the two: an approval (or a batch pre-scan) collected before the host
        # narrowed the authorization profile is refused here instead of
        # executing.
        #
        # batch.execute is the exception: its dispatch is a loop of sub-actions,
        # so holding the admission lock for the whole loop would prevent a
        # downgrade from taking effect between items. The batch releases its
        # final admission check before running the loop. Each child then checks
        # its own and every enclosing batch authorization under admission, with
        # the check held through that child's dispatch.
        if tool == "batch.execute":
            with self._gate.admission_guard():
                parent_verdict = self._batch_authorization_failure(batch_authorizations)
                verdict = (parent_verdict or
                           self._gate.evaluate_allow_only(tool, args))
            if parent_verdict is not None:
                return self._refused(tool, args, parent_verdict, announce_refusal)
            if verdict.decision != "allow":
                return self._refused(tool, args, verdict, announce_refusal)
            with self._execution_lock:
                started_emitted = True
                self._emit("action.started", {"tool": tool, "arguments": args})
                result = admitted()
            if parent_refused_after_start:
                return result
        else:
            with self._execution_lock:
                verdict = self._gate.admit_only(tool, args)
                if verdict.decision == "allow" and batch_authorizations:
                    with self._gate.admission_guard():
                        parent_verdict = self._batch_authorization_failure(
                            batch_authorizations)
                if verdict.decision == "allow" and parent_verdict is None:
                    started_emitted = True
                    self._emit("action.started", {"tool": tool, "arguments": args})
                    verdict, result = self._gate.execute_admitted(
                        tool, args, admitted)
            if verdict.decision != "allow":
                if parent_verdict is not None:
                    return self._refused(tool, args, parent_verdict,
                                         announce_refusal)
                if started_emitted:
                    return refuse_started_action(verdict)
                return self._refused(tool, args, verdict, announce_refusal)
            if parent_verdict is not None:
                if started_emitted:
                    return refuse_started_action(parent_verdict)
                return self._refused(tool, args, parent_verdict,
                                     announce_refusal)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit("action.finished", {
            "tool": tool,
            "ok": result.get("ok", False),
            "duration_ms": duration_ms,
            "error_code": (result.get("error") or {}).get("code"),
        })
        self._attach_meta(result, tool, duration_ms)
        return result

    def _batch_authorization_failure(self, batch_authorizations):
        for arguments in batch_authorizations:
            verdict = self._gate.evaluate_allow_only("batch.execute", arguments)
            if verdict.decision != "allow":
                return verdict
        return None

    def _refused(self, tool: str, args: dict, verdict, announce_refusal: bool) -> ResultDict:
        result = self._verdict_failure(verdict, tool)
        if announce_refusal:
            # Some refusal paths (approved actions and batch items rejected
            # after a downgrade) have no separate per-action event envelope.
            # Emit the pair so subscribers see a complete refusal record.
            self._emit("action.started", {"tool": tool, "arguments": args})
            self._emit("action.finished", {
                "tool": tool,
                "ok": False,
                "duration_ms": 0,
                "error_code": (result.get("error") or {}).get("code"),
            })
        return result

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, tool: str, args: dict) -> ResultDict:
        handler = getattr(self, "_do_" + tool.replace(".", "_"), None)
        if handler is None:
            return self._fail("driver_failed", "no handler for %r" % tool, tool=tool)
        return handler(args)

    # screen.capture --------------------------------------------------------

    def _do_screen_capture(self, args: dict, grid: bool = False) -> ResultDict:
        if args.get("show_grid"):
            return self._fail("backend_unavailable", "show_grid is not yet available", tool="screen.capture")
        if not self._driver.capabilities.get("capture"):
            return self._fail("backend_unavailable",
                              "capture backend is not available on this platform", tool="screen.capture")
        if grid and not getattr(self._driver, "supports_capture_grid", False):
            return self._fail("backend_unavailable", "capture backend does not support grid overlays",
                              tool="screen.capture")
        try:
            bbox = self._surface.region_to_physical_bbox(args.get("region"))
        except ValueError as exc:
            return self._fail("invalid_arguments", str(exc), tool="screen.capture")
        scale = args.get("scale") or 1.0
        canvas_width = max(1, int(round(self._surface.canvas_width * scale)))
        canvas_width, canvas_height = self._fit_max_area(canvas_width, bbox)
        try:
            options = {"grid": True} if grid else {}
            payload = self._driver.capture_with_dimensions(
                bbox=bbox, canvas_width=canvas_width, canvas_height=canvas_height,
                format=args.get("format") or "png",
                quality=args.get("quality") or 85,
                grayscale=bool(args.get("grayscale")), **options)
        except DriverError as exc:
            return self._fail(exc.code, exc.message, tool="screen.capture", data=exc.data)
        except ValueError as exc:
            return self._fail("invalid_arguments", str(exc), tool="screen.capture")
        capture_surface = Surface(
            payload.width, payload.height,
            bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1],
        )
        self._action_surface = capture_surface
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
            "canvas": capture_surface.as_dict(),
            "capture_geometry": {
                "physical_bounds": {
                    "x": bbox[0], "y": bbox[1],
                    "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1],
                },
                "physical_units": "pixels",
                "image": {"width": payload.width, "height": payload.height, "units": "pixels"},
                "action_frame": {
                    "frame": self._frame,
                    "type": "capture_relative",
                    "target": "virtual_desktop",
                    "units": "image_pixels",
                    "image_to_action_scale": {"x": 1, "y": 1},
                    "physical_origin": {"x": bbox[0], "y": bbox[1]},
                    "physical_pixels_per_unit": capture_surface.scale,
                },
            },
        }, tool="screen.capture")

    def _fit_max_area(self, canvas_width: int, bbox: tuple) -> tuple:
        """Fit the capture dimensions under the configured pixel-area cap."""
        max_area = self._get_cfg().capture.max_area
        left, top, right, bottom = bbox
        aspect = (bottom - top) / max(right - left, 1)
        canvas_height = max(1, int(round(canvas_width * aspect)))
        if max_area <= 0:
            return canvas_width, canvas_height
        while canvas_width * canvas_height > max_area and canvas_width > 1:
            canvas_width = max(1, int(canvas_width * 0.9))
            canvas_height = max(1, int(round(canvas_width * aspect)))
        if canvas_width * canvas_height > max_area:
            canvas_height = max_area // canvas_width
        return canvas_width, canvas_height

    # pointer ---------------------------------------------------------------

    def _do_pointer_move(self, args: dict) -> ResultDict:
        x, y = self._action_surface.to_physical(args["x"], args["y"])
        steps = max(1, int(args.get("steps") or 1))
        self._driver.pointer_move(x, y, steps=steps)
        return self._ok({"position": {"x": args["x"], "y": args["y"]}}, tool="pointer.move")

    def _do_pointer_click(self, args: dict) -> ResultDict:
        position = None
        if args.get("x") is not None and args.get("y") is not None:
            x, y = self._action_surface.to_physical(args["x"], args["y"])
            self._driver.pointer_move(x, y, steps=1)
            position = {"x": args["x"], "y": args["y"]}
        times = max(1, int(args.get("times") or 1))
        hold_ms = max(0, int(args.get("hold_ms") or 0))
        self._driver.pointer_click(args["button"], times, hold_ms=hold_ms)
        return self._ok({"button": args["button"], "times": times, "position": position},
                        tool="pointer.click")

    def _do_pointer_drag(self, args: dict) -> ResultDict:
        fx, fy = self._action_surface.to_physical(args["from"]["x"], args["from"]["y"])
        tx, ty = self._action_surface.to_physical(args["to"]["x"], args["to"]["y"])
        steps = max(1, int(args.get("steps") or 24))
        hold_ms = max(0, int(args.get("hold_ms") or 0))
        self._driver.pointer_drag(fx, fy, tx, ty, args["button"],
                                  steps=steps, hold_ms=hold_ms)
        return self._ok({"from": {"x": args["from"]["x"], "y": args["from"]["y"]},
                         "to": {"x": args["to"]["x"], "y": args["to"]["y"]}},
                        tool="pointer.drag")

    def _do_pointer_scroll(self, args: dict) -> ResultDict:
        if args.get("x") is not None and args.get("y") is not None:
            x, y = self._action_surface.to_physical(args["x"], args["y"])
            self._driver.pointer_move(x, y, steps=1)
        self._driver.pointer_scroll(args["axis"], float(args["amount"]))
        return self._ok({"axis": args["axis"], "amount": args["amount"]}, tool="pointer.scroll")

    # keyboard --------------------------------------------------------------

    def _do_keyboard_press(self, args: dict) -> ResultDict:
        self._driver.key_press(args["key"])
        return self._ok({"key": args["key"]}, tool="keyboard.press")

    def _do_keyboard_combo(self, args: dict) -> ResultDict:
        self._driver.key_combo(list(args["keys"]))
        return self._ok({"keys": args["keys"]}, tool="keyboard.combo")

    def _do_keyboard_type(self, args: dict) -> ResultDict:
        interval_ms = max(0, int(args.get("interval_ms") or 0))
        self._driver.key_type(args["text"], interval_ms=interval_ms)
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
                              gap_ms=args.get("gap_ms"),
                              batch_authorizations=getattr(
                                  self._batch_context, "authorizations", None))

    def _build_batch_plan(self, items: List[dict]):
        plan = []
        highest_risk = None
        confirm_reason = None
        risk_ranks = {"benign": 0, "moderate": 1, "high": 2}
        threshold_rank = risk_ranks.get(self._get_cfg().safety.confirm_threshold, 2)
        max_rank = -1

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

            verdict = self._gate.evaluate_allow_only(tool, args)
            if verdict.decision != "allow":
                plan.append(("refused", tool, args, verdict))
                continue

            plan.append(("run", tool, args))
            risk = self._gate.risk_for(tool, args)
            rank = risk_ranks.get(risk, 0)
            if rank > max_rank:
                max_rank = rank
            if rank >= threshold_rank:
                if highest_risk is None or rank > risk_ranks.get(highest_risk, 0):
                    highest_risk = risk
                    confirm_reason = "batch item %s has risk %s >= confirm threshold %s" % (
                        tool, risk, self._get_cfg().safety.confirm_threshold
                    )

        return plan, highest_risk, confirm_reason

    def run_batch(self, items: List[dict], continue_on_error: bool = False,
                  gap_ms: Optional[int] = None,
                  batch_authorizations=None) -> ResultDict:
        """Execute a list of {tool, arguments} items.

        Two phases: a pre-scan validates and gates every item (per-item
        failures become per-item error results, nothing runs yet), then the
        batch either waits for one confirmation (if any item is high-risk) or
        executes sequentially. Runtime failures after that are governed by
        ``continue_on_error``.
        """
        gap_ms = gap_ms if gap_ms is not None else self._get_cfg().runtime.batch_gap_ms
        if batch_authorizations is None:
            batch_authorizations = ({
                "items": items,
                "continue_on_error": continue_on_error,
                "gap_ms": gap_ms,
            },)
        else:
            batch_authorizations = tuple(batch_authorizations)
        plan, needs_confirm_risk, confirm_reason = self._build_batch_plan(items)

        if needs_confirm_risk is not None:
            arguments = {
                "items": items, "continue_on_error": continue_on_error, "gap_ms": gap_ms
            }
            with self._gate.admission_guard():
                verdict = self._gate.evaluate(
                    "batch.execute", arguments, defer_confirmation_event=True,
                    defer_confirmation_publication=True)
                if verdict.decision == "confirm":
                    pending = self._gate.pending_confirmation()
                    if pending is not None:
                        pending.payload = {
                            "items": copy.deepcopy(plan),
                            "continue_on_error": continue_on_error,
                            "gap_ms": gap_ms,
                            "batch_authorizations": copy.deepcopy(batch_authorizations),
                        }
                    request_id = verdict.data["request_id"]
                    response = self._gate.build_confirmation_response(
                        request_id,
                        lambda: self._ok({
                            "status": "awaiting_confirmation",
                            "request_id": request_id,
                            "risk": verdict.risk,
                            "reason": verdict.data.get("reason", ""),
                        }, tool="batch.execute"),
                    )
            if verdict.decision == "confirm":
                if (response is not None
                        and not self._gate.emit_confirmation_requested(
                            request_id, deferred_pending=pending)):
                    response = None
                if response is None:
                    return self._fail(
                        "confirmation_not_pending",
                        "confirmation is no longer pending",
                        tool="batch.execute",
                        data={"request_id": request_id},
                    )
                return response
            if verdict.decision != "allow":
                return self._verdict_failure(verdict, "batch.execute")

        return self._run_batch_payload({
            "items": plan,
            "continue_on_error": continue_on_error,
            "gap_ms": gap_ms,
            "batch_authorizations": batch_authorizations,
        }, batch_authorizations)

    def _run_batch_payload(self, payload: dict, batch_authorizations=None) -> ResultDict:
        started = time.monotonic()
        plan = payload["items"]
        continue_on_error = bool(payload.get("continue_on_error"))
        gap_ms = payload.get("gap_ms") or 0
        if batch_authorizations is None:
            batch_authorizations = payload.get("batch_authorizations", ())
        else:
            batch_authorizations = tuple(batch_authorizations)

        results = []
        status = "completed"
        for index, entry in enumerate(plan):
            if index > 0 and gap_ms > 0:
                time.sleep(gap_ms / 1000.0)
            if entry[0] == "error":
                result = entry[1]
            elif entry[0] == "refused":
                _, tool, args, verdict = entry
                result = self._refused(tool, args, verdict, announce_refusal=True)
            else:
                _, tool, args = entry
                verdict = self._gate.evaluate_allow_only(tool, args)
                if verdict.decision == "allow":
                    result = self._execute(
                        tool, args, announce_refusal=True,
                        batch_authorizations=batch_authorizations)
                else:
                    result = self._refused(tool, args, verdict, announce_refusal=True)
            results.append(result)
            if not result.get("ok") and not continue_on_error:
                status = "aborted"
                break
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit("batch.finished", {
            "status": status,
            "items": len(results),
            "results": results,
            "duration_ms": duration_ms,
        })
        return self._ok({"status": status, "items": results}, tool="batch.execute")

    def _verdict_failure(self, verdict, tool: str) -> ResultDict:
        mapping = {
            "deny": ("policy_denied", {"rule": verdict.data.get("rule")}),
            "stopped": ("safety_stopped", None),
            "standby": ("safety_standby", None),
            "busy": ("busy", None),
            "not_authorized": ("not_authorized", {"auth_profile": verdict.data.get("auth_profile")}),
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
