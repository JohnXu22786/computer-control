"""Recording driver used for dry-runs and tests.

Nothing touches the real hardware: every action is logged, and captures
produce tiny synthetic PNGs (encoded with the standard library only, so the
whole test suite runs without optional dependencies). It also emulates an
accessibility backend with a small fake tree so the semantic actions can be
exercised end to end.
"""

from __future__ import annotations

import struct
import threading
import time
import zlib

from computer_control.a11y.tree import summarize
from computer_control.drivers.base import BaseDriver, CapturePayload, DriverError

def make_png_1x1() -> bytes:
    """A tiny valid PNG (1x1 red pixel) built with the standard library only."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw = b"\x00" + b"\xff\x00\x00"  # filter: none; pixel: red
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


_PNG_1PX = make_png_1x1()

# A valid 1x1 red JPEG (634 bytes), so dry-run captures also carry the
# correct image/* media type on the data URL.
_JPEG_1PX = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141515150c0f171816141812141514ffdb0043010304040504"
    "0509050509140d0b0d1414141414141414141414141414141414141414141414141414141414141414141414141414141414"
    "14141414141414141414141414ffc00011080001000103012200021101031101ffc4001f00000105010101010101000000"
    "00000000000102030405060708090a0bffc400b5100002010303020403050504040000017d010203000411051221314106"
    "13516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a43444546"
    "4748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5"
    "a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7"
    "f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b5110002010204040304"
    "0705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e"
    "125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a"
    "82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5"
    "d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda000c03010002110311003f00f9d28a28afc30ff54c"
    "ffd9"
)


class NullDriver(BaseDriver):
    """Records every call into a thread-safe log. ``enable_a11y`` toggles the
    fake accessibility backend so capability advertisement can be tested."""

    platform_name = "dry-run"

    def __init__(self, enable_a11y: bool = False):
        self._log = []
        self._lock = threading.Lock()
        self._enable_a11y = enable_a11y
        self._nodes = {}
        self._fake_tree = self._make_fake_tree()
        self.capabilities = {
            "capture": True,
            "pointer": True,
            "keys": True,
            "a11y": enable_a11y,
        }

    # ------------------------------------------------------------ recording

    def log(self):
        with self._lock:
            return list(self._log)

    def _record(self, **entry):
        with self._lock:
            self._log.append(entry)

    # ------------------------------------------------------------- capture

    def capture(self, bbox, canvas_width, format="png", quality=85, grayscale=False):
        left, top, right, bottom = bbox
        width = max(1, int(canvas_width))
        height = max(1, int(round((bottom - top) * width / max(right - left, 1))))
        self._record(kind="capture", bbox=(left, top, right, bottom),
                     canvas_width=width, format=format, quality=quality, grayscale=grayscale)
        if format == "jpeg":
            return CapturePayload(_JPEG_1PX, width, height, "jpeg")
        return CapturePayload(_PNG_1PX, width, height, "png")

    # ------------------------------------------------------------- pointer

    def pointer_move(self, x, y, steps=1):
        self._record(kind="move", position=(float(x), float(y)), steps=steps)

    def pointer_click(self, button, times=1, hold_ms=0):
        self._record(kind="click", button=button, times=times, hold_ms=hold_ms)

    def pointer_drag(self, fx, fy, tx, ty, button="left", steps=24, hold_ms=0):
        self._record(kind="drag", origin=(float(fx), float(fy)), target=(float(tx), float(ty)),
                     button=button, steps=steps, hold_ms=hold_ms)

    def pointer_scroll(self, axis, amount):
        self._record(kind="scroll", axis=axis, amount=float(amount))

    # ------------------------------------------------------------- keyboard

    def key_press(self, key_name):
        self._record(kind="press", key=key_name)

    def key_combo(self, key_names):
        self._record(kind="combo", keys=list(key_names))

    def key_type(self, text, interval_ms=0):
        self._record(kind="type", text=text, interval_ms=interval_ms)

    # ------------------------------------------------------- accessibility

    def a11y_snapshot(self, options):
        if not self._enable_a11y:
            raise DriverError("no a11y backend", "backend_unavailable")
        tree, count, truncated = summarize(self._fake_tree, preset=options.get("level", "standard"),
                                           depth=options.get("depth"), max_nodes=options.get("max_nodes"),
                                           include_rects=options.get("include_rects", True),
                                           name_cap=options.get("max_name_len"))
        self._nodes = {n["id"]: n for n in _flatten(tree)}
        return {"tree": tree, "node_count": count, "truncated": truncated,
                "generated_at": time.time()}

    def a11y_activate(self, node_id, method="auto"):
        if not self._enable_a11y:
            raise DriverError("no a11y backend", "backend_unavailable")
        node = self._nodes.get(node_id)
        if node is None:
            raise DriverError("element is not part of the current snapshot", "unknown_node")
        used = "pointer" if method == "pointer" else "invoke"
        rect = node.get("rect")
        position = None
        if rect is not None:
            position = {"x": (rect[0] + rect[2]) / 2.0, "y": (rect[1] + rect[3]) / 2.0}
        self._record(kind="activate", node_id=node_id, method=used, position=position)
        return {"node_id": node_id, "method_used": used, "position": position}

    def a11y_set_text(self, node_id, text):
        if not self._enable_a11y:
            raise DriverError("no a11y backend", "backend_unavailable")
        node = self._nodes.get(node_id)
        if node is None:
            raise DriverError("element is not part of the current snapshot", "unknown_node")
        self._record(kind="a11y_input", node_id=node_id, text=text)
        return {"node_id": node_id, "method_used": "value", "chars": len(text)}

    # ------------------------------------------------------------- hotkey & info

    def desktop_info(self) -> dict:
        return {
            "platform": self.platform_name,
            "virtual_screen": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "dpi_mode": "per_monitor_v2",
            "capture_backend": "synthetic",
        }

    def hotkey_probe(self, key_names: list) -> bool:
        with self._lock:
            held = getattr(self, "_held_keys", None)
            if held:
                return set(key_names).issubset(held)
        return False

    def simulate_hotkey_down(self, key_names: list) -> None:
        with self._lock:
            self._held_keys = set(key_names)

    def simulate_hotkey_up(self) -> None:
        with self._lock:
            self._held_keys = set()

    def close(self) -> None:
        with self._lock:
            self._nodes.clear()
            self._held_keys = set()

    # -------------------------------------------------------------- helpers

    def _make_fake_tree(self):
        return {
            "id": 1, "role": "window", "name": "Fake Application", "rect": [0, 0, 800, 600],
            "children": [
                {"id": 2, "role": "edit", "name": "Search box", "rect": [10, 10, 200, 28]},
                {"id": 3, "role": "button", "name": "Go", "rect": [220, 10, 60, 28]},
                {"id": 4, "role": "list", "name": "Results",
                 "children": [
                     {"id": 5, "role": "listitem", "name": "Result one", "rect": [10, 50, 300, 20]},
                     {"id": 6, "role": "listitem", "name": "Result two", "rect": [10, 72, 300, 20]},
                 ]},
            ],
        }


def _flatten(node):
    out = [node]
    for child in node.get("children", []):
        out.extend(_flatten(child))
    return out
