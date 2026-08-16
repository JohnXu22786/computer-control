"""Driver abstraction: the platform execution layer.

A driver owns the raw hardware interfaces of one platform. The engine never
touches platform APIs directly - it talks to a driver through this contract.

Coordinates are ALWAYS physical desktop pixels here. The engine converts
model-space coordinates before calling in. Screenshots come back encoded
(PNG/JPEG bytes) so the engine never needs an imaging library.

Windows is the fully implemented platform. macOS/Linux drivers implement the
same contract against their native APIs (CGEvent/IOHIDEvent, X11/wayland
toolkits, AT-SPI/NSAccessibility) - the shape of this interface is the
porting contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


class DriverError(Exception):
    """A driver-level failure surfaced to callers as a tool error.

    ``code`` maps to the plugin's error vocabulary (driver_failed,
    backend_unavailable, element_stale, ...).
    """

    def __init__(self, message: str, code: str = "driver_failed", data: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


@dataclass
class CapturePayload:
    bytes: bytes
    width: int
    height: int
    format: str  # "png" | "jpeg"


class BaseDriver(ABC):
    """Contract every platform driver implements."""

    platform_name = "abstract"

    capabilities = {
        "capture": False,
        "pointer": False,
        "keys": False,
        "a11y": False,
    }

    # ------------------------------------------------------------- capture

    @abstractmethod
    def capture(self, bbox: tuple, canvas_width: int, format: str = "png",
                quality: int = 85, grayscale: bool = False) -> CapturePayload:
        """Capture the physical desktop bbox (left, top, right, bottom),
        scale it so its width is ``canvas_width``, encode it and return."""

    # ------------------------------------------------------------- pointer

    def pointer_move(self, x: float, y: float, steps: int = 1) -> None:
        raise DriverError("pointer control is not implemented for %s" % self.platform_name, "backend_unavailable")

    def pointer_click(self, button: str, times: int = 1, hold_ms: int = 0) -> None:
        raise DriverError("pointer control is not implemented for %s" % self.platform_name, "backend_unavailable")

    def pointer_drag(self, fx: float, fy: float, tx: float, ty: float,
                     button: str = "left", steps: int = 24, hold_ms: int = 0) -> None:
        raise DriverError("pointer control is not implemented for %s" % self.platform_name, "backend_unavailable")

    def pointer_scroll(self, axis: str, amount: float) -> None:
        raise DriverError("pointer control is not implemented for %s" % self.platform_name, "backend_unavailable")

    # ------------------------------------------------------------- keyboard

    def key_press(self, key_name: str) -> None:
        raise DriverError("keyboard control is not implemented for %s" % self.platform_name, "backend_unavailable")

    def key_combo(self, key_names: List[str]) -> None:
        raise DriverError("keyboard control is not implemented for %s" % self.platform_name, "backend_unavailable")

    def key_type(self, text: str, interval_ms: int = 0) -> None:
        raise DriverError("keyboard control is not implemented for %s" % self.platform_name, "backend_unavailable")

    # ------------------------------------------------------- accessibility

    def a11y_snapshot(self, options: dict) -> dict:
        raise DriverError("accessibility backend is not available for %s" % self.platform_name, "backend_unavailable")

    def a11y_activate(self, node_id: int, method: str = "auto") -> dict:
        raise DriverError("accessibility backend is not available for %s" % self.platform_name, "backend_unavailable")

    def a11y_set_text(self, node_id: int, text: str) -> dict:
        raise DriverError("accessibility backend is not available for %s" % self.platform_name, "backend_unavailable")

    # ------------------------------------------------------------- hotkey

    def hotkey_probe(self, key_names: List[str]) -> bool:
        """True when every named key is currently held down."""
        return False

    # ----------------------------------------------------------- lifecycle

    def close(self) -> None:
        pass
