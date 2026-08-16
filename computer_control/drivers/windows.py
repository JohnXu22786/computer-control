"""Windows driver: real input injection and screen capture.

Execution layer summary
- Pointer/keyboard input goes through SendInput (user32). Keyboard events are
  injected as hardware *scan codes* (layout-independent, accepted by games and
  raw-input apps) with a virtual-key fallback for keys that have no scan
  mapping; text is injected as Unicode key events so any script works without
  touching the clipboard.
- The process is put into per-monitor DPI awareness before anything else so
  that all metrics, captures and injected coordinates agree on *physical*
  pixels. The virtual desktop (which can start at negative coordinates when a
  monitor sits left/above the primary) is the coordinate frame for absolute
  mouse moves.
- Captures use either mss (fast BitBlt, preferred on Windows) or Pillow's
  ImageGrab, scaled to the requested canvas width and encoded in the driver.
- Semantic actions go through the UIA bridge; it degrades to bounding-box
  pixel clicks when patterns are unavailable.
"""

from __future__ import annotations

import ctypes
import io
import sys
import threading
import time
from typing import List, Optional

from computer_control.config import Config
from computer_control.drivers.base import BaseDriver, CapturePayload, DriverError
from computer_control.keys import parse_key, parse_key_full

try:  # Pillow is a hard dependency; fail loudly at import if it is missing.
    from PIL import Image, ImageGrab
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("Pillow is required (pip install -r requirements.txt)") from exc

# ---------------------------------------------------------------- constants

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Keys whose scan codes carry the 0xE0 prefix (navigation cluster, both
# Windows keys, context menu, right control/alt, numpad Enter, numpad /,
# PrtScn...). Numpad Enter shares VK_RETURN with the main Enter key, so it is
# handled separately via parse_key_full.
_EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # page up/down, end, home, arrows
    0x2C, 0x2D, 0x2E,                                # printscreen, insert, delete
    0x5B, 0x5C, 0x5D,                                # lwin, rwin, apps
    0x6F,                                            # numpad /
    0xA3, 0xA5,                                      # rctrl, ralt
})

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", ctypes.c_ulong), ("_u", _INPUT_UNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _mss_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("mss") is not None


class WindowsDriver(BaseDriver):
    """The fully implemented Windows platform driver."""

    platform_name = "windows"

    def __init__(self, cfg: Optional[Config] = None, log=None):
        self._log = log or (lambda message: sys.stderr.write(message + "\n"))
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.MapVirtualKeyW.restype = ctypes.c_uint
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._dpi_mode = self._set_dpi_awareness()
        self._virtual_screen = self._read_virtual_screen()
        self._capture_backend = self._pick_capture_backend(cfg)
        self._uia = None
        self._uia_lock = threading.Lock()
        self._log("[windows-driver] dpi=%s virtual_screen=%s capture=%s"
                  % (self._dpi_mode, self._virtual_screen, self._capture_backend))
        self.capabilities = {
            "capture": True,
            "pointer": True,
            "keys": True,
            "a11y": self._probe_uia(),
        }

    # ------------------------------------------------------------- platform

    def _set_dpi_awareness(self) -> str:
        """Best-effort per-monitor DPI awareness so all Win32 geometry is in
        physical pixels. Called once, before any other Win32 call."""
        try:
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return "per-monitor-v2"
        except Exception:
            pass
        try:
            shcore = ctypes.WinDLL("shcore.dll")
            shcore.SetProcessDpiAwareness(2)
            return "per-monitor"
        except Exception:
            pass
        try:
            self._user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "unaware"

    def _read_virtual_screen(self):
        gsm = self._user32.GetSystemMetrics
        return (
            gsm(SM_XVIRTUALSCREEN),
            gsm(SM_YVIRTUALSCREEN),
            gsm(SM_CXVIRTUALSCREEN),
            gsm(SM_CYVIRTUALSCREEN),
        )

    def desktop_info(self) -> dict:
        return {
            "platform": self.platform_name,
            "dpi_mode": self._dpi_mode,
            "virtual_screen": {
                "x": self._virtual_screen[0],
                "y": self._virtual_screen[1],
                "width": self._virtual_screen[2],
                "height": self._virtual_screen[3],
            },
            "capture_backend": self._capture_backend,
        }

    def _pick_capture_backend(self, cfg: Optional[Config]) -> str:
        wanted = (cfg.capture.backend if cfg else "auto") or "auto"
        if wanted == "mss":
            if not _mss_installed():
                raise DriverError("capture backend 'mss' requested but the mss package is not installed",
                                  code="backend_unavailable")
            return "mss"
        if wanted == "auto":
            return "mss" if _mss_installed() else "pillow"
        return "pillow"

    def _probe_uia(self) -> bool:
        try:
            bridge = self._uia_bridge()
            return bridge.available
        except Exception:
            return False

    def _uia_bridge(self):
        from computer_control.a11y.windows_uia import UiaBridge

        with self._uia_lock:
            if self._uia is None:
                self._uia = UiaBridge()
            return self._uia

    def close(self):
        with self._uia_lock:
            if self._uia is not None:
                try:
                    self._uia.close()
                except Exception:
                    pass
                self._uia = None

    # ------------------------------------------------------------- capture

    def capture(self, bbox: tuple, canvas_width: int, format: str = "png",
                quality: int = 85, grayscale: bool = False) -> CapturePayload:
        left, top, right, bottom = bbox
        width = max(1, int(right - left))
        height = max(1, int(bottom - top))
        try:
            if self._capture_backend == "mss":
                image = self._capture_mss(left, top, width, height)
            else:
                image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except Exception as exc:
            raise DriverError("capture failed: %s" % exc, code="driver_failed")
        return self._encode(image, canvas_width, format, quality, grayscale)

    def _capture_mss(self, left, top, width, height) -> Image.Image:
        import mss

        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _encode(self, image: Image.Image, canvas_width: int, format: str,
                quality: int, grayscale: bool) -> CapturePayload:
        if image.mode != "RGB":
            image = image.convert("RGB")
        if grayscale:
            image = image.convert("L")
        if image.width != canvas_width:
            new_height = max(1, int(round(image.height * canvas_width / image.width)))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image = image.resize((canvas_width, new_height), resampling)
        buffer = io.BytesIO()
        if format == "jpeg":
            image.save(buffer, "JPEG", quality=quality)
        else:
            image.save(buffer, "PNG", optimize=False)
        return CapturePayload(buffer.getvalue(), image.width, image.height, format)

    # ------------------------------------------------------------- pointer

    def pointer_move(self, x: float, y: float, steps: int = 1) -> None:
        steps = max(1, int(steps))
        cx, cy = self.cursor_position()
        for i in range(1, steps + 1):
            t = i / steps
            self._send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                             dx=cx + (x - cx) * t, dy=cy + (y - cy) * t)
            if steps > 1:
                time.sleep(0.008)

    def cursor_position(self):
        point = _POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return (0.0, 0.0)
        return (float(point.x), float(point.y))

    def _send_mouse(self, flags: int, dx=0.0, dy=0.0, data=0) -> None:
        vx, vy, vw, vh = self._virtual_screen
        if flags & MOUSEEVENTF_ABSOLUTE:
            norm_x = int(round((dx - vx) * 65535 / max(vw - 1, 1)))
            norm_y = int(round((dy - vy) * 65535 / max(vh - 1, 1)))
        else:
            norm_x, norm_y = int(dx), int(dy)
        inp = _INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = _MOUSEINPUT(norm_x, norm_y, data & 0xFFFFFFFF, flags, 0, 0)
        self._send_input(inp)

    def pointer_click(self, button: str, times: int = 1, hold_ms: int = 0) -> None:
        down, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
        for i in range(max(1, int(times))):
            self._send_mouse(down)
            if hold_ms > 0:
                time.sleep(hold_ms / 1000.0)
            else:
                time.sleep(0.01)
            self._send_mouse(up)
            if i + 1 < max(1, int(times)):
                time.sleep(0.06)

    def pointer_drag(self, fx: float, fy: float, tx: float, ty: float,
                     button: str = "left", steps: int = 24, hold_ms: int = 0) -> None:
        down, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
        self.pointer_move(fx, fy, steps=4)
        time.sleep(0.02)
        self._send_mouse(down)
        time.sleep(0.02)
        self.pointer_move(tx, ty, steps=max(1, int(steps)))
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        self._send_mouse(up)

    def pointer_scroll(self, axis: str, amount: float) -> None:
        delta = int(round(amount * WHEEL_DELTA))
        flag = MOUSEEVENTF_WHEEL if axis == "vertical" else MOUSEEVENTF_HWHEEL
        # send in chunks to respect the 16-bit signed range
        for _ in range(abs(delta) // 32767 + 1):
            chunk = min(delta, 32767) if delta >= 0 else max(delta, -32767)
            self._send_mouse(flag, data=chunk & 0xFFFFFFFF)
            delta -= chunk
            if delta:
                time.sleep(0.01)

    # ------------------------------------------------------------- keyboard

    def _send_keyboard(self, vk: int = 0, scan: int = 0, flags: int = 0) -> None:
        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = _KEYBDINPUT(vk & 0xFFFF, scan & 0xFFFF, flags, 0, 0)
        self._send_input(inp)

    def _send_input(self, inp: _INPUT) -> None:
        sent = self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if sent != 1:
            raise DriverError("SendInput failed (sent=%d, last_error=%s)"
                              % (sent, ctypes.get_last_error()), code="driver_failed")

    def _vk(self, key_name: str) -> int:
        return parse_key(key_name)

    def _key_down(self, vk: int, extended: bool = False) -> None:
        if vk == 0x13:  # VK_PAUSE has no injectable scan code; use the vk path
            self._send_keyboard(vk=vk, flags=0)
            return
        scan = self._user32.MapVirtualKeyW(vk, 0)
        if scan == 0:
            raise DriverError("key %#x has no scan code mapping" % vk, code="driver_failed")
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if (extended or vk in _EXTENDED_VKS) else 0)
        self._send_keyboard(scan=scan, flags=flags)

    def _key_up(self, vk: int, extended: bool = False) -> None:
        if vk == 0x13:
            self._send_keyboard(vk=vk, flags=KEYEVENTF_KEYUP)
            return
        scan = self._user32.MapVirtualKeyW(vk, 0)
        if scan == 0:
            raise DriverError("key %#x has no scan code mapping" % vk, code="driver_failed")
        flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if (extended or vk in _EXTENDED_VKS) else 0)
        self._send_keyboard(scan=scan, flags=flags)

    def key_press(self, key_name: str) -> None:
        vk, extended = parse_key_full(key_name)
        self._key_down(vk, extended)
        time.sleep(0.02)
        self._key_up(vk, extended)

    def key_combo(self, key_names: List[str]) -> None:
        """Press every key down in order, release in reverse. If any key
        fails mid-way, the keys already pressed are released before the error
        propagates - a stuck modifier is worse than a failed combo."""
        resolved = [parse_key_full(name) for name in key_names]
        pressed = []
        try:
            for vk, extended in resolved:
                self._key_down(vk, extended)
                pressed.append((vk, extended))
                time.sleep(0.015)
        except Exception:
            for vk, extended in reversed(pressed):
                try:
                    self._key_up(vk, extended)
                except Exception:
                    pass
            raise
        for vk, extended in reversed(resolved):
            self._key_up(vk, extended)
            time.sleep(0.015)

    def key_type(self, text: str, interval_ms: int = 0) -> None:
        interval = max(0, int(interval_ms)) / 1000.0
        for char in text:
            if char == "\n":
                self.key_press("enter")
                if interval:
                    time.sleep(interval)
                continue
            if char == "\t":
                self.key_press("tab")
                if interval:
                    time.sleep(interval)
                continue
            self._type_unicode_char(char)
            if interval:
                time.sleep(interval)

    def _type_unicode_char(self, char: str) -> None:
        code = ord(char)
        if code <= 0xFFFF:
            self._unicode_pair(code)
        else:
            value = code - 0x10000
            high = 0xD800 + (value >> 10)
            low = 0xDC00 + (value & 0x3FF)
            self._unicode_pair(high)
            self._unicode_pair(low)

    def _unicode_pair(self, unit: int) -> None:
        self._send_keyboard(scan=unit, flags=KEYEVENTF_UNICODE)
        time.sleep(0.004)
        self._send_keyboard(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)

    # ------------------------------------------------------- accessibility

    def a11y_snapshot(self, options: dict) -> dict:
        return self._uia_bridge().snapshot(options)

    def a11y_activate(self, node_id: int, method: str = "auto") -> dict:
        outcome = self._uia_bridge().activate(node_id, method=method)
        if outcome.get("method_used") == "pointer" and outcome.get("position"):
            position = outcome["position"]
            self.pointer_move(position["x"], position["y"], steps=4)
            time.sleep(0.02)
            self.pointer_click("left", 1)
        return outcome

    def a11y_set_text(self, node_id: int, text: str) -> dict:
        outcome = self._uia_bridge().set_text(node_id, text)
        if outcome.get("method_used") == "pointer_type" and outcome.get("position"):
            position = outcome["position"]
            self.pointer_move(position["x"], position["y"], steps=4)
            time.sleep(0.02)
            self.pointer_click("left", 1)
            time.sleep(0.05)
            self.key_type(text)
        return outcome

    # --------------------------------------------------------------- hotkey

    def hotkey_probe(self, key_names: List[str]) -> bool:
        try:
            vks = [self._vk(name) for name in key_names]
        except Exception:
            return False
        try:
            for vk in vks:
                if not (self._user32.GetAsyncKeyState(vk) & 0x8000):
                    return False
            return True
        except Exception:
            return False
