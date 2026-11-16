"""Tests for the out-of-band host control channel (host_control).

All tests drive poll_once directly - the 0.1s polling thread is only smoke-
tested for start/close, so every semantic test is deterministic (no sleeps).
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from computer_control import control_permissions
from computer_control.host_control import HostControlChannel
from computer_control.protocol import Router
from computer_control.session import Session
from computer_control.drivers.dummy import NullDriver


def _supports_host_control_acl_validation():
    return sys.platform.startswith("linux") or os.name == "nt"


class TestUnsupportedHostControlPlatform(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "non-Linux POSIX platform behavior")
    def test_non_linux_posix_is_unsupported_and_acl_validation_fails_closed(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertFalse(_supports_host_control_acl_validation())
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(PermissionError):
                    control_permissions.validate_control_directory(directory)

                path = os.path.join(directory, "control.json")
                with open(path, "w", encoding="utf-8") as control_file:
                    control_file.write("{}")
                with self.assertRaises(PermissionError):
                    control_permissions.validate_control_file(path)


def make_session(base_config=None, emit=None):
    return Session(emit=emit or (lambda t, p=None: None),
                   driver_factory=lambda cfg: NullDriver(enable_a11y=True),
                   base_config=base_config or {})


@unittest.skipUnless(_supports_host_control_acl_validation(),
                     "host control ACL verification is unsupported on this platform")
class TestChannelParsingAndSeq(unittest.TestCase):
    def setUp(self):
        self._identity_policy = mock.patch.object(
            control_permissions, "_validate_host_control_identity")
        self._identity_policy.start()
        self.addCleanup(self._identity_policy.stop)
        self._windows_acl = mock.patch.object(control_permissions, "_validate_windows_acl")
        self._windows_acl.start()
        self.addCleanup(self._windows_acl.stop)
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
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited", "a11y_snapshot": False}])
        self.assertEqual(self.channel.last_seq, 1)

    def test_same_seq_is_idempotent(self):
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(len(self.applied), 1)

    def test_lower_seq_is_ignored(self):
        self.write({"seq": 3, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.write({"seq": 2, "auth_profile": "revoked", "a11y_snapshot": True})
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited", "a11y_snapshot": False}])

    def test_persistent_directive_is_reapplied_after_process_restart(self):
        self.write({"seq": 1, "auth_profile": "revoked", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.write({"seq": 2, "auth_profile": "read_only", "a11y_snapshot": False})

        restarted_applied = []
        restarted = HostControlChannel(self.path,
                                       lambda directive: restarted_applied.append(directive) or True)
        self.assertTrue(restarted.poll_once())
        self.assertEqual(restarted.last_seq, 2)
        self.assertEqual(restarted_applied,
                         [{"auth_profile": "read_only", "a11y_snapshot": False}])

    def test_regrant_same_profile_via_seq_bump_applies(self):
        # limited -> (agent narrows further) -> the host re-grants limited:
        # same profile value, so the edge trigger has to be the seq.
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.applied.clear()
        self.write({"seq": 2, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "limited", "a11y_snapshot": False}])

    def test_a11y_snapshot_directive_maps_through(self):
        self.write({"seq": 1, "auth_profile": "read_only", "a11y_snapshot": True})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.applied, [{"auth_profile": "read_only", "a11y_snapshot": True}])

    def test_incomplete_snapshot_is_rejected_without_consuming_sequence(self):
        for doc in ({"seq": 1, "auth_profile": "read_only"},
                    {"seq": 1, "a11y_snapshot": False}):
            with self.subTest(doc=doc):
                self.write(doc)
                self.assertFalse(self.channel.poll_once())
                self.assertEqual(self.channel.last_seq, 0)
                self.assertEqual(self.applied, [])

        self.write({"seq": 1, "auth_profile": "read_only", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.channel.last_seq, 1)

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
        self.write({"seq": 1, "auth_profile": "read_only", "a11y_snapshot": False})
        self.assertTrue(self.channel.poll_once())

    def test_deeply_nested_json_does_not_stop_polling(self):
        deep_json = "[" * 100_000 + "0" + "]" * 100_000
        valid_directive = json.dumps({"seq": 1, "auth_profile": "limited",
                                      "a11y_snapshot": False})
        with mock.patch("computer_control.host_control.read_control_file",
                        side_effect=[deep_json, valid_directive]), \
                mock.patch.object(self.channel._stop, "wait",
                                  side_effect=[False, False, True]):
            self.channel._loop()

        self.assertEqual(self.applied,
                         [{"auth_profile": "limited", "a11y_snapshot": False}])
        self.assertEqual(self.channel.last_seq, 1)
        self.assertTrue(any("content is not valid JSON" in note for note in self.notes))

    def test_missing_file_is_a_no_op(self):
        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.notes, [])

    def test_missing_file_starts_without_a_snapshot(self):
        started_with = []
        result = self.channel.start_session(lambda directive: started_with.append(directive) or "started")
        self.assertEqual(result, "started")
        self.assertEqual(started_with, [None])
        self.assertEqual(self.channel.last_seq, 0)

    def test_poll_read_error_is_logged_and_sequence_remains_retryable(self):
        directive = json.dumps({"seq": 1, "auth_profile": "limited",
                                "a11y_snapshot": False})
        with mock.patch("computer_control.host_control.read_control_file",
                        side_effect=[PermissionError("temporary permission failure"),
                                     OSError("temporary read failure"),
                                     UnicodeDecodeError("utf-8", b"\xff", 0, 1,
                                                        "invalid start byte"), directive]):
            self.assertFalse(self.channel.poll_once())
            self.assertEqual(self.channel.last_seq, 0)
            self.assertFalse(self.applied)
            self.assertTrue(any("temporary permission failure" in note
                                for note in self.notes))
            self.assertFalse(self.channel.poll_once())
            self.assertEqual(self.channel.last_seq, 0)
            self.assertFalse(self.applied)
            self.assertTrue(any("temporary read failure" in note for note in self.notes))
            self.assertFalse(self.channel.poll_once())
            self.assertEqual(self.channel.last_seq, 0)
            self.assertFalse(self.applied)
            self.assertTrue(any("invalid start byte" in note for note in self.notes))
            self.assertTrue(self.channel.poll_once())
        self.assertEqual(self.channel.last_seq, 1)
        self.assertEqual(self.applied, [{"auth_profile": "limited", "a11y_snapshot": False}])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are checked on POSIX")
    def test_insecure_parent_permissions_rejected(self):
        parent_mode = os.stat(self.tmp.name).st_mode & 0o777
        os.chmod(self.tmp.name, parent_mode | 0o002)
        try:
            with self.assertRaises(PermissionError):
                HostControlChannel(self.path, self._record)
        finally:
            os.chmod(self.tmp.name, parent_mode)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are checked on POSIX")
    def test_world_writable_directive_file_is_not_applied(self):
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})
        os.chmod(self.path, 0o666)

        self.assertFalse(self.channel.poll_once())
        self.assertEqual(self.channel.last_seq, 0)
        self.assertEqual(self.applied, [])

    def test_apply_false_keeps_directive_pending(self):
        outcomes = [False, True]
        self.channel._apply = lambda directive: outcomes.pop(0)
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})
        self.assertFalse(self.channel.poll_once())  # no live session yet
        self.assertEqual(self.channel.last_seq, 0)
        self.assertTrue(self.channel.poll_once())  # retried and applied
        self.assertEqual(self.channel.last_seq, 1)

    def test_apply_exception_consumes_directive_and_survives(self):
        def boom(directive):
            raise RuntimeError("session blew up")
        self.channel._apply = boom
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})
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
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.assertTrue(applied.wait(5.0))
        self.channel.close()
        self.channel.close()  # idempotent
        self.assertIsNone(self.channel._thread)
        self.assertEqual(self.applied, [{"auth_profile": "limited", "a11y_snapshot": False}])
        self.assertEqual(self.channel.last_seq, 1)

    def test_close_does_not_wait_for_inflight_apply(self):
        callback_started = threading.Event()
        release_callback = threading.Event()
        close_returned = threading.Event()
        inner_apply = self.channel._apply

        def blocking_apply(directive):
            callback_started.set()
            release_callback.wait()
            return inner_apply(directive)

        self.channel._apply = blocking_apply
        self.channel.start()
        poller = self.channel._thread
        closer = None
        try:
            self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
            self.assertTrue(callback_started.wait(2.0))
            closer = threading.Thread(
                target=lambda: (self.channel.close(), close_returned.set()), daemon=True)
            closer.start()

            self.assertTrue(close_returned.wait(2.0), "close waited for the apply callback")
            self.assertTrue(poller.is_alive())
            self.assertEqual(self.channel.last_seq, 0)
        finally:
            release_callback.set()
            if closer is not None:
                closer.join(2.0)
            self.channel.close()
            poller.join(2.0)

        self.assertFalse(closer.is_alive() if closer is not None else False)
        self.assertFalse(poller.is_alive())
        self.assertEqual(self.channel.last_seq, 1)

    def test_start_after_timed_out_close_waits_for_old_poller_to_exit(self):
        callback_started = threading.Event()
        release_callback = threading.Event()
        inner_apply = self.channel._apply

        def blocking_apply(directive):
            callback_started.set()
            release_callback.wait()
            return inner_apply(directive)

        self.channel._apply = blocking_apply
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
        self.channel.start()
        poller = self.channel._thread
        replacement = None
        try:
            self.assertTrue(callback_started.wait(2.0))
            self.channel.close()
            self.assertTrue(poller.is_alive(), "close should return after its bounded join")

            self.channel.start()
            replacement = self.channel._thread
            self.assertIs(replacement, poller,
                          "start must not replace a poller that is still in its callback")

            release_callback.set()
            poller.join(2.0)
            self.assertFalse(poller.is_alive())

            self.channel.start()
            replacement = self.channel._thread
            self.assertIsNot(replacement, poller,
                             "start should work after the previous poller has exited")
        finally:
            release_callback.set()
            self.channel.close()
            poller.join(2.0)
            if replacement is not None and replacement is not poller:
                replacement.join(2.0)

        self.assertEqual(self.channel.last_seq, 1)
        self.assertEqual(self.applied,
                         [{"auth_profile": "limited", "a11y_snapshot": False}])


@unittest.skipUnless(_supports_host_control_acl_validation(),
                     "host control ACL verification is unsupported on this platform")
class TestRouterIntegration(unittest.TestCase):
    def setUp(self):
        self._identity_policy = mock.patch.object(
            control_permissions, "_validate_host_control_identity")
        self._identity_policy.start()
        self.addCleanup(self._identity_policy.stop)
        self._windows_acl = mock.patch.object(control_permissions, "_validate_windows_acl")
        self._windows_acl.start()
        self.addCleanup(self._windows_acl.stop)
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
        self.write({"seq": 1, "auth_profile": "limited", "a11y_snapshot": False})
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

    def test_unreadable_directive_does_not_start_session(self):
        router = self.make_router()
        with mock.patch("computer_control.host_control.read_control_file",
                        side_effect=PermissionError("control file unreadable")):
            with self.assertRaisesRegex(PermissionError, "control file unreadable"):
                router.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "session.start", "params": {}})
        self.assertIsNotNone(router._session_cache)
        self.assertFalse(router._session_cache.started)
        self.assertEqual(router._control_channel.last_seq, 0)

    def test_unexpected_control_file_io_error_does_not_start_session(self):
        router = self.make_router()
        with mock.patch("computer_control.host_control.read_control_file",
                        side_effect=OSError("control I/O failure")):
            with self.assertRaisesRegex(OSError, "control I/O failure"):
                router.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "session.start", "params": {}})
        self.assertIsNotNone(router._session_cache)
        self.assertFalse(router._session_cache.started)
        self.assertEqual(router._control_channel.last_seq, 0)

    def test_process_restart_replays_complete_host_snapshot(self):
        base = {"safety": {"auth_profile": "read_only",
                            "a11y_snapshot_opt_in": True}}
        first_router = self.make_router(base)
        first_router.handle({"jsonrpc": "2.0", "id": 1,
                             "method": "session.start", "params": {}})
        self.write({"seq": 1, "auth_profile": "read_only", "a11y_snapshot": False})
        self.assertTrue(first_router._control_channel.poll_once())
        first_session = first_router._session_cache
        self.assertEqual((first_session.call("a11y.snapshot", {}).get("error") or {}).get("code"),
                         "not_authorized")

        self.write({"seq": 2, "auth_profile": "read_only", "a11y_snapshot": False})
        self.assertTrue(first_router._control_channel.poll_once())
        first_session.stop()
        first_router.close()

        second_router = self.make_router(base)
        second_router.handle({"jsonrpc": "2.0", "id": 2,
                              "method": "session.start", "params": {}})
        second_session = second_router._session_cache
        self.assertEqual(second_session._cfg.safety.auth_profile, "read_only")
        self.assertEqual((second_session.call("a11y.snapshot", {}).get("error") or {}).get("code"),
                         "not_authorized")
        self.assertEqual(second_router._control_channel.last_seq, 2)

    def test_process_restart_caps_persisted_snapshot_at_new_base_config(self):
        first_router = self.make_router({"safety": {"auth_profile": "full"}})
        first_router.handle({"jsonrpc": "2.0", "id": 1,
                             "method": "session.start", "params": {}})
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})
        self.assertTrue(first_router._control_channel.poll_once())
        first_router._session_cache.stop()
        first_router.close()

        second_router = self.make_router({"safety": {"auth_profile": "read_only"}})
        second_router.handle({"jsonrpc": "2.0", "id": 2,
                             "method": "session.start", "params": {}})
        session = second_router._session_cache
        self.assertEqual(session._cfg.safety.auth_profile, "read_only")
        self.assertEqual((session.call("pointer.click", {}).get("error") or {}).get("code"),
                         "not_authorized")

    def test_pending_host_grant_overrides_narrow_session_start_profile(self):
        router = self.make_router({"safety": {"auth_profile": "full"}})
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})

        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start",
                       "params": {"config": {"safety": {"auth_profile": "read_only"}}}})
        session = router._session_cache
        self.assertEqual(session._cfg.safety.auth_profile, "full")
        self.assertTrue(session.call("pointer.click", {})["ok"])

    def test_pending_host_grant_overrides_same_session_profile_floor(self):
        router = self.make_router({"safety": {"auth_profile": "full"}})
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start",
                       "params": {"config": {"safety": {"auth_profile": "read_only"}}}})
        session = router._session_cache
        session.stop()
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})

        router.handle({"jsonrpc": "2.0", "id": 2, "method": "session.start", "params": {}})
        self.assertEqual(session._cfg.safety.auth_profile, "full")
        self.assertTrue(session.call("pointer.click", {})["ok"])

    def test_pending_directive_is_capped_at_config_on_start(self):
        router = self.make_router({"safety": {"auth_profile": "read_only"}})
        self.write({"seq": 1, "auth_profile": "full", "a11y_snapshot": False})
        self.assertFalse(router._control_channel.poll_once())  # nothing running
        self.assertEqual(router._control_channel.last_seq, 0)
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        self.assertEqual(router._control_channel.last_seq, 1)
        with router._session_lock:
            session = router._session_cache
        self.assertEqual(session._cfg.safety.auth_profile, "read_only")
        self.assertEqual((session.call("pointer.click", {}).get("error") or {}).get("code"),
                         "not_authorized")

    def test_stop_race_keeps_directive_pending_until_restart(self):
        router = self.make_router({"safety": {"auth_profile": "read_only",
                                                "a11y_snapshot_opt_in": True}})
        router.handle({"jsonrpc": "2.0", "id": 1, "method": "session.start", "params": {}})
        session = router._session_cache
        self.assertTrue(session.call("a11y.snapshot", {})["ok"])
        apply_directive = session.apply_host_directive
        stop_once = {"pending": True}

        def stop_then_apply(directive):
            if stop_once["pending"]:
                stop_once["pending"] = False
                session.stop()
            return apply_directive(directive)

        session.apply_host_directive = stop_then_apply
        self.write({"seq": 1, "auth_profile": "read_only", "a11y_snapshot": False})
        self.assertFalse(router._control_channel.poll_once())
        self.assertEqual(router._control_channel.last_seq, 0)

        router.handle({"jsonrpc": "2.0", "id": 2,
                       "method": "session.start", "params": {}})
        self.assertEqual(router._control_channel.last_seq, 1)
        self.assertEqual(session._cfg.safety.auth_profile, "read_only")
        self.assertEqual((session.call("a11y.snapshot", {}).get("error") or {}).get("code"),
                         "not_authorized")

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
        self.write({"seq": 1, "auth_profile": "revoked", "a11y_snapshot": False})
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
