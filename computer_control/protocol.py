"""JSON-RPC 2.0 protocol layer: request routing and envelope conventions.

Transports (stdio, HTTP) are thin wrappers around :class:`Router`.

Envelope convention
-------------------
Application-level outcomes always ride inside ``result`` as

    {"ok": true,  "result": {...}, "error": null, "meta": {...}}
    {"ok": false, "result": null,  "error": {"code": ..., "message": ..., "data": {...}}, "meta": {...}}

JSON-RPC ``error`` is reserved for protocol-level failures: parse errors,
unknown methods, malformed params. Tool-level failures (policy denials,
stale snapshots, ...) are envelope errors so the caller gets one uniform
shape for every application outcome.

Events are JSON-RPC notifications:

    {"jsonrpc": "2.0", "method": "event", "params": {"type": "action.finished", "payload": {...}}}
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Optional

from computer_control.session import Session, SessionError

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000


class ProtocolError(Exception):
    """A JSON-RPC level failure."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data

    def as_jsonrpc(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.data:
            error["data"] = self.data
        return error


def load_request(line: bytes):
    """Parse one raw request line into a dict; raises ProtocolError."""
    try:
        request = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("parse error: %s" % exc, PARSE_ERROR)
    if not isinstance(request, dict):
        raise ProtocolError("request must be a JSON object", INVALID_REQUEST)
    if request.get("jsonrpc") != "2.0":
        raise ProtocolError("missing or invalid 'jsonrpc' field", INVALID_REQUEST)
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError("missing 'method' field", INVALID_REQUEST)
    params = request.get("params")
    if params is not None and not isinstance(params, dict):
        raise ProtocolError("'params' must be an object", INVALID_PARAMS)
    request["params"] = params or {}
    return request


def _rpc_response(request: dict, result=None, error=None) -> dict:
    response = {"jsonrpc": "2.0", "id": request.get("id")}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


class Router:
    """Dispatches requests to the current session. Thread-safe.

    ``event_sink`` receives (event_type, payload) for every event the session
    emits; transports install it so events reach subscribers and the wire.
    ``base_config`` (raw dict, usually the plugin's config file) is the base
    that session.start parameters override. ``driver_factory`` lets tests
    inject a recording driver.
    """

    def __init__(self, session_factory: Optional[Callable[[], Session]] = None,
                 event_sink: Optional[Callable[[str, Optional[dict]], None]] = None,
                 base_config: Optional[dict] = None,
                 driver_factory: Optional[Callable] = None):
        self._event_sink = event_sink or (lambda event_type, payload=None: None)
        self._base_config = base_config or {}
        self._driver_factory = driver_factory

        def make_default_session():
            return Session(emit=self._event_sink, base_config=self._base_config,
                           driver_factory=self._driver_factory)

        self._factory = session_factory or make_default_session
        self._session_cache = None
        self._session_lock = threading.Lock()
        self._handlers = {
            "session.start": self._session_start,
            "session.stop": self._session_stop,
            "session.configure": self._session_configure,
            "session.confirm": self._session_confirm,
            "session.resume": self._session_resume,
            "control.panic": self._control_panic,
            "tools.call": self._tools_call,
            "tools.call_batch": self._tools_call_batch,
            "tools.list": self._tools_list,
            "system.status": self._system_status,
        }

    # ------------------------------------------------------------ dispatch

    def set_event_sink(self, sink: Callable[[str, Optional[dict]], None]) -> None:
        """Install the event sink. Sessions created after this point emit
        through it (sessions are created lazily on the first request)."""
        self._event_sink = sink

    def handle(self, request: dict) -> Optional[dict]:
        """Handle one parsed request; returns a response dict, or None for
        notifications (requests without an id). Notifications never receive
        a response, not even for unknown methods."""
        is_notification = request.get("id") is None
        handler = self._handlers.get(request.get("method"))
        if handler is None:
            if is_notification:
                return None
            return _rpc_response(request, error={"code": METHOD_NOT_FOUND,
                                                 "message": "method not found: %s" % request.get("method")})
        try:
            result = handler(request.get("params") or {})
        except ProtocolError as exc:
            if is_notification:
                return None
            return _rpc_response(request, error=exc.as_jsonrpc())
        if is_notification:
            return None
        return _rpc_response(request, result)

    def handle_line(self, line: bytes) -> Optional[dict]:
        request = load_request(line)
        return self.handle(request)

    # ------------------------------------------------------------ handlers

    def _get_session(self) -> Session:
        with self._session_lock:
            if self._session_cache is None:
                self._session_cache = self._factory()
            return self._session_cache

    def _guard(self, fn, params):
        try:
            return fn()
        except SessionError as exc:
            return {"ok": False, "result": None,
                    "error": {"code": exc.code, "message": exc.message, "data": exc.data},
                    "meta": {}}

    def _session_start(self, params):
        return self._guard(lambda: self._get_session().start(params.get("config") or params), params)

    def _session_stop(self, params):
        return self._guard(self._get_session().stop, params)

    def _session_configure(self, params):
        return self._guard(lambda: self._get_session().configure(params.get("patch") or params), params)

    def _session_confirm(self, params):
        request_id = params.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return {"ok": False, "result": None,
                    "error": {"code": "invalid_arguments", "message": "request_id is required"}, "meta": {}}
        if "approve" not in params or not isinstance(params.get("approve"), bool):
            return {"ok": False, "result": None,
                    "error": {"code": "invalid_arguments", "message": "approve (boolean) is required"}, "meta": {}}
        return self._guard(lambda: self._get_session().confirm(request_id, params["approve"]), params)

    def _session_resume(self, params):
        return self._guard(self._get_session().resume, params)

    def _control_panic(self, params):
        if "on" not in params or not isinstance(params.get("on"), bool):
            return {"ok": False, "result": None,
                    "error": {"code": "invalid_arguments", "message": "on (boolean) is required"}, "meta": {}}
        return self._guard(lambda: self._get_session().panic(params["on"]), params)

    def _tools_call(self, params):
        tool = params.get("tool")
        if not isinstance(tool, str) or not tool:
            return {"ok": False, "result": None,
                    "error": {"code": "invalid_arguments", "message": "tool is required"}, "meta": {}}
        return self._guard(lambda: self._get_session().call(tool, params.get("arguments") or {}), params)

    def _tools_call_batch(self, params):
        items = params.get("items")
        if not isinstance(items, list):
            return {"ok": False, "result": None,
                    "error": {"code": "invalid_arguments", "message": "items must be a list"}, "meta": {}}
        return self._guard(lambda: self._get_session().call_batch(
            items,
            continue_on_error=params.get("continue_on_error") is True,
            gap_ms=params.get("gap_ms"),
        ), params)

    def _tools_list(self, params):
        return self._guard(self._get_session().list_tools, params)

    def _system_status(self, params):
        return self._guard(self._get_session().status, params)
