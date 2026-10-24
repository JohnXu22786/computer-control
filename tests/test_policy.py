"""Unit tests for the safety gate: rules, risk classification, confirmation flow,
emergency stop, panic file and idle watchdog."""

import os
import tempfile
import threading
import time
import unittest

from computer_control.config import from_dict
from computer_control.policy import PolicyError, SafetyGate


class EmitRecorder:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event_type, payload=None):
        with self.lock:
            self.events.append((event_type, payload))

    def types(self):
        with self.lock:
            return [t for t, _ in self.events]


def make_gate(overrides=None, emit=None):
    cfg = from_dict(overrides or {})
    return SafetyGate(cfg.safety, emit=emit or EmitRecorder(), driver_hotkey_probe=lambda keys: False)


def wait_for(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestRiskClassification(unittest.TestCase):
    def test_benign_actions(self):
        gate = make_gate()
        for tool in ("screen.capture", "wait.pause", "a11y.snapshot"):
            self.assertEqual(gate.risk_for(tool, {}), "benign", tool)

    def test_moderate_actions(self):
        gate = make_gate()
        for tool in ("pointer.click", "pointer.move", "pointer.drag", "pointer.scroll",
                     "keyboard.press", "keyboard.type", "a11y.activate", "a11y.input"):
            self.assertEqual(gate.risk_for(tool, {}), "moderate", tool)

    def test_combo_risk_escalation(self):
        gate = make_gate()
        self.assertEqual(gate.risk_for("keyboard.combo", {"keys": ["win", "r"]}), "high")
        self.assertEqual(gate.risk_for("keyboard.combo", {"keys": ["ctrl", "alt", "del"]}), "high")
        self.assertEqual(gate.risk_for("keyboard.combo", {"keys": ["ctrl", "c"]}), "moderate")
        self.assertEqual(gate.risk_for("keyboard.combo", {"keys": ["shift", "esc"]}), "moderate")
        self.assertEqual(gate.risk_for("keyboard.combo", {"keys": "win+r"}), "high")  # string form


class TestRules(unittest.TestCase):
    def test_deny_rule_blocks(self):
        gate = make_gate({"safety": {"rules": [{"match": {"tool": "pointer.*"}, "effect": "deny"}]}})
        v = gate.evaluate("pointer.click", {"button": "left"})
        self.assertEqual(v.decision, "deny")
        self.assertIn("rule", v.reason)
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")

    def test_argument_contains_matcher(self):
        rules = [{"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"}]
        gate = make_gate({"safety": {"rules": rules}})
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["win", "l"]}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["ctrl", "c"]}).decision, "allow")

    def test_argument_contains_matcher_case_insensitive(self):
        rules = [
            {"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "Win"}}, "effect": "deny"},
            {"match": {"tool": "keyboard.press", "argument": {"name": "key", "matcher": "contains", "value": "Enter"}}, "effect": "deny"},
        ]
        gate = make_gate({"safety": {"rules": rules}})
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["win", "l"]}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.combo", {"keys": ["WIN", "L"]}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.press", {"key": "enter"}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.press", {"key": "tab"}).decision, "allow")

    def test_argument_glob_matcher_on_string(self):
        rules = [{"match": {"tool": "keyboard.type", "argument": {"name": "text", "matcher": "glob", "value": "rm -rf*"}}, "effect": "deny"}]
        gate = make_gate({"safety": {"rules": rules}})
        self.assertEqual(gate.evaluate("keyboard.type", {"text": "rm -rf /"}).decision, "deny")
        self.assertEqual(gate.evaluate("keyboard.type", {"text": "echo hi"}).decision, "allow")

    def test_equals_matcher(self):
        rules = [{"match": {"tool": "pointer.scroll", "argument": {"name": "axis", "matcher": "equals", "value": "horizontal"}}, "effect": "deny"}]
        gate = make_gate({"safety": {"rules": rules}})
        self.assertEqual(gate.evaluate("pointer.scroll", {"axis": "horizontal", "amount": 3}).decision, "deny")
        self.assertEqual(gate.evaluate("pointer.scroll", {"axis": "vertical", "amount": 3}).decision, "allow")

    def test_allowlist_mode(self):
        cfg = {
            "safety": {
                "default_rule": "deny",
                "rules": [{"match": {"tool": "screen.capture"}, "effect": "allow"}],
            }
        }
        gate = make_gate(cfg)
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
        v = gate.evaluate("pointer.move", {"x": 1, "y": 1})
        self.assertEqual(v.decision, "deny")
        self.assertIn("not allowed", v.reason)

    def test_argument_missing_does_not_match(self):
        rules = [{"match": {"tool": "keyboard.type", "argument": {"name": "text", "matcher": "glob", "value": "x*"}}, "effect": "deny"}]
        gate = make_gate({"safety": {"rules": rules}})
        self.assertEqual(gate.evaluate("keyboard.type", {}).decision, "allow")


class TestConfirmationFlow(unittest.TestCase):
    def test_high_risk_requires_confirmation(self):
        emit = EmitRecorder()
        gate = make_gate({"safety": {"confirm_threshold": "high"}}, emit=emit)
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        self.assertEqual(v.decision, "confirm")
        self.assertEqual(v.risk, "high")
        self.assertIn("request_id", v.data)

    def test_moderate_actions_pass_below_threshold(self):
        gate = make_gate({"safety": {"confirm_threshold": "high"}})
        v = gate.evaluate("keyboard.type", {"text": "hello"})
        self.assertEqual(v.decision, "allow")

    def test_lower_threshold_raises_confirm_scope(self):
        gate = make_gate({"safety": {"confirm_threshold": "moderate"}})
        v = gate.evaluate("keyboard.type", {"text": "hello"})
        self.assertEqual(v.decision, "confirm")
        # while a confirmation is pending, everything else is busy
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "busy")
        pending = gate.pending_confirmation()
        gate.resolve(pending.request_id, approve=False)
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")

    def test_approve_and_execute_approval_callback(self):
        approved = []
        emit = EmitRecorder()
        gate = make_gate({"safety": {"confirm_threshold": "high"}}, emit=emit)
        gate.on_approved = lambda pending: approved.append(pending.request_id)
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        pending = gate.resolve(v.data["request_id"], approve=True)
        self.assertEqual(pending.status, "approved")
        self.assertEqual(approved, [pending.request_id])
        self.assertIn("safety.confirmation_resolved", emit.types())

    def test_deny_resolves(self):
        gate = make_gate({"safety": {"confirm_threshold": "high"}})
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        pending = gate.resolve(v.data["request_id"], approve=False)
        self.assertEqual(pending.status, "denied")

    def test_busy_while_pending(self):
        gate = make_gate({"safety": {"confirm_threshold": "high"}})
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        self.assertEqual(v.decision, "confirm")
        v2 = gate.evaluate("screen.capture", {})
        self.assertEqual(v2.decision, "busy")
        gate.resolve(v.data["request_id"], approve=False)
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")

    def test_expiry_denies(self):
        gate = make_gate({"safety": {"confirm_timeout_s": 0.05, "confirm_threshold": "high"}})
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        rid = v.data["request_id"]
        time.sleep(0.12)
        with self.assertRaises(PolicyError) as ctx:
            gate.resolve(rid, approve=True)
        self.assertEqual(ctx.exception.code, "confirmation_expired")
        # expired pending is purged: new confirmations can be created
        v2 = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        self.assertEqual(v2.decision, "confirm")

    def test_unknown_request_id(self):
        gate = make_gate()
        with self.assertRaises(PolicyError):
            gate.resolve("no-such-id", approve=True)


class TestEmergencyStop(unittest.TestCase):
    def test_trigger_and_release(self):
        emit = EmitRecorder()
        gate = make_gate(emit=emit)
        gate.trigger_panic(source="hotkey")
        self.assertEqual(gate.state, "stopped")
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "stopped")
        self.assertIn("safety.panic_triggered", emit.types())
        gate.release_panic(source="protocol")
        self.assertEqual(gate.state, "ready")
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
        self.assertIn("safety.panic_released", emit.types())

    def test_panic_file_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            panic_file = os.path.join(d, "panic.flag")
            gate = make_gate({"safety": {"panic_file": panic_file}})
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
            open(panic_file, "w").close()
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "stopped")
            os.remove(panic_file)
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")

    def test_trigger_panic_creates_panic_file(self):
        with tempfile.TemporaryDirectory() as d:
            panic_file = os.path.join(d, "panic.flag")
            gate = make_gate({"safety": {"panic_file": panic_file}})
            gate.trigger_panic(source="hotkey")
            self.assertTrue(os.path.exists(panic_file))
            gate.release_panic(source="protocol")
            self.assertFalse(os.path.exists(panic_file))

    def test_hotkey_probe_triggers(self):
        emit = EmitRecorder()
        probe_calls = []

        def probe(keys):
            probe_calls.append(list(keys))
            return True

        gate = SafetyGate(from_dict({}).safety, emit=emit, driver_hotkey_probe=probe)
        gate.poll_hotkey()  # edge: first sighting arms the trigger
        self.assertEqual(gate.state, "stopped")
        self.assertEqual(probe_calls, [["ctrl", "alt", "f12"]])

    def test_hotkey_release_toggle(self):
        gate = SafetyGate(from_dict({}).safety, emit=EmitRecorder(), driver_hotkey_probe=lambda keys: True)
        gate.poll_hotkey()
        self.assertEqual(gate.state, "stopped")
        # while stopped the probe is still true -> no change; user must release then press again
        gate.poll_hotkey()
        self.assertEqual(gate.state, "stopped")
        gate._hotkey_was_down = False
        gate.poll_hotkey()
        self.assertEqual(gate.state, "ready")


class TestAuthorizationProfiles(unittest.TestCase):
    """Authorization profiles are an axis orthogonal to the operational state:
    they narrow which tools clear the gate, and never introduce a state."""

    def test_read_only_blocks_writes(self):
        gate = make_gate({"safety": {"auth_profile": "read_only"}})
        for tool in ("screen.capture", "a11y.snapshot"):
            self.assertEqual(gate.evaluate(tool, {}).decision, "allow", tool)
        for tool, args in (("pointer.click", {}), ("keyboard.type", {"text": "hi"}),
                          ("a11y.activate", {}), ("batch.execute", {"items": []})):
            v = gate.evaluate(tool, args)
            self.assertEqual(v.decision, "not_authorized", tool)
            self.assertEqual(v.data.get("auth_profile"), "read_only", tool)
        self.assertEqual(gate.state, "ready")

    def test_limited_blocks_keyboard(self):
        gate = make_gate({"safety": {"auth_profile": "limited"}})
        for tool in ("keyboard.type", "keyboard.press", "keyboard.combo"):
            self.assertEqual(gate.evaluate(tool, {}).decision, "not_authorized", tool)
        self.assertEqual(gate.evaluate("pointer.click", {}).decision, "allow")
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
        self.assertEqual(gate.state, "ready")

    def test_full_profile_is_unrestricted(self):
        gate = make_gate({"safety": {"auth_profile": "full"}})
        for tool in ("screen.capture", "pointer.click", "keyboard.type", "wait.pause"):
            self.assertEqual(gate.evaluate(tool, {}).decision, "allow", tool)

    def test_batch_inner_actions_are_gated(self):
        # evaluate_allow_only is the batch-internal path; confirmation is
        # already granted there, so authorization must still refuse.
        gate = make_gate({"safety": {"auth_profile": "read_only"}})
        self.assertEqual(gate.evaluate_allow_only("pointer.click", {}).decision, "not_authorized")
        self.assertEqual(gate.evaluate_allow_only("keyboard.type", {}).decision, "not_authorized")
        self.assertEqual(gate.evaluate_allow_only("screen.capture", {}).decision, "allow")

    def test_revoked_keeps_observation_and_stays_ready(self):
        emit = EmitRecorder()
        gate = make_gate({"safety": {"auth_profile": "revoked"}}, emit=emit)
        for tool in ("screen.capture", "a11y.snapshot"):
            self.assertEqual(gate.evaluate(tool, {}).decision, "allow", tool)
        for tool in ("pointer.click", "keyboard.type", "wait.pause", "batch.execute"):
            v = gate.evaluate(tool, {})
            self.assertEqual(v.decision, "not_authorized", tool)
            self.assertEqual(v.data.get("auth_profile"), "revoked", tool)
        # revoked is a rung on the capability ladder, not an emergency stop.
        self.assertEqual(gate.state, "ready")
        self.assertNotIn("safety.panic_triggered", emit.types())
        # the batch-internal path sees the same floor.
        self.assertEqual(gate.evaluate_allow_only("screen.capture", {}).decision, "allow")
        self.assertEqual(gate.evaluate_allow_only("pointer.click", {}).decision, "not_authorized")

    def test_entering_revoked_cancels_pending_confirmation(self):
        # An approval collected before the withdrawal must not run afterwards.
        emit = EmitRecorder()
        gate = make_gate({"safety": {"auth_profile": "full", "confirm_threshold": "high"}}, emit=emit)
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        self.assertEqual(v.decision, "confirm")
        rid = v.data["request_id"]
        gate.set_grant("revoked")
        self.assertIsNone(gate.pending_confirmation())
        self.assertIn("safety.confirmation_resolved", emit.types())
        self.assertEqual(gate.state, "ready")
        with self.assertRaises(PolicyError) as ctx:
            gate.resolve(rid, approve=True)
        self.assertEqual(ctx.exception.code, "confirmation_not_found")

    def test_reconfigure_to_revoked_cancels_pending_confirmation(self):
        # reconfigure is the funnel both channels go through.
        gate = make_gate({"safety": {"auth_profile": "limited", "confirm_threshold": "moderate"}})
        v = gate.evaluate("pointer.click", {})
        self.assertEqual(v.decision, "confirm")
        rid = v.data["request_id"]
        gate.reconfigure(from_dict({"safety": {"auth_profile": "revoked",
                                               "confirm_threshold": "moderate"}}).safety)
        self.assertIsNone(gate.pending_confirmation())
        self.assertEqual(gate.state, "ready")
        with self.assertRaises(PolicyError):
            gate.resolve(rid, approve=True)

    def test_revoked_creates_no_new_confirmations(self):
        # The grant sits ahead of the confirmation flow, so revoking cannot be
        # undone by a later action re-arming a pending request.
        emit = EmitRecorder()
        gate = make_gate({"safety": {"auth_profile": "revoked", "confirm_threshold": "high"}}, emit=emit)
        v = gate.evaluate("keyboard.combo", {"keys": ["win", "r"]})
        self.assertEqual(v.decision, "not_authorized")
        self.assertNotIn("safety.confirmation_requested", emit.types())
        self.assertEqual(gate.state, "ready")

    def test_grant_allowlist_does_not_exempt_from_deny_rules(self):
        gate = make_gate({"safety": {"auth_profile": "read_only",
                                     "rules": [{"match": {"tool": "screen.capture"}, "effect": "deny"}]}})
        self.assertEqual(gate.evaluate("screen.capture", {}).decision, "deny")

    def test_grant_check_precedes_rule_scan(self):
        # The grant sits ahead of the rules, so a tool the profile already
        # refuses reports not_authorized even when a deny rule would match.
        gate = make_gate({"safety": {"auth_profile": "read_only",
                                     "rules": [{"match": {"tool": "pointer.*"}, "effect": "deny"}]}})
        self.assertEqual(gate.evaluate("pointer.click", {}).decision, "not_authorized")


class TestIdleWatchdog(unittest.TestCase):
    def test_standby_after_idle(self):
        emit = EmitRecorder()
        gate = make_gate({"safety": {"idle_timeout_s": 0.15, "idle_action": "standby"}}, emit=emit)
        gate.start_watchdog()
        try:
            self.assertTrue(wait_for(lambda: gate.state == "standby"))
            self.assertIn("session.idle", emit.types())
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "standby")
            gate.resume()
            self.assertEqual(gate.state, "ready")
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
        finally:
            gate.close()

    def test_activity_resets_idle(self):
        emit = EmitRecorder()
        gate = make_gate({"safety": {"idle_timeout_s": 0.8, "idle_action": "standby"}}, emit=emit)
        gate.start_watchdog()
        try:
            time.sleep(0.2)
            gate.note_activity()
            time.sleep(0.2)
            self.assertNotIn("session.idle", emit.types())
            self.assertTrue(wait_for(lambda: gate.state == "standby", timeout=2.5))
        finally:
            gate.close()

    def test_idle_action_none_only_emits(self):
        emit = EmitRecorder()
        gate = make_gate({"safety": {"idle_timeout_s": 0.1, "idle_action": "none"}}, emit=emit)
        gate.start_watchdog()
        try:
            self.assertTrue(wait_for(lambda: "session.idle" in emit.types()))
            self.assertEqual(gate.state, "ready")
            self.assertEqual(gate.evaluate("screen.capture", {}).decision, "allow")
        finally:
            gate.close()

    def test_watchdog_does_not_fire_when_disabled(self):
        gate = make_gate({"safety": {"idle_timeout_s": 0, "idle_action": "standby"}})
        gate.start_watchdog()
        try:
            time.sleep(0.2)
            self.assertEqual(gate.state, "ready")
        finally:
            gate.close()


class TestReconfigure(unittest.TestCase):
    def test_reconfigure_applies_rules_and_threshold(self):
        gate = make_gate()
        gate.reconfigure(from_dict({"safety": {"confirm_threshold": "moderate"}}).safety)
        v = gate.evaluate("keyboard.type", {"text": "hi"})
        self.assertEqual(v.decision, "confirm")


if __name__ == "__main__":
    unittest.main()
