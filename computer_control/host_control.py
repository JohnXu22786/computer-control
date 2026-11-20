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

The parent directory and file must be owned by a separate host administrator:
root on Linux, or LocalSystem / built-in Administrators on Windows. The plugin
must run unprivileged and have read-only access; on Linux each process thread's
permitted, effective, ambient, and inheritable capability sets must be empty,
and its CapBnd must exclude CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FOWNER, and
CAP_SETUID, CAP_SYS_PTRACE, and CAP_SYS_ADMIN. The first four can change file
ownership, bypass file-mode checks, or switch the filesystem UID to root;
CAP_SYS_PTRACE can control processes in the same user namespace, including a
trusted host writer, and CAP_SYS_ADMIN can mount an attacker-controlled
filesystem over the control path. Inability to inspect these values fails
closed. Linux also checks mode bits and access/default POSIX ACLs. Windows
checks DACLs and opens the file without following a reparse point, then
validates and reads the same handle. Other POSIX platforms fail closed until
their ACLs can be verified.

The Linux capability check examines only the plugin's own threads; it cannot
attest same-UID sibling processes or processes launched later. Enforce the
bounding-set restriction across the entire interactive session, including its
desktop and terminal launchers, or isolate the host writer and plugin under
identities that prevent sibling access. Restricting only the plugin process
does not protect this channel. The plugin's interactive OS identity must not
have sudo or administrator rights: desktop input could otherwise open a shell
as that user and rewrite a file owned by the same user.

    {"seq": 1, "auth_profile": "full", "a11y_snapshot": true}

- ``seq`` (required): a positive integer, strictly increasing per
  directive. Only ``seq`` values above the last applied one take effect,
  which makes re-granting the same profile possible (bump the seq and
  rewrite the file) and makes replays of an old file content no-ops within
  the process lifetime. The sequence counter is in memory: after a process
  restart the persistent file is applied again as the host's current desired
  state. Keep the complete snapshot in place if both settings should be
  restored. If the file is removed, a running session keeps its current
  settings, while a new process starts from its host config.
- ``auth_profile`` (required): revoked / read_only / limited / full; any
  direction is allowed - the file itself is the host's authority.
- ``a11y_snapshot`` (required): the host's explicit opt-in/out for
  ``a11y.snapshot`` under the read_only / revoked profiles.

Every directive is a complete desired-state snapshot. When changing one
setting, repeat the current value of the other; omitted settings are rejected
so a later profile update cannot drop a persisted accessibility opt-out.

A directive that arrives while no session is running stays pending: it is
retried every tick and lands on the next live session. Deleting the file does
not change a running session. If a new process starts without the file, its
session starts from the host config instead of replaying the previous snapshot.

The channel reads the file but never writes it.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Callable, Optional

from computer_control.config import AUTH_PROFILES
from computer_control.control_permissions import read_control_file, validate_control_directory

_DIRECTIVE_KEYS = frozenset({"seq", "auth_profile", "a11y_snapshot"})
_OPTIONAL_DIRECTIVE_KEYS = frozenset({"confirmation"})


class HostControlChannel:
    """Polls a host-written directive file and applies valid directives.

    ``apply`` receives the validated directive dict (``auth_profile`` and/or
    ``a11y_snapshot``) and returns True when it was applied. Returning False
    means "not now" (e.g. no live session): the seq is not consumed and the
    directive is retried on the next poll.
    """

    def __init__(self, path: str, apply: Callable[[dict], bool],
                 log: Optional[Callable[[str], None]] = None):
        absolute_path = os.path.abspath(str(path))
        parent = os.path.realpath(os.path.dirname(absolute_path) or os.curdir)
        self._path = os.path.join(parent, os.path.basename(absolute_path))
        validate_control_directory(parent)
        self._apply = apply
        self._log = log or (lambda message: None)
        self._last_seq = 0
        self._last_note = ""
        self._lock = threading.Lock()
        # Serialize polling and session startup without blocking close on apply.
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                self._thread = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="host-control", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
            if not thread.is_alive():
                with self._lock:
                    if self._thread is thread:
                        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            self.poll_once()

    def start_session(self, start: Callable[[Optional[dict]], object]):
        """Start with any pending snapshot applied before the session is live."""
        with self._poll_lock:
            with self._lock:
                pending = self._read_pending_locked()
            if pending is None:
                return start(None)
            seq, directive = pending
            result = start(directive)
            if isinstance(result, dict) and result.get("ok") is False:
                # Protocol handlers report session.start failures as a result
                # object rather than raising. Keep the snapshot pending so a
                # later start can apply it instead of consuming its sequence.
                return result
            with self._lock:
                self._last_seq = seq
                self._last_note = ""
            return result

    def poll_once(self) -> bool:
        """One poll. Returns True when a directive was applied."""
        with self._poll_lock:
            with self._lock:
                try:
                    pending = self._read_pending_locked()
                except (OSError, UnicodeError) as exc:
                    self._note("control file: could not read directive; will retry (%s)" % exc)
                    return False
            if pending is None:
                return False
            seq, directive = pending

            try:
                applied = self._apply(directive)
            except Exception as exc:
                # An apply-side failure must not wedge the channel on one bad
                # directive forever: consume it and let the host observe the
                # missing grant_changed / session.configured event and rewrite.
                with self._lock:
                    self._last_seq = seq
                    self._note("control file: directive seq %d could not be applied: %s"
                               % (seq, exc))
                return False
            if applied:
                with self._lock:
                    self._last_seq = seq
                    self._last_note = ""
            return bool(applied)

    def _read_pending_locked(self):
        try:
            raw = read_control_file(self._path)
        except FileNotFoundError:
            return None  # no directive file
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):
            self._note("control file: content is not valid JSON - directive ignored")
            return None
        if not isinstance(doc, dict):
            self._note("control file: directive must be a JSON object - ignored")
            return None
        unknown = set(doc) - _DIRECTIVE_KEYS - _OPTIONAL_DIRECTIVE_KEYS
        if unknown:
            self._note("control file: unknown key(s) %s - directive ignored"
                       % ", ".join(sorted(unknown)))
            return None
        missing = _DIRECTIVE_KEYS - set(doc)
        if missing:
            self._note("control file: missing required key(s) %s - directive ignored"
                       % ", ".join(sorted(missing)))
            return None
        seq = doc.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            self._note("control file: 'seq' must be a positive integer - directive ignored")
            return None
        if seq <= self._last_seq:
            return None  # already applied; idempotent re-read of an old directive

        confirmation = doc.get("confirmation")
        if confirmation is not None:
            if (not isinstance(confirmation, dict)
                    or set(confirmation) != {"request_id", "approve"}
                    or not isinstance(confirmation.get("request_id"), str)
                    or not confirmation["request_id"]
                    or not isinstance(confirmation.get("approve"), bool)):
                self._note("control file: 'confirmation' must contain a request_id and boolean approve - directive ignored")
                return None

        profile = doc["auth_profile"]
        if profile not in AUTH_PROFILES:
            self._note("control file: auth_profile %r is not one of %s - directive ignored"
                       % (profile, "/".join(AUTH_PROFILES)))
            return None
        opt_in = doc["a11y_snapshot"]
        if not isinstance(opt_in, bool):
            self._note("control file: 'a11y_snapshot' must be a boolean - directive ignored")
            return None
        directive = {"auth_profile": profile, "a11y_snapshot": opt_in}
        if confirmation is not None:
            directive["confirmation"] = confirmation
        return seq, directive

    def _note(self, message: str) -> None:
        if message == self._last_note:
            return
        self._last_note = message
        self._log(message)
