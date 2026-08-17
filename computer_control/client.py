"""Client: talk to a running plugin server from Python.

Two transports:
- ``StdioClient``: spawns ``python -m computer_control serve`` (or any
  stdio-jsonrpc process) and speaks the line-delimited protocol over pipes.
- ``HttpClient``: JSON-RPC over the HTTP transport, with an SSE event feed.

Both expose ``call(method, params)`` and ``events()``. Used by the demo
script and available to harnesses that want a thin Python driver.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Optional


class ClientError(Exception):
    """Protocol-level failure (transport or JSON-RPC error response)."""

    def __init__(self, message: str, response: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.response = response


class _BaseClient:
    def __init__(self, timeout: float = 600.0):
        self._timeout = timeout
        self._events = queue.Queue()

    def call(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        request = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            request["params"] = params
        response = self._transact(request, timeout)
        if "error" in response:
            raise ClientError("jsonrpc error: %s" % response["error"], response)
        return response["result"]

    def events(self, block: bool = True, timeout: Optional[float] = None):
        return self._events.get(block=block, timeout=timeout)

    def close(self) -> None:
        pass

    def _next_id(self) -> int:
        # The counter is shared across every client instance (class-level), so
        # two clients talking to the same server never issue colliding ids.
        # `self._counter += 1` would create an instance attribute instead and
        # restart every client at 1 - the collision this is meant to prevent.
        with _BaseClient._id_lock:
            _BaseClient._counter += 1
            return _BaseClient._counter

    _counter = 0
    _id_lock = threading.Lock()

    # subclasses implement _transact and event delivery


class StdioClient(_BaseClient):
    """Spawn a stdio server subprocess and speak line-delimited JSON-RPC."""

    def __init__(self, command=None, timeout: float = 600.0):
        super().__init__(timeout)
        command = command or ["python", "-m", "computer_control", "serve"]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # server logs go to the console, not the protocol pipe
        )
        self._responses = {}
        self._pending = {}  # request id -> Event
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            line = self._process.stdout.readline()
            if not line:
                # wake everyone up; the process died
                with self._lock:
                    for event in self._pending.values():
                        event.set()
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if "method" in message:  # notification (event)
                self._events.put(message["params"])
            else:
                request_id = message.get("id")
                with self._lock:
                    self._responses[request_id] = message
                    event = self._pending.pop(request_id, None)
                if event is not None:
                    event.set()

    def _transact(self, request: dict, timeout: Optional[float]) -> dict:
        event = threading.Event()
        with self._lock:
            self._pending[request["id"]] = event
        payload = json.dumps(request).encode("utf-8") + b"\n"
        try:
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                self._pending.pop(request["id"], None)
            raise ClientError("server process is not running: %s" % exc)
        if not event.wait(timeout or self._timeout):
            with self._lock:
                self._pending.pop(request["id"], None)
            raise ClientError("no response within %.1fs" % (timeout or self._timeout))
        with self._lock:
            response = self._responses.pop(request["id"], None)
        if response is None:
            raise ClientError("response for id %s was lost" % request["id"])
        return response

    def close(self) -> None:
        process = self._process
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            process.kill()
        try:
            process.stdout.close()
        except Exception:
            pass


class HttpClient(_BaseClient):
    """JSON-RPC over HTTP, events via SSE."""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 120.0):
        super().__init__(timeout)
        self._base_url = base_url.rstrip("/")
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._sse_thread.start()

    def _transact(self, request: dict, timeout: Optional[float]) -> dict:
        data = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(self._base_url + "/rpc", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = None
            raise ClientError("http %s" % exc.code, body)

    def _sse_loop(self) -> None:
        try:
            with urllib.request.urlopen(self._base_url + "/events", timeout=None) as resp:
                buffer = b""
                while True:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    if buffer.endswith(b"\n\n"):
                        self._dispatch_sse(buffer)
                        buffer = b""
        except Exception:
            pass

    def _dispatch_sse(self, chunk: bytes) -> None:
        for block in chunk.decode("utf-8", errors="replace").split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    try:
                        self._events.put(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
                    break
