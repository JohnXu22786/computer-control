"""Tests for the out-of-band host control channel (host_control).

All tests drive poll_once directly - the 0.1s polling thread is only smoke-
tested for start/close, so every semantic test is deterministic (no sleeps).
"""

import json
import os
import tempfile
import threading
import unittest

from computer_control.host_control import HostControlChannel
from computer_control.protocol import Router
from computer_control.session import Session
from computer_control.drivers.dummy import NullDriver


def make_session(base_config=None, emit=None):
    return Session(emit=emit or (lambda t, p=None: None),
                   driver_factory=lambda cfg: NullDriver(enable_a11y=True),
                   base_config=base_config or {})


class TestChannelParsingAndSeq(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "control.json")
        self.applied = []
        self.notes = []
        # apply callbacks must return True on success (falsy means "not now"
        # and the directive stays pending) - list.append alone returns None
        self.channel = HostControlChannel(self.path, self._record,
                                          log=self.notes.append)

    def _record(self, directive):
        self.applied.append(directive)
        return True

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def test_seq_advances_and_directive_applies(self):
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited"}])
        self.assertEqual(self.channel.last_seq, 1)

    def test_same_seq_is_idempotent(self):
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertTrue(self.channel.poll_once())
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(len(self.applied), 1)

    def test_lower_seq_is_ignored(self):
        self.write({"seq": 3, "auth_profile": "limited"})
        self.assertTrue(self.channel.poll_once())
        self.write({"seq": 2, "auth_profile": "revoked"})
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited"}])

    def test_regrant_same_profile_via_seq_bump_applies(self):
        # limited -> (agent narrows further) -> the host re-grants limited:
        # same profile value, so the edge trigger has to be the seq.
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertTrue(self.channel.poll_once())
        self.applied.clear()
        self.write({"seq": 2, "auth_profile": "limited"})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited"}])

    def test_a11y_snapshot_directive_maps_through(self):
        self.write({"seq": 1, "a11y_snapshot": True})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"a11y_snapshot": True}])

    def test_invalid_content_ignored_and_seq_not_consumed(self):
        for doc in ("not json", json.dumps([1, 2]), json.dumps("x"),
                    json.dumps({"auth_profile": "full"}),  # missing seq
                    json.dumps({"seq": 0, "auth_profile": "full"}),  # seq must be positive
                    json.dumps({"seq": True, "auth_profile": "full"}),  # bool is not an int
                    json.dumps({"seq": 1, "auth_profile": "admin"}),  # unknown profile
                    json.dumps({"seq": 1, "a11y_snapshot": "yes"}),  # not a bool
                    json.dumps({"seq": 1, "profile": "full"}),  # unknown key
                    json.dumps({"seq": 1})):  # carries no change
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(doc)
            self.assertFalse(self.channel.poll_once(), doc)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.channel.last_seq, 0)
        self.assertTrue(self.notes)  # the rejections were surfaced once each
        # a corrected directive with the same seq still applies
        self.write({"seq": 1, "auth_profile": "read_only"})
        self.assertTrue(self.channel.poll_once())

    def test_missing_file_is_a_no_op(self):
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.applied, [])

    def test_apply_false_keeps_directive_pending(self):
        outcomes = [False, True]
        self.channel._apply = lambda directive: outcomes.pop(0)
        self.write({"seq": 1, "auth_profile": "full"})
        self.assertFalse(self.channel.poll_once())  # no live session yet
        self.assertEqual(self.channel.last_seq, 0)
        self.assertTrue(self.channel.poll_once())  # retried and applied
        self.assertEqual(self.channel.last_seq, 1)

    def test_apply_exception_consumes_directive_and_survives(self):
        def boom(directive):
            raise RuntimeError("session blew up")
        self.channel._apply = boom
        self.write({"seq": 1, "auth_profile": "full"})
        self.assertFalse(self.channel.poll_once())
        # consumed: a retried poll does not hammer the apply forever
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.channel.last_seq, 1)
        self.assertTrue(any("could not be applied" in note for note in self.notes))

    def test_poll_thread_starts_and_stops(self):
        self.channel.start()
        self.channel.start()  # idempotent
        # the daemon thread picks the directive up on its own 0.1s tick; the
        # apply callback signals an Event so the wait is bounded - no spin
        # loop starving the poller, no stray thread to wedge the process
        applied = threading.Event()
        inner_apply = self.channel._apply

        def apply_and_signal(directive):
            result = inner_apply(directive)
            if result:
                applied.set()
            return result

        self.channel._apply = apply_and_signal
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertTrue(applied.wait(5.0))
        self.channel.close()
        self.channel.close()  # idempotent
        self.assertIsNone(self.channel._thread)
        self.assertEqual(self.applied, [{"auth_profile": "limited"}])
        self.assertEqual(self.channel.last_seq, 1)


class TestRouterIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "control.json")
        self.events = []
        self.router = None

    def make_router(self, base_config=None):
        lock = threading.Lock()
        events = self.events

        def emit(event_type, payload=None):
            with lock:
                events.append((event_type, payload))

        def factory():
            return make_session(base_config, emit)

        self.router = Router(factory, event_sink=emit, control_file=self.path)
        # These tests drive poll_once() directly so seq consumption is
        # deterministic: with the background poller alive its 0.1s tick would
        # race the manual polls for the seq edge. The thread itself (start /
        # close / picking a directive up unprompted) is covered in
        # TestChannelParsingAndSeq.test_poll_thread_starts_and_stops.
        self.router._control_channel.close()
        return self.router

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def tearDown(self):
        if self.router is not None:
            with self.router._session_lock:
                session = self.router._session_cache
            if session is not None:
                try:
                    session.stop()
                except Exception:
                    pass
            self.router.close()

    def test_directive_grants_widen_and_reanchor_floor(self):
        # The sticky floor must follow the host's grant, not stay stuck at
        # the agent's last narrowing: after full -> read_only (agent) ->
        # limited (host), a restart lands on limited - neither the config
        # file's full nor the stale read_only floor.
        router = self.make_router({"safety": {"auth_profile": "full"}})
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        with router._session_lock:
            session = router._session_cache
        router.handle({"jsonrpc": "2.0", "id": 2, "method": "session.configure",
                      "params": {"patch": {"safety": {"auth_profile": "read_only"}}}})
        self.assertEqual((session.call("pointer.click", {}).get("error") or {}).get("code"),
                         "not_authorized")
        self.write({"seq": 1, "auth_profile": "limited"})
        self.assertTrue(router._control_channel.poll_once())

        # the grant took effect and announced itself as a host widening
        self.assertTrue(session.call("pointer.click", {})["ok"])
        grants = [p for t, p in self.events if t == "safety.grant_changed"]
        self.assertEqual(grants[-1], {"from": "read_only", "to": "limited",
                                      "actor": "host", "widened": True})
        # the floor re-anchored: a stop/start keeps the granted level
        session.stop()
        r = session.start({})
        self.assertEqual(r["result"]["auth_profile"], "limited")
        self.assertEqual((session.call("keyboard.combo", {"keys": ["a"]}).get("error") or {})
                         .get("code"), "not_authorized")
        self.assertTrue(session.call("pointer.click", {})["ok"])

    def test_directive_stays_pending_until_a_session_is_live(self):
        router = self.make_router({"safety": {"auth_profile": "read_only"}})
        self.write({"seq": 1, "auth_profile": "full"})
        self.assertFalse(router._control_channel.poll_once())  # nothing running
        self.assertEqual(router._control_channel.last_seq, 0)
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        self.assertTrue(router._control_channel.poll_once())
        with router._session_lock:
            session = router._session_cache
        self.assertEqual(session._cfg.safety.auth_profile, "full")
        self.assertTrue(session.call("pointer.click", {})["ok"])

    def test_directive_revokes_and_cancels_pending_batch_confirmation(self):
        router = self.make_router({"safety": {"auth_profile": "full",
                                              "confirm_threshold": "moderate"}})
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        with router._session_lock:
            session = router._session_cache
        r = session.call_batch([{"tool": "pointer.click", "arguments": {"button": "left"}},
                                {"tool": "keyboard.combo", "arguments": {"keys": ["ctrl", "c"]}}])
        request_id = r["result"]["request_id"]
        self.assertEqual(r["result"]["status"], "awaiting_confirmation")
        # the host revokes over the control channel; the pending confirmation
        # must be cancelled (approve:false on the resolved event), not executed
        self.write({"seq": 1, "auth_profile": "revoked"})
        self.assertTrue(router._control_channel.poll_once())
        self.assertIsNone(session._gate.pending_confirmation())
        resolved = [p for t, p in self.events if t == "safety.confirmation_resolved"]
        self.assertEqual([p.get("approve") for p in resolved], [False])
        response = router.handle({"jsonrpc": "2.0", "id": 2, "method": "session.confirm",
                                  "params": {"request_id": request_id, "approve": True}})
        self.assertEqual(response["result"]["error"]["code"], "confirmation_not_found")

    def test_control_file_is_invisible_in_band(self):
        # The channel's path appears in no config surface the agent can name:
        # it is not a config key, not a routed method, not in the manifest.
        from computer_control import manifest

        router = self.make_router({"safety": {"auth_profile": "read_only"}})
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        for method in ("control.grant", "control.grant_profile", "session.grant",
                       "host.grant", "control.channel"):
            response = router.handle({"jsonrpc": "2.0", "id": 2, "method": method,
                                      "params": {"auth_profile": "full"}})
            self.assertEqual(response["error"]["code"], -32601, method)
        for tool in manifest.manifest_tools():
            self.assertNotIn("control", tool["name"])
        # and there is no in-band way to point the channel anywhere else:
        # the safety schema knows no such key, so naming it is invalid_config
        with router._session_lock:
            session = router._session_cache
        session.stop()
        response = router.handle({"jsonrpc": "2.0", "id": 3, "method": "session.start",
                                  "params": {"config": {"safety": {"control_file": "x"}}}})
        self.assertEqual(response["result"]["error"]["code"], "invalid_config")


if __name__ == "__main__":
    unittest.main()
