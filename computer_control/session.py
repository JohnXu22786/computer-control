"""Session: one lifecycle of the plugin inside a harness.

A session owns the driver, the surface geometry, the safety gate, the engine
and a single worker thread that executes actions serially. Starting a session
is when everything is wired together; stopping it tears everything down.

State machine (visible via system.status / events):

    idle --session.start--> ready
    ready --confirm requested--> confirming   (implicit; the gate owns it)
    ready --panic/hotkey/panic file--> stopped
    ready --idle timeout--> standby
    stopped/standby --session.resume--> ready
    any --session.stop--> idle
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional

from computer_control import actions as act
from computer_control.config import Config, apply_overrides, default_config, from_dict
from computer_control.drivers import create_driver
from computer_control.drivers.base import BaseDriver
from computer_control.engine import Engine
from computer_control.geometry import Surface
from computer_control.policy import SafetyGate

# Sections that may change on a running session via session.configure.
_CONFIGURABLE_KEYS = ("capture", "safety", "a11y", "runtime")

# Fields that are fixed at session.start and rejected by session.configure.
_IMMUTABLE_FIELDS = {
    "capture": {"backend"},
    "safety": {"emergency_hotkey", "panic_file", "visual_indicator"},
}


class SessionError(Exception):
    def __init__(self, code: str, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class Session:
    """Embeddable session. Emits (event_type, payload) via ``emit``.

    ``base_config`` is a raw dict (usually the plugin's config file); it is
    the base that ``session.start`` parameters override. Without it, built-in
    defaults are used.
    """

    def __init__(self, emit: Callable[[str, Optional[dict]], None],
                 driver_factory: Optional[Callable[[Config], BaseDriver]] = None,
                 base_config: Optional[dict] = None):
        self._emit = emit
        self._driver_factory = driver_factory or create_driver
        self._base_config = base_config or {}
        self._lock = threading.RLock()
        self._worker_queue: Optional[queue.Queue] = None
        self._worker: Optional[threading.Thread] = None
        self._driver: Optional[BaseDriver] = None
        self._surface: Optional[Surface] = None
        self._gate: Optional[SafetyGate] = None
        self._engine: Optional[Engine] = None
        self._cfg = default_config()
        self._started_at: Optional[float] = None
        self._action_count = 0
        self._frame_counter = 0
        self._stop_requested = threading.Event()

    # ------------------------------------------------------------ lifecycle

    @property
    def state(self) -> str:
        with self._lock:
            if self._gate is None:
                return "idle"
            return self._gate.state

    @property
    def started(self) -> bool:
        with self._lock:
            return self._gate is not None

    def start(self, raw_config: Optional[dict]) -> dict:
        with self._lock:
            if self._gate is not None:
                raise SessionError("already_started", "session is already running; call session.stop first")
            try:
                base = from_dict(self._base_config) if self._base_config else default_config()
                cfg = apply_overrides(base, raw_config or {})
            except ValueError as exc:
                raise SessionError("invalid_config", "configuration rejected: %s" % exc)
            self._cfg = cfg
            try:
                driver = self._driver_factory(cfg)
            except Exception as exc:
                raise SessionError("backend_unavailable", "cannot create platform driver: %s" % exc)
            surface = self._read_surface(cfg, driver)
            gate = SafetyGate(cfg.safety, emit=self._emit,
                              driver_hotkey_probe=lambda keys: driver.hotkey_probe(keys))
            engine = Engine(driver, surface, gate, self._emit, lambda: self._cfg)
            self._driver = driver
            self._surface = surface
            self._gate = gate
            self._engine = engine
            self._started_at = time.time()
            self._action_count = 0
            self._frame_counter = 0
            self._stop_requested.clear()
            self._worker_queue = queue.Queue()
            self._worker = threading.Thread(target=self._worker_loop, name="session-worker", daemon=True)
            self._worker.start()
            # Approved confirmations execute on the worker thread, serialized
            # with every other action.
            def queue_approval(pending):
                outcome = threading.Event()
                holder = {"result": None}
                self._worker_queue.put((lambda: engine._on_confirmation_approved(pending), outcome, holder))

            gate.on_approved = queue_approval
            gate.on_panic_state = self._on_panic_state
            gate.start_watchdog()
            self._emit("session.started", {"state": self.state, "surface": surface.as_dict(),
                                           "capabilities": driver.capabilities})
            return {"ok": True, "result": {"state": self.state, "surface": surface.as_dict(),
                                           "capabilities": driver.capabilities},
                    "error": None, "meta": {}}

    def _read_surface(self, cfg: Config, driver: BaseDriver) -> Surface:
        info = getattr(driver, "desktop_info", lambda: None)()
        if info and info.get("virtual_screen"):
            vs = info["virtual_screen"]
            if vs.get("width", 0) > 0 and vs.get("height", 0) > 0:
                return Surface.from_physical(cfg.capture.default_width, vs["x"], vs["y"], vs["width"], vs["height"])
        return Surface.from_physical(cfg.capture.default_width, 0, 0, cfg.capture.default_width,
                                     int(cfg.capture.default_width * 9 / 16))

    def stop(self) -> dict:
        """Stop the session.

        The gate is suspended first so queued and future actions are blocked;
        queued-but-not-started tasks are answered immediately with
        safety_stopped. The currently executing action (hardware injection)
        cannot be interrupted mid-flight and is allowed to finish.
        """
        with self._lock:
            if self._gate is None:
                raise SessionError("not_started", "no session is running")
            gate = self._gate
            engine = self._engine
            queue_ = self._worker_queue
            self._gate = None
            self._engine = None
            self._driver = None
            self._stop_requested.set()
        gate.suspend()
        gate.close()
        # answer every queued-but-unstarted task immediately
        while True:
            try:
                _, outcome, holder = queue_.get_nowait()
            except Exception:
                break
            holder["result"] = _error("safety_stopped", "session stopped before the action ran")
            outcome.set()
        worker = self._worker
        in_flight_note = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=30.0)
            if worker.is_alive():
                in_flight_note = "in-flight action still running"
        if engine is not None:
            engine.close()
        self._hide_indicator_if_any()
        payload = {"note": in_flight_note} if in_flight_note else {}
        self._emit("session.stopped", payload)
        return {"ok": True, "result": {"state": "idle"}, "error": None, "meta": {}}

    # ------------------------------------------------------------- config

    def configure(self, patch: dict) -> dict:
        with self._lock:
            if self._gate is None:
                raise SessionError("not_started", "no session is running")
            denied_sections = set((patch or {})) - set(_CONFIGURABLE_KEYS)
            if denied_sections:
                raise SessionError("invalid_config",
                                   "not configurable at runtime: %s" % ", ".join(sorted(denied_sections)))
            immutable = []
            for section, fields in _IMMUTABLE_FIELDS.items():
                section_patch = (patch or {}).get(section)
                if isinstance(section_patch, dict):
                    for field in fields:
                        if field in section_patch:
                            immutable.append("%s.%s" % (section, field))
            if immutable:
                raise SessionError("invalid_config",
                                   "immutable after session.start: %s" % ", ".join(sorted(immutable)))
            try:
                new_cfg = apply_overrides(self._cfg, patch or {})
            except ValueError as exc:
                raise SessionError("invalid_config", "configuration rejected: %s" % exc)
            self._cfg = new_cfg
            self._gate.reconfigure(new_cfg.safety)
            self._emit("session.configured", {"patch": patch or {}})
            return {"ok": True, "result": {"state": self.state, "applied": patch or {}},
                    "error": None, "meta": {}}

    # --------------------------------------------------------------- calls

    def call(self, tool: str, arguments: dict) -> dict:
        spec = act.get_spec(tool)
        if spec is None:
            return {"ok": False, "result": None,
                    "error": {"code": "unknown_tool", "message": "no such tool: %r" % tool}, "meta": {}}
        return self._submit(tool, arguments)

    def call_batch(self, items: List[dict], continue_on_error: bool = False, gap_ms: Optional[int] = None) -> dict:
        return self._submit("batch.execute", {
            "items": items, "continue_on_error": continue_on_error, "gap_ms": gap_ms,
        })

    def _submit(self, tool: str, arguments: dict) -> dict:
        with self._lock:
            if self._gate is None:
                return _error("not_started", "no session is running; call session.start first")
            engine = self._engine
            queue_ = self._worker_queue
            # The configured max_wait_ms governs how long an action may run;
            # the +60s grace covers queueing behind earlier actions. A tiny
            # floor keeps degenerate configs (max_wait_ms=1) from timing out
            # before the task even starts.
            timeout_s = max(0.5, self._cfg.runtime.max_wait_ms / 1000.0 + 60.0)
        outcome = threading.Event()
        holder = {"result": _error("driver_failed", "the action raised an internal error")}
        task = (lambda: holder.update(result=engine.run_tool(tool, arguments)), outcome, holder)

        queue_.put(task)
        if not outcome.wait(timeout_s):
            # The task may still execute afterwards (it is queued); report
            # honestly that the outcome is unknown rather than a failure.
            return _error("timeout",
                          "no result within %.0fs; the action may still have executed" % timeout_s)
        result = holder["result"]
        if result.get("ok"):
            with self._lock:
                self._action_count += 1
                if tool == "screen.capture" and result.get("result", {}).get("frame"):
                    self._frame_counter = result["result"]["frame"]
        return result

    def confirm(self, request_id: str, approve: bool) -> dict:
        with self._lock:
            if self._gate is None:
                return _error("not_started", "no session is running")
            gate = self._gate
        try:
            pending = gate.resolve(request_id, bool(approve))
        except Exception as exc:
            return _error(getattr(exc, "code", "confirmation_not_found"), str(exc))
        if pending.status == "approved":
            self._note_activity()
        return {"ok": True, "result": {"request_id": request_id, "status": pending.status},
                "error": None, "meta": {"state": gate.state}}

    def resume(self) -> dict:
        with self._lock:
            if self._gate is None:
                return _error("not_started", "no session is running")
            gate = self._gate
        gate.resume()
        self._hide_indicator_if_any()
        return {"ok": True, "result": {"state": gate.state}, "error": None, "meta": {}}

    def panic(self, on: bool) -> dict:
        with self._lock:
            if self._gate is None:
                return _error("not_started", "no session is running")
            gate = self._gate
        if on:
            gate.trigger_panic(source="protocol")
            self._show_indicator_if_any()
        else:
            gate.release_panic(source="protocol")
            self._hide_indicator_if_any()
        return {"ok": True, "result": {"panic": bool(on), "state": gate.state}, "error": None, "meta": {}}

    # ------------------------------------------------------------- queries

    def list_tools(self) -> dict:
        with self._lock:
            capabilities = self._driver.capabilities if self._driver else {}
            state = self.state
        tools = []
        for name in act.tool_names():
            spec = act.get_spec(name)
            available = self._tool_available(name, capabilities)
            entry = spec.to_dict()
            entry["available"] = available
            tools.append(entry)
        return {"ok": True, "result": {"tools": tools, "state": state}, "error": None, "meta": {}}

    def _tool_available(self, tool: str, capabilities: dict) -> bool:
        if tool.startswith("a11y."):
            return bool(capabilities.get("a11y"))
        if tool == "screen.capture":
            return bool(capabilities.get("capture"))
        return True

    def status(self) -> dict:
        with self._lock:
            gate = self._gate
            driver = self._driver
            surface = self._surface
            started_at = self._started_at
            action_count = self._action_count
            frame = self._frame_counter
        if gate is None:
            return {"ok": True, "result": {"state": "idle", "started_at": None}, "error": None, "meta": {}}
        return {
            "ok": True,
            "result": {
                "state": gate.state,
                "platform": driver.platform_name,
                "capabilities": driver.capabilities,
                "surface": surface.as_dict(),
                "started_at": started_at,
                "action_count": action_count,
                "frame": frame,
                "pending_confirmation": self._pending_info(),
            },
            "error": None,
            "meta": {},
        }

    def _pending_info(self) -> Optional[dict]:
        with self._lock:
            if self._gate is None:
                return None
            pending = self._gate.pending_confirmation()
        if pending is None:
            return None
        return {
            "request_id": pending.request_id,
            "tool": pending.tool,
            "risk": pending.risk,
            "reason": pending.reason,
            "expires_in_s": max(0.0, pending.expires_at - time.monotonic()),
        }

    def _note_activity(self) -> None:
        with self._lock:
            if self._gate is not None:
                self._gate.note_activity()

    # ------------------------------------------------------- worker thread

    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                task = self._worker_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            run, outcome, holder = task
            try:
                run()
            except Exception as exc:  # pragma: no cover - defensive
                holder["result"] = _error("driver_failed", "the action raised an internal error: %s" % exc)
            finally:
                outcome.set()

    # ------------------------------------------------------- stop indicator

    def _on_panic_state(self, active: bool, source: str) -> None:
        """Show/hide the STOP banner for every panic source (hotkey, protocol,
        panic file), not just protocol-triggered ones."""
        try:
            if active:
                self._show_indicator_if_any()
            else:
                self._hide_indicator_if_any()
        except Exception:
            pass

    def _show_indicator_if_any(self) -> None:
        if not self._cfg.safety.visual_indicator:
            return
        if getattr(self, "_indicator", None) is None:
            try:
                from computer_control.overlay import StopIndicator

                self._indicator = StopIndicator(self._cfg.safety.emergency_hotkey)
            except Exception:
                self._indicator = None
        indicator = self._indicator
        if indicator is not None:
            try:
                indicator.show()
            except Exception:
                pass

    def _hide_indicator_if_any(self) -> None:
        indicator = getattr(self, "_indicator", None)
        if indicator is not None:
            try:
                indicator.hide()
            except Exception:
                pass


def _error(code: str, message: str) -> dict:
    return {"ok": False, "result": None,
            "error": {"code": code, "message": message}, "meta": {}}
