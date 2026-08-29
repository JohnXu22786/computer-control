"""Servers: stdio transport (the primary one) and a small HTTP transport.

stdio
-----
Line-delimited JSON-RPC 2.0 over stdin/stdout, UTF-8, one JSON object per
line. stdout carries ONLY protocol traffic; logs go to stderr. The harness
spawns ``python -m computer_control serve`` and talks to these pipes.

HTTP
----
POST /rpc          one request object per call
GET  /health       liveness + state
GET  /events       Server-Sent Events; replays the recent event ring then
                   streams live events

Both transports install an event sink on the router so every session event
reaches the wire (stdio notification) and the event ring (HTTP replay).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Callable, Optional

from computer_control.protocol import ProtocolError, Router, load_request

_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with _LOG_LOCK:
        sys.stderr.write("[computer-control] %s\n" % message)
        sys.stderr.flush()


class EventRing:
    """Bounded ring buffer of recent events + live subscribers.

    Slow subscribers never block publishing: their queues overflow and the
    events are dropped (counted), which is the standard backpressure for
    event feeds.
    """

    def __init__(self, size: int = 200):
        self._size = size
        self._events = []
        self._subscribers = set()
        self._dropped = 0
        self._lock = threading.Lock()

    def publish(self, event_type: str, payload: Optional[dict]) -> None:
        event = {"type": event_type, "payload": payload, "at": time.time()}
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._size:
                self._events = self._events[-self._size:]
            for queue_ in list(self._subscribers):
                try:
                    queue_.put_nowait(event)
                except Exception:
                    self._dropped += 1

    def subscribe(self):
        import queue

        queue_ = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(queue_)
        return queue_

    def unsubscribe(self, queue_) -> None:
        with self._lock:
            self._subscribers.discard(queue_)

    def recent(self) -> list:
        with self._lock:
            return list(self._events)

    def stats(self) -> dict:
        with self._lock:
            return {"buffered": len(self._events), "subscribers": len(self._subscribers),
                    "dropped": self._dropped}


def _notify(write, event_type, payload) -> None:
    line = json.dumps({"jsonrpc": "2.0", "method": "event",
                       "params": {"type": event_type, "payload": payload}})
    write(line.encode("utf-8") + b"\n")


def _install_sink(router: Router, ring: EventRing,
                  extra: Optional[Callable[[str, Optional[dict]], None]] = None) -> None:
    """Wire the router's sessions to the ring (+ wire notifications)."""

    def route_event(event_type, payload):
        ring.publish(event_type, payload)
        if extra is not None:
            extra(event_type, payload)

    router.set_event_sink(route_event)
    router._event_ring = ring


def serve_stdio(router: Router, stdin, stdout, stop_event: Optional[threading.Event] = None) -> None:
    """Run the JSON-RPC loop over binary file-like streams until EOF.

    Events are pushed to stdout as JSON-RPC notifications and mirrored into
    the router's event ring (so HTTP subscribers can replay them).
    """
    ring = EventRing()
    _install_sink(router, ring, extra=lambda t, p: _notify(stdout.write, t, p))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            line = stdin.readline()
        except Exception as exc:
            log("stdio read error: %s" % exc)
            break
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line.strip():
            continue
        response = None
        try:
            request = load_request(line)
            response = router.handle(request)
        except ProtocolError as exc:
            # Parse/invalid-request failures: the id is unknown, so reply
            # with a null id (JSON-RPC spec) - but only when we could not
            # parse; notifications are never answered.
            request_id = _extract_id(line)
            if request_id is None and line.strip().startswith(b"{"):
                response = {"jsonrpc": "2.0", "id": None, "error": exc.as_jsonrpc()}
            elif request_id is not None:
                response = {"jsonrpc": "2.0", "id": request_id, "error": exc.as_jsonrpc()}
        except Exception as exc:
            log("unhandled error: %s\n%s" % (exc, _stack()))
            request_id = _extract_id(line)
            if request_id is not None:
                response = {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32000, "message": "internal error: %s" % exc}}
        if response is not None:
            try:
                stdout.write(json.dumps(response).encode("utf-8") + b"\n")
                stdout.flush()
            except Exception as exc:
                log("stdio write error: %s" % exc)
                break


def _extract_id(line: bytes):
    try:
        request = json.loads(line.decode("utf-8"))
        return request.get("id")
    except Exception:
        return None


def _stack() -> str:
    import traceback

    return traceback.format_exc()


# ---------------------------------------------------------------- http

def make_http_server(router: Router, host: str, port: int):
    """Build (but do not start) the HTTP server. Returns the server object so
    callers can run it in a thread (tests) or serve it forever (cli)."""
    ring = EventRing()
    _install_sink(router, ring)

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "computer-control/0.1"

        def log_message(self, fmt, *args):
            log("http: %s" % (fmt % args))

        def _read_body(self):
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or 0)
            except (TypeError, ValueError):
                return None
            if length < 0 or length > 10 * 1024 * 1024:
                return None
            return self.rfile.read(length) if length else b""

        def do_POST(self):
            if self.path != "/rpc":
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            body = self._read_body()
            if body is None:
                self._json_response(400, {"jsonrpc": "2.0", "id": None,
                                          "error": {"code": -32600, "message": "invalid body"}})
                return
            try:
                request = load_request(body)
            except ProtocolError as exc:
                self._json_response(400, {"jsonrpc": "2.0", "id": None, "error": exc.as_jsonrpc()})
                return
            try:
                response = router.handle(request)
            except ProtocolError as exc:
                response = {"jsonrpc": "2.0", "id": request.get("id"), "error": exc.as_jsonrpc()}
            except Exception as exc:
                log("http handler error: %s" % exc)
                response = {"jsonrpc": "2.0", "id": request.get("id"),
                            "error": {"code": -32000, "message": "internal error: %s" % exc}}
            if response is None:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json_response(200, response)

        def do_GET(self):
            if self.path == "/health":
                self._json_response(200, {"ok": True, "state": "running", "time": time.time(),
                                          "events": ring.stats()})
            elif self.path == "/events":
                self._stream_events()
            else:
                self._json_response(404, {"ok": False, "error": "not found"})

        def _json_response(self, status: int, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream_events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            import queue as queue_module

            # subscribe first, then replay, then skip anything duplicated by
            # the live feed (events are timestamped by the ring)
            queue_ = ring.subscribe()
            try:
                recent = ring.recent()
                last_at = recent[-1]["at"] if recent else 0.0
                for event in recent:
                    self._sse(event)
                keepalive = time.monotonic()
                while True:
                    try:
                        event = queue_.get(timeout=5)
                        if event["at"] > last_at:
                            self._sse(event)
                    except queue_module.Empty:
                        if time.monotonic() - keepalive > 15:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            keepalive = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                ring.unsubscribe(queue_)

        def _sse(self, event):
            data = json.dumps({"type": event["type"], "payload": event["payload"], "at": event["at"]})
            self.wfile.write(("event: message\ndata: %s\n\n" % data).encode("utf-8"))
            self.wfile.flush()

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def run_http(router: Router, host: str, port: int) -> None:
    """Blocking HTTP server. Events go through the ring; subscribers get an
    SSE stream with a recent-event replay."""
    server = make_http_server(router, host, port)
    actual_host, actual_port = server.server_address[:2]
    log("HTTP server on http://%s:%d" % (actual_host, actual_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
