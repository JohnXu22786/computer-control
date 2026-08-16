"""The safety gate: the single choke point every action passes through.

It enforces, in order:
  1. operational state  - stopped (emergency stop) / standby (idle) block everything
  2. allow/deny rules   - an explicit deny always wins over allows; allowlist
                          mode when the default rule is deny
  3. risk confirmation  - actions at or above the configured risk threshold
                          wait for a human approval before they may run

It also owns the emergency-stop machinery (hotkey polling, panic file,
protocol-triggered panic) and the idle watchdog that puts the session into
standby after inactivity.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from computer_control.actions import RISK_LEVELS, risk_for
from computer_control.config import SafetyConfig

_STATE_READY = "ready"
_STATE_CONFIRMING = "confirming"
_STATE_STOPPED = "stopped"
_STATE_STANDBY = "standby"

_RISK_ORDER = {"benign": 0, "moderate": 1, "high": 2}

# Aliases that refer to the same physical key must not be able to dodge risk
# classification or rule matching.
_WIN_LIKE = frozenset({"win", "lwin", "rwin", "super", "meta"})
_CTRL_LIKE = frozenset({"ctrl", "lctrl", "rctrl"})
_ALT_LIKE = frozenset({"alt", "lalt", "ralt"})


class PolicyError(Exception):
    """A safety policy violation surfaced to the caller."""

    def __init__(self, code: str, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass
class GateVerdict:
    """Result of evaluating one action against the gate."""

    decision: str  # allow | deny | confirm | stopped | standby | busy
    reason: str = ""
    risk: str = RISK_LEVELS[0]
    data: dict = field(default_factory=dict)


@dataclass
class PendingConfirmation:
    request_id: str
    tool: str
    arguments: dict
    risk: str
    reason: str
    created_at: float
    expires_at: float
    payload: dict = field(default_factory=dict)
    status: str = "pending"  # pending | approved | denied | expired | cancelled
    resolved_at: Optional[float] = None


class EmitRecorder:
    """Collects (event_type, payload) pairs; handy for tests and embedding."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event_type, payload=None):
        with self.lock:
            self.events.append((event_type, payload))

    def types(self):
        with self.lock:
            return [t for t, _ in self.events]

    def dump(self):
        with self.lock:
            return list(self.events)


def _glob_match(pattern, text: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(text, pattern)


def _contains_any(haystack: str, needle: str) -> bool:
    return needle in haystack


def _modifier_family(name: str) -> frozenset:
    """The alias family of a modifier name ('lwin' -> the win family), or the
    empty set for non-modifiers."""
    lowered = name.lower()
    for family in (_WIN_LIKE, _CTRL_LIKE, _ALT_LIKE):
        if lowered in family:
            return family
    return frozenset()


def _contains_match(value, needle: str) -> bool:
    """'contains' matching that also understands modifier aliases, so a rule
    written for 'win' catches lwin/rwin/super/meta."""
    if needle in value:
        return True
    family = _modifier_family(needle)
    if not family:
        return False
    return bool(family & _modifier_family(value))


def _matches_argument(rule, value) -> bool:
    """Match a single argument value against a rule's argument spec."""
    if rule.argument_name is None:
        return True
    if value is None:
        return False
    if rule.matcher == "equals":
        return value == rule.value
    if rule.matcher == "glob":
        return isinstance(value, str) and _glob_match(rule.value, value)
    if rule.matcher == "contains":
        if isinstance(value, list):
            return any(_contains_match(str(v), str(rule.value)) for v in value)
        return isinstance(value, str) and _contains_match(value, str(rule.value))
    return False


class SafetyGate:
    """Thread-safe policy engine. All public methods are safe to call from any
    thread; the polling and watchdog threads are started with start_watchdog."""

    def __init__(self, cfg: SafetyConfig, emit: Callable[[str, Optional[dict]], None],
                 driver_hotkey_probe: Callable[[List[str]], bool]):
        self._cfg = cfg
        self._emit = emit
        self._hotkey_probe = driver_hotkey_probe
        self._lock = threading.RLock()
        self._state = _STATE_READY
        self._panic_sources = set()
        self._pending: Optional[PendingConfirmation] = None
        self._expired_ids = {}  # request_id -> expiry timestamp, for error reporting
        self._last_activity = time.monotonic()
        self._idle_fired = False
        self._hotkey_was_down = False
        self._hotkey_names = _parse_hotkey_spec(cfg.emergency_hotkey)
        self._running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.on_approved: Optional[Callable[[PendingConfirmation], None]] = None
        self.on_panic_state: Optional[Callable[[bool, str], None]] = None

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> str:
        with self._lock:
            if self._panic_active_locked():
                return _STATE_STOPPED
            if self._pending is not None and self._state == _STATE_READY:
                return _STATE_CONFIRMING
            return self._state

    def _panic_active_locked(self) -> bool:
        return bool(self._panic_sources) or self._panic_file_exists()

    def resume(self) -> None:
        """Leave stopped or standby (and any pending confirmation) and go back
        to ready. A pending confirmation is cancelled with an explicit event
        so the harness can close its prompt."""
        with self._lock:
            was = self._state
            if self._panic_active_locked():
                was = _STATE_STOPPED
            self._cancel_pending_locked("cancelled by session.resume")
            self._panic_sources.clear()
            self._clear_panic_file()
            self._state = _STATE_READY
            self._last_activity = time.monotonic()
            self._idle_fired = False
        if was != _STATE_READY:
            self._emit("session.resumed", {"from": was})

    def suspend(self) -> None:
        """Enter the stopped state without emitting panic events or touching
        the panic file. Used at session shutdown so in-flight work is blocked."""
        with self._lock:
            self._cancel_pending_locked("cancelled by session stop")
            self._state = _STATE_STOPPED

    # ------------------------------------------------------------- emergency

    def trigger_panic(self, source: str) -> None:
        with self._lock:
            was_stopped = self._panic_active_locked()
            self._panic_sources.add(source)
            self._state = _STATE_STOPPED
            if not was_stopped:
                self._cancel_pending_locked("cancelled by emergency stop")
                self._touch_panic_file()
                self._emit("safety.panic_triggered", {"source": source})
        self._notify_panic_state(True, source)

    def release_panic(self, source: str) -> None:
        """Disengage the emergency stop. Any release clears all recorded
        sources - releasing is an explicit human/harness act."""
        with self._lock:
            if not self._panic_sources and not self._panic_file_exists():
                return
            self._panic_sources.clear()
            self._clear_panic_file()
            if self._state == _STATE_STOPPED:
                self._state = _STATE_READY
                self._last_activity = time.monotonic()
                self._idle_fired = False
                self._emit("safety.panic_released", {"source": source})
        self._notify_panic_state(False, source)

    def _notify_panic_state(self, active: bool, source: str) -> None:
        callback = self.on_panic_state
        if callback is not None:
            try:
                callback(active, source)
            except Exception:
                pass

    def _touch_panic_file(self) -> None:
        if not self._cfg.panic_file:
            return
        try:
            with open(self._cfg.panic_file, "w", encoding="utf-8") as fh:
                fh.write("panic\n")
        except OSError:
            pass  # best effort; the in-memory flag still protects

    def _clear_panic_file(self) -> None:
        if not self._cfg.panic_file:
            return
        try:
            os.remove(self._cfg.panic_file)
        except OSError:
            pass

    def _panic_file_exists(self) -> bool:
        return bool(self._cfg.panic_file) and os.path.exists(self._cfg.panic_file)

    def poll_hotkey(self) -> None:
        """Sample the hotkey probe. Called by the polling thread (and tests).

        The combo must be fully released between two activations, so an
        operator can toggle the emergency stop by pressing the hotkey twice.
        From standby, the hotkey resumes the session.
        """
        if not self._hotkey_names:
            return
        try:
            down = bool(self._hotkey_probe(self._hotkey_names))
        except Exception:
            return
        with self._lock:
            if down and not self._hotkey_was_down:
                if self._panic_active_locked() or self._state == _STATE_STOPPED:
                    self.release_panic_locked("hotkey")
                elif self._state == _STATE_STANDBY:
                    self._state = _STATE_READY
                    self._last_activity = time.monotonic()
                    self._idle_fired = False
                    self._emit("session.resumed", {"from": _STATE_STANDBY})
                else:
                    self.trigger_panic_locked("hotkey")
            self._hotkey_was_down = down

    def trigger_panic_locked(self, source: str) -> None:
        self.trigger_panic(source)

    def release_panic_locked(self, source: str) -> None:
        self.release_panic(source)

    # --------------------------------------------------------- idle watchdog

    def note_activity(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            self._idle_fired = False

    def start_watchdog(self) -> None:
        """Start the idle watchdog + hotkey polling thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
        thread = threading.Thread(target=self._watchdog_loop, name="safety-watchdog", daemon=True)
        self._watchdog_thread = thread
        thread.start()

    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(0.1)
            if self._hotkey_names:
                try:
                    self.poll_hotkey()
                except Exception:
                    pass
            with self._lock:
                idle_for = time.monotonic() - self._last_activity
                idle_enabled = self._cfg.idle_timeout_s > 0
                if (self._state == _STATE_READY and self._pending is None
                        and idle_enabled and idle_for > self._cfg.idle_timeout_s):
                    if not self._idle_fired:
                        self._idle_fired = True
                        self._emit("session.idle", {"after_s": self._cfg.idle_timeout_s})
                        if self._cfg.idle_action == "standby":
                            self._state = _STATE_STANDBY
                self._expire_pending_locked()

    def close(self) -> None:
        with self._lock:
            self._running = False
        self._stop_event.set()
        thread = self._watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    # ------------------------------------------------------------ evaluation

    def risk_for(self, tool: str, arguments: dict) -> str:
        return risk_for(tool, arguments)

    def evaluate(self, tool: str, arguments: dict) -> GateVerdict:
        """Run an action through the gate. Never raises for policy outcomes."""
        with self._lock:
            self._purge_expired_locked()
            if self._state == _STATE_STOPPED or self._panic_active_locked():
                return GateVerdict("stopped", "emergency stop is active", risk=self.risk_for(tool, arguments))
            if self._state == _STATE_STANDBY:
                return GateVerdict("standby", "session is in idle standby; call session.resume", risk=self.risk_for(tool, arguments))
            if self._pending is not None:
                return GateVerdict("busy", "a confirmation is pending; resolve it first", risk=self.risk_for(tool, arguments))

            risk = self.risk_for(tool, arguments)
            rule_verdict = self._apply_rules_locked(tool, arguments)
            if rule_verdict == "deny":
                return GateVerdict("deny", "blocked by a deny rule", risk=risk, data={"rule": self._last_rule})
            if rule_verdict == "not_allowed":
                return GateVerdict("deny", "action not allowed (allowlist mode, default rule is deny)", risk=risk)

            threshold = _RISK_ORDER.get(self._cfg.confirm_threshold, 2)
            if _RISK_ORDER.get(risk, 0) >= threshold:
                pending = self._create_pending_locked(tool, arguments, risk)
                return GateVerdict("confirm", "high-risk action requires confirmation", risk=risk,
                                   data={"request_id": pending.request_id, "reason": pending.reason})
            return GateVerdict("allow", risk=risk)

    def evaluate_allow_only(self, tool: str, arguments: dict) -> GateVerdict:
        """Gate an action for execution inside an already-approved context
        (batch payload). Confirmation is implicitly granted; deny rules,
        operational state and the panic file still apply."""
        with self._lock:
            self._purge_expired_locked()
            if self._state == _STATE_STOPPED or self._panic_active_locked():
                return GateVerdict("stopped", "emergency stop is active", risk=self.risk_for(tool, arguments))
            if self._state == _STATE_STANDBY:
                return GateVerdict("standby", "session is in idle standby; call session.resume", risk=self.risk_for(tool, arguments))
            rule_verdict = self._apply_rules_locked(tool, arguments)
            if rule_verdict == "deny":
                return GateVerdict("deny", "blocked by a deny rule", risk=self.risk_for(tool, arguments),
                                   data={"rule": self._last_rule})
            if rule_verdict == "not_allowed":
                return GateVerdict("deny", "action not allowed (allowlist mode, default rule is deny)",
                                   risk=self.risk_for(tool, arguments))
            return GateVerdict("allow", risk=self.risk_for(tool, arguments))

    def _apply_rules_locked(self, tool: str, arguments: dict):
        """Returns 'allow' | 'deny' | 'not_allowed'.

        All rules are scanned: the first matching allow and the first matching
        deny are remembered, and an explicit deny always wins regardless of
        rule order. An 'allow' effect never exempts from the confirmation
        threshold (that is a separate safety layer).
        """
        first_allow = None
        first_deny = None
        for rule in self._cfg.rules:
            if not _glob_match(rule.tool_pattern, tool):
                continue
            if rule.argument_name is not None and not _matches_argument(rule, arguments.get(rule.argument_name)):
                continue
            if rule.effect == "deny" and first_deny is None:
                first_deny = rule
            elif rule.effect == "allow" and first_allow is None:
                first_allow = rule
            if first_deny is not None:
                break
        if first_deny is not None:
            self._last_rule = first_deny.as_dict()
            return "deny"
        if first_allow is not None:
            self._last_rule = None
            return "allow"
        self._last_rule = None
        return "allow" if self._cfg.default_rule == "allow" else "not_allowed"

    # ------------------------------------------------------- confirmation

    def _create_pending_locked(self, tool: str, arguments: dict, risk: str) -> PendingConfirmation:
        request_id = "cfm-%s" % uuid.uuid4().hex[:12]
        pending = PendingConfirmation(
            request_id=request_id,
            tool=tool,
            arguments=arguments,
            risk=risk,
            reason="risk %s >= confirm threshold %s" % (risk, self._cfg.confirm_threshold),
            created_at=time.monotonic(),
            expires_at=time.monotonic() + self._cfg.confirm_timeout_s,
        )
        self._pending = pending
        self._emit("safety.confirmation_requested", {
            "request_id": request_id,
            "tool": tool,
            "arguments": arguments,
            "risk": risk,
            "reason": pending.reason,
            "expires_at": pending.expires_at,
            "timeout_s": self._cfg.confirm_timeout_s,
        })
        return pending

    def _purge_expired_locked(self) -> None:
        pending = self._pending
        if pending is None or pending.status != "pending":
            return
        if time.monotonic() >= pending.expires_at:
            pending.status = "expired"
            pending.resolved_at = time.monotonic()
            self._expired_ids[pending.request_id] = pending.resolved_at
            # prune stale records (they are only for error messages)
            cutoff = time.monotonic() - 300
            for rid in [rid for rid, at in self._expired_ids.items() if at < cutoff]:
                del self._expired_ids[rid]
            self._pending = None
            self._emit("safety.confirmation_expired", {"request_id": pending.request_id})

    def _expire_pending_locked(self) -> None:
        self._purge_expired_locked()

    def _cancel_pending_locked(self, reason: str) -> None:
        pending = self._pending
        if pending is None:
            return
        pending.status = "cancelled"
        pending.resolved_at = time.monotonic()
        self._pending = None
        self._emit("safety.confirmation_resolved", {
            "request_id": pending.request_id,
            "approve": False,
            "reason": reason,
            "tool": pending.tool,
        })

    def resolve(self, request_id: str, approve: bool) -> PendingConfirmation:
        """Resolve a pending confirmation. Raises PolicyError for unknown or
        expired requests."""
        with self._lock:
            self._purge_expired_locked()
            if request_id in self._expired_ids:
                raise PolicyError("confirmation_expired",
                                  "confirmation %s expired without a decision" % request_id)
            pending = self._pending
            if pending is None or pending.request_id != request_id:
                raise PolicyError("confirmation_not_found",
                                  "no pending confirmation with id %r" % request_id)
            if pending.status != "pending":
                raise PolicyError("confirmation_not_found",
                                  "confirmation %s is already %s" % (request_id, pending.status))
            pending.status = "approved" if approve else "denied"
            pending.resolved_at = time.monotonic()
            self._pending = None
            self._last_activity = time.monotonic()
            self._idle_fired = False
            self._emit("safety.confirmation_resolved", {
                "request_id": request_id,
                "approve": approve,
                "tool": pending.tool,
            })
            if approve and self.on_approved is not None:
                self.on_approved(pending)
            return pending

    def pending_confirmation(self) -> Optional[PendingConfirmation]:
        with self._lock:
            return self._pending

    # ------------------------------------------------------------- lifecycle

    def reconfigure(self, cfg: SafetyConfig) -> None:
        with self._lock:
            self._cfg = cfg
            self._hotkey_names = _parse_hotkey_spec(cfg.emergency_hotkey)


def _parse_hotkey_spec(spec: str) -> List[str]:
    if not spec:
        return []
    from computer_control.keys import parse_hotkey

    return parse_hotkey(spec)
