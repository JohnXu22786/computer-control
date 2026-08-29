"""Surface geometry: the mapping between the model-space canvas and the
physical virtual desktop.

The model never sees raw physical pixels. Every screenshot it receives is
scaled to a canonical canvas (width anchored, aspect preserved), and every
coordinate it returns lives in that canvas. This module owns that contract.

  model point (mx, my)  --to_physical-->  physical point (px, py)
  physical bbox         --to_model------>  model bbox
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in arbitrary (model or physical) units."""

    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_bbox(cls, bbox):
        """Build from a (left, top, right, bottom) tuple."""
        left, top, right, bottom = bbox
        return cls(left, top, right - left, bottom - top)

    def as_bbox(self) -> Tuple[int, int, int, int]:
        """(left, top, right, bottom) with outward rounding so the rect is fully covered."""
        return (
            int(math.floor(self.x)),
            int(math.floor(self.y)),
            int(math.ceil(self.x + self.width)),
            int(math.ceil(self.y + self.height)),
        )

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def clamp_point(self, x: float, y: float) -> Tuple[int, int]:
        return (
            int(round(min(max(x, self.x), self.x + self.width - 1))),
            int(round(min(max(y, self.y), self.y + self.height - 1))),
        )


@dataclass(frozen=True)
class Surface:
    """A canonical canvas laid over the physical virtual desktop.

    Scaling is uniform: one model pixel always maps to ``scale`` physical
    pixels on every axis, which keeps the model's mental image consistent.
    """

    canvas_width: int
    canvas_height: int
    physical_x: int
    physical_y: int
    physical_width: int
    physical_height: int

    @classmethod
    def from_physical(cls, canvas_width: int, x: int, y: int, w: int, h: int) -> "Surface":
        """Create a canvas of ``canvas_width`` model pixels over the physical
        rect (x, y, w, h). The canvas height follows the physical aspect ratio."""
        if w <= 0 or h <= 0:
            raise ValueError("physical desktop must have positive size")
        if canvas_width <= 0:
            raise ValueError("canvas width must be positive")
        scale = w / canvas_width
        canvas_height = max(1, int(round(h / scale)))
        return cls(canvas_width, canvas_height, x, y, w, h)

    @property
    def scale(self) -> float:
        """Physical pixels per model pixel (uniform)."""
        return self.physical_width / self.canvas_width

    def to_physical(self, mx: float, my: float, clamp: bool = True) -> Tuple[float, float]:
        """Map a model-space point to physical desktop pixels."""
        if clamp:
            mx, my = self._clamp_model(mx, my)
        return (
            self.physical_x + mx * self.scale,
            self.physical_y + my * self.scale,
        )

    def to_model(self, px: float, py: float) -> Tuple[float, float]:
        """Map a physical desktop pixel back to model space."""
        return (
            (px - self.physical_x) / self.scale,
            (py - self.physical_y) / self.scale,
        )

    def clamp_point(self, mx: float, my: float) -> Tuple[int, int]:
        cx, cy = self._clamp_model(mx, my)
        return (int(round(cx)), int(round(cy)))

    def _clamp_model(self, mx: float, my: float) -> Tuple[float, float]:
        return (
            min(max(float(mx), 0.0), float(self.canvas_width - 1)),
            min(max(float(my), 0.0), float(self.canvas_height - 1)),
        )

    def region_to_physical_bbox(self, region: Optional[dict]) -> Tuple[int, int, int, int]:
        """Convert a model-space region {x, y, width, height} to a physical
        (left, top, right, bottom) capture bbox. ``None`` means the full
        desktop. The bbox is clamped to the physical desktop, so out-of-canvas
        regions degrade to a partial capture instead of an out-of-bounds one."""
        if region is None:
            return (
                self.physical_x,
                self.physical_y,
                self.physical_x + self.physical_width,
                self.physical_y + self.physical_height,
            )
        left = self.physical_x + region["x"] * self.scale
        top = self.physical_y + region["y"] * self.scale
        right = self.physical_x + (region["x"] + region["width"]) * self.scale
        bottom = self.physical_y + (region["y"] + region["height"]) * self.scale
        left, top = max(left, self.physical_x), max(top, self.physical_y)
        right, bottom = min(right, self.physical_x + self.physical_width), min(bottom, self.physical_y + self.physical_height)
        if right <= left or bottom <= top:
            # region entirely off the desktop: fall back to the full desktop
            return (
                self.physical_x,
                self.physical_y,
                self.physical_x + self.physical_width,
                self.physical_y + self.physical_height,
            )
        return (
            int(math.floor(left)),
            int(math.floor(top)),
            int(math.ceil(right)),
            int(math.ceil(bottom)),
        )

    def as_dict(self) -> dict:
        return {
            "display_width_px": self.canvas_width,
            "display_height_px": self.canvas_height,
            "physical": {
                "x": self.physical_x,
                "y": self.physical_y,
                "width": self.physical_width,
                "height": self.physical_height,
            },
            "scale": self.scale,
        }
