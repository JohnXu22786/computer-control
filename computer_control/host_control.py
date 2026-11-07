"""Out-of-band host control channel.

DSH runs this plugin as a stdio subprocess, so the host cannot make a
direct Python call (Session.grant_profile) to change the authorization
profile at runtime - and every method routed through the JSON-RPC table is
by definition reachable by the agent, so no routed method may be able to
widen authorization either.

This module is the host-only side door: the host writes one small JSON
directive to a file whose path is fixed at process start (``serve
--control-file`` or the ``COMPUTER_CONTROL_CONTROL_FILE`` environment
variable) and the channel polls it. The agent cannot reach it:

- the path never appears in the configuration schema, the router table,
  the manifest or any event payload, so no in-band request can name or
  redirect it (a ``safety`` patch carrying host-only keys is rejected by
  the session outright);
- the plugin exposes no file-writing tool, so the agent has no way to
  produce or rewrite the directive file through the plugin itself.

Directive file format (one JSON object per file, host replaces the file
atomically - write a temp file in the same directory, then os.replace):

    {"seq": 1, "auth_profile": "full", "a11y_snapshot": true}

- ``seq`` (required): a positive integer, strictly increasing per
  directive. Only ``seq`` values above the last applied one take effect,
  which makes re-granting the same profile possible (bump the seq and
  rewrite the file) and makes replays of an old file content no-ops.
- ``auth_profile`` (optional): revoked / read_only / limited / full; any
  direction is allowed - the file itself is the host's authority.
- ``a11y_snapshot`` (optional): the host's explicit opt-in for
  ``a11y.snapshot`` under the read_only / revoked profiles.

A directive that arrives while no session is running stays pending: it is
retried every tick and lands on the next live session. Deleting the file
is NOT a revoke - the last applied grant persists (the sticky floor keeps
holding it); revocation is an explicit directive.

The channel reads the file but never writes it.
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Optional

from computer_control.config import AUTH_PROFILES

_DIRECTIVE_KEYS = frozenset({"seq", "auth_profile", "a11y_snapshot"})


class HostControlChannel:
    """Polls a host-written directive file and applies valid directives.

    ``apply`` receives the validated directive dict (``auth_profile`` and/or
    ``a11y_snapshot``) and returns True when it was applied. Returning False
    means "not now" (e.g. no live session): the seq is not consumed and the
    directive is retried on the next poll.
    """

    def __init__(self, path: str, apply: Callable[[dict], bool],
                 log: Optional[Callable[[str], None]] = None):
        self._path = str(path)
        self._apply = apply
        self._log = log or (lambda message: None)
        self._last_seq = 0
        self._last_note = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="host-control", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            self.poll_once()

    def poll_once(self) -> bool:
        """One poll. Returns True when a directive was applied."""
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                return False  # no directive file (or transient replace window)
            try:
                doc = json.loads(raw)
            except ValueError:
                self._note("control file: content is not valid JSON - directive ignored")
                return False
            if not isinstance(doc, dict):
                self._note("control file: directive must be a JSON object - ignored")
                return False
            unknown = set(doc) - _DIRECTIVE_KEYS
            if unknown:
                self._note("control file: unknown key(s) %s - directive ignored"
                           % ", ".join(sorted(unknown)))
                return False
            seq = doc.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
                self._note("control file: 'seq' must be a positive integer - directive ignored")
                return False
            if seq <= self._last_seq:
                return False  # already applied; idempotent re-read of an old directive

            directive = {}
            profile = doc.get("auth_profile")
            if profile is not None:
                if profile not in AUTH_PROFILES:
                    self._note("control file: auth_profile %r is not one of %s - directive ignored"
                               % (profile, "/".join(AUTH_PROFILES)))
                    return False
                directive["auth_profile"] = profile
            opt_in = doc.get("a11y_snapshot")
            if opt_in is not None:
                if not isinstance(opt_in, bool):
                    self._note("control file: 'a11y_snapshot' must be a boolean - directive ignored")
                    return False
                directive["a11y_snapshot"] = opt_in
            if not directive:
                self._note("control file: directive carries no change - ignored")
                return False

            try:
                applied = self._apply(directive)
            except Exception as exc:
                # An apply-side failure must not wedge the channel on one bad
                # directive forever: consume it and let the host observe the
                # missing grant_changed / session.configured event and rewrite.
                self._last_seq = seq
                self._note("control file: directive seq %d could not be applied: %s" % (seq, exc))
                return False
            if applied:
                self._last_seq = seq
                self._last_note = ""
            return bool(applied)

    def _note(self, message: str) -> None:
        if message == self._last_note:
            return
        self._last_note = message
        self._log(message)
