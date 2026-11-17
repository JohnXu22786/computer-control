"""The safety gate: the single choke point every action passes through.

It enforces, in order:
  1. operational state  - stopped (emergency stop) / standby (idle) block everything
  2. authorization profile - read_only / limited / full / revoked shrink which
                          tools may run; orthogonal to the state above - a live
                          session may narrow it in-band but never widen it
  3. allow/deny rules   - an explicit deny always wins over allows; allowlist
                          mode when the default rule is deny
  4. risk confirmation  - actions at or above the configured risk threshold
                          wait for a human approval before they may run

A dedicated admission lock makes the final authorization check and the action
dispatch atomic against grant changes: ``execute_admitted`` holds it across
check + execution, and ``set_grant``/``reconfigure`` hold it across the grant
mutation, so a downgrade can never land between the two. It deliberately is
not the gate lock itself - the gate lock stays free for the emergency stop,
the hotkey poll and the idle watchdog while a (possibly long) action runs.

It also owns the emergency-stop machinery (hotkey polling, panic file,
protocol-triggered panic) and the idle watchdog that puts the session into
standby after inactivity.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from computer_control.actions import RISK_LEVELS, ValidationError, clean_arguments, risk_for
from computer_control.config import AUTH_PROFILES, ConfigError, SafetyConfig

_STATE_READY = "ready"
_STATE_CONFIRMING = "confirming"
_STATE_STOPPED = "stopped"
_STATE_STANDBY = "standby"

_RISK_ORDER = {"benign": 0, "moderate": 1, "high": 2}

_GRANT_READ_ONLY = "read_only"
_GRANT_LIMITED = "limited"
_GRANT_REVOKED = "revoked"

_NOT_AUTHORIZED = "not_authorized"


def _validate_grant_profile(profile: str) -> None:
    if not isinstance(profile, str) or profile not in AUTH_PROFILES:
        raise ConfigError("safety.auth_profile must be one of %s, got %r"
                          % ("/".join(AUTH_PROFILES), profile))


# Observation-only surface for the read_only profile. 'system.status' is a
# protocol method rather than a gated tool; it stays listed so the profile
# reads as the intended capability set. a11y.snapshot is listed as a floor
# capability but additionally requires the host's explicit opt-in (see
# _check_grant_locked).
_READ_ONLY_TOOLS = frozenset({"screen.capture", "system.status", "a11y.snapshot"})

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

    decision: str  # allow | deny | confirm | stopped | standby | busy | not_authorized
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
    timeout_s: float
    payload: dict = field(default_factory=dict)
    status: str = "pending"  # pending | approved | denied | expired | cancelled
    resolved_at: Optional[float] = None
    request_event_state: str = "ready"  # ready | deferred | publishing | sent | discarded
    deferred_events: list = field(default_factory=list)
    deferred_callbacks: list = field(default_factory=list)


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
    """'contains' matching that also understands modifier aliases and is
    case-insensitive, so a rule written for 'win' catches lwin/rwin/super/meta
    and uppercase/lowercase variations."""
    v_str = str(value)
    n_str = str(needle)
    if n_str.lower() in v_str.lower():
        return True
    family = _modifier_family(n_str)
    if not family:
        return False
    return bool(family & _modifier_family(v_str))


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
        _validate_grant_profile(cfg.auth_profile)
        self._cfg = cfg
        self._emit = emit
        self._hotkey_probe = driver_hotkey_probe
        self._lock = threading.RLock()
        # Serializes grant mutations against execute_admitted's check+dispatch
        # pair. Lock order everywhere: admission -> gate lock.
        self._admission_lock = threading.RLock()
        self._admission_context = threading.local()
        self._gate_context = threading.local()
        self._state = _STATE_READY
        self._grant = cfg.auth_profile
        self._last_rule = None
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

    @contextmanager
    def admission_guard(self):
        """Hold admission across a compound decision; publish callbacks after release."""
        callbacks = getattr(self._admission_context, "callbacks", None)
        outermost = callbacks is None
        if outermost:
            callbacks = []
            self._admission_context.events = []
            self._admission_context.callbacks = callbacks
        try:
            with self._admission_lock:
                yield
        finally:
            if outermost:
                events = self._admission_context.events
                del self._admission_context.events
                del self._admission_context.callbacks
                for event_type, payload in events:
                    self._emit(event_type, payload)
                for callback, pending in callbacks:
                    callback(pending)

    @contextmanager
    def _gate_guard(self):
        """Hold the gate lock and publish callbacks after its outermost release."""
        events = getattr(self._gate_context, "events", None)
        outermost = events is None
        if outermost:
            self._gate_context.events = []
            self._gate_context.callbacks = []
        try:
            with self._lock:
                yield
        finally:
            if outermost:
                events = self._gate_context.events
                callbacks = self._gate_context.callbacks
                del self._gate_context.events
                del self._gate_context.callbacks
                for event_type, payload in events:
                    self._emit(event_type, payload)
                for callback, args in callbacks:
                    callback(*args)

    def _publish_event(self, event_type: str, payload: Optional[dict]) -> None:
        events = getattr(self._admission_context, "events", None)
        if events is not None:
            events.append((event_type, payload))
            return
        events = getattr(self._gate_context, "events", None)
        if events is None:
            self._emit(event_type, payload)
        else:
            events.append((event_type, payload))

    def _defer_callback(self, callback, *args) -> None:
        callbacks = getattr(self._admission_context, "callbacks", None)
        if callbacks is not None:
            callbacks.append((callback, args))
            return
        callbacks = getattr(self._gate_context, "callbacks", None)
        if callbacks is None:
            callback(*args)
        else:
            callbacks.append((callback, args))

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
        with self._gate_guard():
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
        with self._gate_guard():
            self._cancel_pending_locked("cancelled by session stop")
            self._state = _STATE_STOPPED

    # ------------------------------------------------------------- emergency

    def trigger_panic(self, source: str) -> None:
        with self._gate_guard():
            was_stopped = self._panic_active_locked()
            self._panic_sources.add(source)
            self._state = _STATE_STOPPED
            if not was_stopped:
                self._cancel_pending_locked("cancelled by emergency stop")
                self._touch_panic_file()
                self._publish_event("safety.panic_triggered", {"source": source})
            self._defer_callback(self._notify_panic_state, True, source)

    def release_panic(self, source: str) -> None:
        """Disengage the emergency stop. Any release clears all recorded
        sources - releasing is an explicit human/harness act."""
        with self._gate_guard():
            if not self._panic_sources and not self._panic_file_exists():
                return
            self._panic_sources.clear()
            self._clear_panic_file()
            if self._state == _STATE_STOPPED:
                self._state = _STATE_READY
                self._last_activity = time.monotonic()
                self._idle_fired = False
                self._publish_event("safety.panic_released", {"source": source})
            self._defer_callback(self._notify_panic_state, False, source)

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
        with self._gate_guard():
            if down and not self._hotkey_was_down:
                if self._panic_active_locked() or self._state == _STATE_STOPPED:
                    self.release_panic_locked("hotkey")
                elif self._state == _STATE_STANDBY:
                    self._state = _STATE_READY
                    self._last_activity = time.monotonic()
                    self._idle_fired = False
                    self._publish_event("session.resumed", {"from": _STATE_STANDBY})
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
            idle_event = None
            with self._gate_guard():
                idle_for = time.monotonic() - self._last_activity
                idle_enabled = self._cfg.idle_timeout_s > 0
                if (self._state == _STATE_READY and self._pending is None
                        and idle_enabled and idle_for > self._cfg.idle_timeout_s):
                    if not self._idle_fired:
                        self._idle_fired = True
                        idle_event = ("session.idle", {"after_s": self._cfg.idle_timeout_s})
                        if self._cfg.idle_action == "standby":
                            self._state = _STATE_STANDBY
                self._expire_pending_locked()
            if idle_event is not None:
                self._emit(*idle_event)

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

    def _grant_deny_reason(self) -> str:
        if self._grant == _GRANT_REVOKED:
            return ("authorization profile 'revoked' is active; screen capture and status still "
                    "run, but only the host can grant a wider profile or opt a11y.snapshot back in")
        return ("action not permitted by authorization profile '%s'; requires the user to grant a wider profile"
                % self._grant)

    def evaluate(self, tool: str, arguments: dict,
                 defer_confirmation_event: bool = False,
                 defer_confirmation_publication: bool = False) -> GateVerdict:
        """Run an action through the gate, optionally deferring its request event."""
        with self._gate_guard():
            self._purge_expired_locked()
            if self._state == _STATE_STOPPED or self._panic_active_locked():
                return GateVerdict("stopped", "emergency stop is active", risk=self.risk_for(tool, arguments))
            if self._state == _STATE_STANDBY:
                return GateVerdict("standby", "session is in idle standby; call session.resume", risk=self.risk_for(tool, arguments))
            if self._pending is not None:
                return GateVerdict("busy", "a confirmation is pending; resolve it first", risk=self.risk_for(tool, arguments))

            risk = self.risk_for(tool, arguments)
            rule_verdict = self._apply_rules_locked(tool, arguments)
            if rule_verdict == _NOT_AUTHORIZED:
                return GateVerdict(_NOT_AUTHORIZED, self._grant_deny_reason(), risk=risk,
                                   data={"auth_profile": self._grant})
            if rule_verdict == "deny":
                return GateVerdict("deny", "blocked by a deny rule", risk=risk, data={"rule": self._last_rule})
            if rule_verdict == "not_allowed":
                return GateVerdict("deny", "action not allowed (allowlist mode, default rule is deny)", risk=risk)

            threshold = _RISK_ORDER.get(self._cfg.confirm_threshold, 2)
            if _RISK_ORDER.get(risk, 0) >= threshold:
                pending = self._create_pending_locked(
                    tool, arguments, risk, emit_event=not defer_confirmation_event,
                    request_event_state=("deferred" if defer_confirmation_publication
                                         else "ready"))
                return GateVerdict("confirm", "high-risk action requires confirmation", risk=risk,
                                   data={"request_id": pending.request_id, "reason": pending.reason})
            return GateVerdict("allow", risk=risk)

    def evaluate_allow_only(self, tool: str, arguments: dict) -> GateVerdict:
        """Gate an action for execution inside an already-approved context
        (batch payload). Confirmation is implicitly granted; deny rules,
        authorization profiles, operational state and the panic file still
        apply."""
        with self._gate_guard():
            self._purge_expired_locked()
            if self._state == _STATE_STOPPED or self._panic_active_locked():
                return GateVerdict("stopped", "emergency stop is active", risk=self.risk_for(tool, arguments))
            if self._state == _STATE_STANDBY:
                return GateVerdict("standby", "session is in idle standby; call session.resume", risk=self.risk_for(tool, arguments))
            rule_verdict = self._apply_rules_locked(tool, arguments)
            if rule_verdict == _NOT_AUTHORIZED:
                return GateVerdict(_NOT_AUTHORIZED, self._grant_deny_reason(), risk=self.risk_for(tool, arguments),
                                   data={"auth_profile": self._grant})
            if rule_verdict == "deny":
                return GateVerdict("deny", "blocked by a deny rule", risk=self.risk_for(tool, arguments),
                                   data={"rule": self._last_rule})
            if rule_verdict == "not_allowed":
                return GateVerdict("deny", "action not allowed (allowlist mode, default rule is deny)",
                                   risk=self.risk_for(tool, arguments))
            return GateVerdict("allow", risk=self.risk_for(tool, arguments))

    def execute_admitted(self, tool: str, arguments: dict, runner):
        """Final authorization check and dispatch, atomic against grant changes.

        The check and ``runner`` run under the same admission lock that
        ``set_grant``/``reconfigure`` acquire, so a ``session.configure`` or
        ``grant_profile`` cannot land a grant change between the check and the
        dispatch. Returns ``(verdict, result)``; ``result`` is None when the
        verdict is not ``allow``.
        """
        with self.admission_guard():
            verdict = self.evaluate_allow_only(tool, arguments)
            if verdict.decision != "allow":
                return verdict, None
            return verdict, runner()

    def admit_only(self, tool: str, arguments: dict) -> GateVerdict:
        """Final authorization check under the admission lock, without holding
        the lock across a dispatch.

        Used for batch.execute: its dispatch is a loop of sub-actions that
        each re-enter execute_admitted, so holding the admission lock across
        the whole loop would starve set_grant/reconfigure until the batch
        ended - every remaining item would escape a downgrade. With the check
        scoped to the batch itself, the lock is free between items and the
        per-item final check refuses anything the downgrade now blocks.
        """
        with self.admission_guard():
            return self.evaluate_allow_only(tool, arguments)

    def _check_grant_locked(self, tool: str) -> Optional[str]:
        """Authorization profile check - the orthogonal axis to the operational
        state. Returns _NOT_AUTHORIZED or None, and never touches _state.

        'revoked' and 'read_only' share the observation floor: withdrawing
        authority must not blind the human deciding whether to hand it back.
        a11y.snapshot sits on that floor only with the host's explicit
        opt-in (safety.a11y_snapshot_opt_in, host-only settable) - the tree
        exposes whatever text happens to be on screen, so a narrowed/withdrawn
        profile defaults to refusing it. 'limited' blocks keyboard input and
        its a11y equivalent (a11y.input types into focused nodes, i.e. it
        injects text); everything else on the floor still runs. revoked bites
        harder on the way in (a pending confirmation is cancelled, see
        set_grant) and is the only rung an agent cannot climb back out of
        in-band.
        """
        if self._grant in (_GRANT_REVOKED, _GRANT_READ_ONLY):
            if tool == "a11y.snapshot":
                if not self._cfg.a11y_snapshot_opt_in:
                    return _NOT_AUTHORIZED
            elif tool not in _READ_ONLY_TOOLS:
                return _NOT_AUTHORIZED
        if self._grant == _GRANT_LIMITED and (tool.startswith("keyboard.") or tool == "a11y.input"):
            return _NOT_AUTHORIZED
        return None

    def _apply_rules_locked(self, tool: str, arguments: dict):
        """Returns 'allow' | 'deny' | 'not_allowed' | 'not_authorized'.

        Every action - plain and batch-internal alike - arrives through here,
        so the authorization check is placed ahead of the rule scan.

        All rules are scanned: the first matching allow and the first matching
        deny are remembered, and an explicit deny always wins regardless of
        rule order. An 'allow' effect never exempts from the confirmation
        threshold (that is a separate safety layer).
        """
        granted = self._check_grant_locked(tool)
        if granted is not None:
            return granted
        decision, rule = self._configured_rules_locked(tool, arguments)
        self._last_rule = rule.as_dict() if rule is not None else None
        return decision

    def _configured_rules_locked(self, tool: str, arguments: dict):
        """Return the configured rule decision without applying the grant."""
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
            return "deny", first_deny
        if first_allow is not None:
            return "allow", None
        return ("allow" if self._cfg.default_rule == "allow" else "not_allowed"), None

    # ------------------------------------------------------- confirmation

    def _confirmation_requested_payload(self, pending: PendingConfirmation) -> dict:
        return {
            "request_id": pending.request_id,
            "tool": pending.tool,
            "arguments": copy.deepcopy(pending.arguments),
            "risk": pending.risk,
            "reason": pending.reason,
            "expires_at": pending.expires_at,
            "timeout_s": pending.timeout_s,
        }

    def _create_pending_locked(self, tool: str, arguments: dict, risk: str,
                               emit_event: bool = True,
                               request_event_state: str = "ready") -> PendingConfirmation:
        request_id = "cfm-%s" % uuid.uuid4().hex[:12]
        arguments_snapshot = copy.deepcopy(arguments)
        timeout_s = self._cfg.confirm_timeout_s
        created_at = time.monotonic()
        pending = PendingConfirmation(
            request_id=request_id,
            tool=tool,
            arguments=arguments_snapshot,
            risk=risk,
            reason="risk %s >= confirm threshold %s" % (risk, self._cfg.confirm_threshold),
            created_at=created_at,
            expires_at=created_at + timeout_s,
            timeout_s=timeout_s,
            request_event_state=request_event_state,
        )
        self._pending = pending
        if emit_event:
            self._emit_confirmation_event_locked(
                pending, "safety.confirmation_requested",
                self._confirmation_requested_payload(pending))
            pending.request_event_state = "sent"
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
            self._emit_confirmation_event_locked(
                pending, "safety.confirmation_expired",
                {"request_id": pending.request_id})

    def _expire_pending_locked(self) -> None:
        self._purge_expired_locked()

    def _cancel_pending_locked(self, reason: str) -> None:
        pending = self._pending
        if pending is None:
            return
        pending.status = "cancelled"
        pending.resolved_at = time.monotonic()
        self._pending = None
        self._emit_confirmation_event_locked(pending, "safety.confirmation_resolved", {
            "request_id": pending.request_id,
            "approve": False,
            "reason": reason,
            "tool": pending.tool,
        })

    def _emit_confirmation_event_locked(self, pending: PendingConfirmation,
                                        event_type: str, payload: dict) -> None:
        if pending.request_event_state in ("deferred", "publishing"):
            pending.deferred_events.append((event_type, payload))
            return
        self._publish_event(event_type, payload)

    def resolve(self, request_id: str, approve: bool) -> PendingConfirmation:
        """Resolve a pending confirmation. Raises PolicyError for unknown or
        expired requests."""
        with self.admission_guard():
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
                self._emit_confirmation_event_locked(pending, "safety.confirmation_resolved", {
                    "request_id": request_id,
                    "approve": approve,
                    "tool": pending.tool,
                })
                callback = self.on_approved if approve else None
                callback_after_publication = (
                    callback is not None
                    and pending.request_event_state in ("deferred", "publishing")
                )
                if callback_after_publication:
                    pending.deferred_callbacks.append(callback)
        if callback is not None and not callback_after_publication:
            deferred = getattr(self._admission_context, "callbacks", None)
            if deferred is None:
                # Keep execution outside both locks to avoid inversion with
                # the action path. Resolution and response construction
                # serialize under admission -> gate.
                callback(pending)
            else:
                deferred.append((callback, pending))
        return pending

    def pending_confirmation(self) -> Optional[PendingConfirmation]:
        with self._lock:
            return self._pending

    def emit_confirmation_requested(self, request_id: str,
                                    deferred_pending: Optional[PendingConfirmation] = None) -> bool:
        """Publish a deferred request after its response has been constructed.

        The publication state is claimed under admission and gate locks, while
        the synchronous emitter runs outside both. Resolution events that race
        publication are emitted afterward in request order.
        """
        with self.admission_guard():
            with self._lock:
                self._purge_expired_locked()
                current = self._pending
                pending = (current if current is not None
                           and current.request_id == request_id
                           and current.status == "pending" else deferred_pending)
                if (pending is None or pending.request_id != request_id
                        or pending.request_event_state
                        in ("publishing", "sent", "discarded")):
                    return False
                if (pending is not current
                        and pending.request_event_state != "deferred"):
                    return False
                payload = self._confirmation_requested_payload(pending)
                pending.request_event_state = "publishing"

        try:
            self._emit("safety.confirmation_requested", payload)
        finally:
            self._finish_confirmation_request_publication(pending)
        return True

    def _finish_confirmation_request_publication(self,
                                                pending: PendingConfirmation) -> None:
        with self.admission_guard():
            with self._lock:
                pending.request_event_state = "sent"
                deferred_events = pending.deferred_events
                deferred_callbacks = pending.deferred_callbacks
                pending.deferred_events = []
                pending.deferred_callbacks = []
        for event_type, event_payload in deferred_events:
            self._emit(event_type, event_payload)
        for callback in deferred_callbacks:
            callback(pending)

    def discard_confirmation_request(self, pending: Optional[PendingConfirmation]) -> None:
        """Finish a deferred request that became invalid before its response existed."""
        if pending is None:
            return
        with self.admission_guard():
            with self._lock:
                if pending.request_event_state != "deferred":
                    return
                pending.request_event_state = "discarded"
                deferred_events = pending.deferred_events
                deferred_callbacks = pending.deferred_callbacks
                pending.deferred_events = []
                pending.deferred_callbacks = []
        for event_type, event_payload in deferred_events:
            self._emit(event_type, event_payload)
        for callback in deferred_callbacks:
            callback(pending)

    def build_confirmation_response(self, request_id: str,
                                    build_response: Callable[[], dict]) -> Optional[dict]:
        """Build the response while this pending request is protected from resolution."""
        with self.admission_guard():
            with self._lock:
                self._purge_expired_locked()
                pending = self._pending
                if (pending is None or pending.status != "pending"
                        or pending.request_id != request_id):
                    return None
                return build_response()

    # ------------------------------------------------------------- lifecycle

    def set_grant(self, profile: str) -> None:
        """Change the authorization profile on a live gate.

        A downgrade cancels a pending confirmation the new profile no longer
        authorizes: authority was withdrawn, so an approval collected earlier
        must not run. For a pending ``batch.execute`` this looks through the
        stashed payload - if any sub-action is no longer authorized, the whole
        batch confirmation is cancelled. Entering 'revoked' cancels the pending
        confirmation unconditionally - it bites harder on the way in. The
        operational state is left alone - revoked is a capability rung, not a
        state, so a revoked session still reports 'ready'.

        The grant mutation holds the admission lock, so it cannot land between
        an action's final check and its dispatch.
        """
        _validate_grant_profile(profile)
        with self.admission_guard():
            with self._lock:
                self._set_grant_locked(profile)

    def _set_grant_locked(self, profile: str) -> None:
        entering_revoked = profile == _GRANT_REVOKED and self._grant != _GRANT_REVOKED
        self._grant = profile
        if entering_revoked:
            self._cancel_pending_locked("cancelled by authorization revoke")
            return
        pending = self._pending
        if (pending is not None and pending.status == "pending"
                and self._pending_blocked_by_grant_locked(pending)):
            self._cancel_pending_locked("cancelled by authorization downgrade")

    def _pending_blocked_by_grant_locked(self, pending: PendingConfirmation) -> bool:
        if self._check_grant_locked(pending.tool) is not None:
            return True
        if pending.tool != "batch.execute":
            return False
        # batch.execute itself is never refused by 'limited', so a mixed batch
        # could smuggle keyboard items past a downgrade unless the sub-actions
        # are checked too.
        for tool, _ in _batch_subtool_names(pending, self._configured_rules_locked):
            if self._check_grant_locked(tool) is not None:
                return True
        return False

    def reconfigure(self, cfg: SafetyConfig) -> None:
        with self.admission_guard():
            with self._lock:
                _validate_grant_profile(cfg.auth_profile)
                self._cfg = cfg
                self._hotkey_names = _parse_hotkey_spec(cfg.emergency_hotkey)
                self._set_grant_locked(cfg.auth_profile)


def _batch_subtool_names(pending: PendingConfirmation, rule_decision) -> List[tuple]:
    """Sub-action tool names of a pending batch confirmation: the stashed plan
    when available (only 'run' entries execute), otherwise the raw items, so a
    downgrade landing before the engine stashed the payload still sees the
    sub-tools. Nested batches are inspected recursively."""
    names = []

    def collect(items, planned=False):
        for item in items:
            if planned:
                if not isinstance(item, (list, tuple)) or len(item) < 3 or item[0] != "run":
                    continue
                tool, arguments = item[1], item[2]
            else:
                if not isinstance(item, dict) or not item.get("tool"):
                    continue
                tool, arguments = item["tool"], item.get("arguments") or {}
            if not isinstance(tool, str) or not tool:
                continue
            if not planned:
                try:
                    arguments = clean_arguments(tool, arguments)
                except ValidationError:
                    continue
            if rule_decision(tool, arguments)[0] != "allow":
                continue
            names.append((tool, arguments))
            if tool == "batch.execute" and isinstance(arguments, dict):
                collect(arguments.get("items") or [])

    payload = pending.payload
    if payload:
        collect(payload.get("items") or [], planned=True)
    else:
        collect((pending.arguments or {}).get("items") or [])
    return names


def _parse_hotkey_spec(spec: str) -> List[str]:
    if not spec:
        return []
    from computer_control.keys import parse_hotkey

    return parse_hotkey(spec)
