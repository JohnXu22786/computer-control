# Desktop capture and coordinate mapping

This describes the current desktop route. Coordinates increase rightward and downward. Physical coordinates are pixels in the virtual desktop bounding rectangle; its origin can be negative when a monitor lies left of or above the primary monitor. The rectangle can include gaps between monitors.

The session creates a **desktop canvas** over that rectangle using the configured capture width. Its height is the physical aspect ratio rounded to an integer (at least one). Its uniform scale is physical desktop width divided by canvas width, in physical pixels per canvas unit. The `region` argument to every `screen.capture` is in this desktop canvas, even after a previous crop or scaled capture; it is not relative to the previous image. Omit `region` for the entire desktop. Region `x` and `y` must be nonnegative and width and height positive.

For a region, the engine maps its edges through the desktop canvas scale and physical origin, intersects them with the physical desktop rectangle, then rounds left and top down and right and bottom up to physical pixel boundaries. A partially overlapping region captures the intersection. A region with no intersection returns `invalid_arguments`; it does **not** fall back to the full desktop. A failed capture leaves the previous action surface in place.

The requested output width is the desktop canvas width multiplied by `scale` (default 1), rounded to an integer and at least one. This is true for a crop too: `scale` does not multiply the region's width. The output height is the captured physical rectangle's aspect ratio times the output width, rounded and at least one. The configured `capture.max_area` can reduce the output dimensions; in an extreme aspect ratio the height can be limited independently. The driver may reject a capture if it cannot produce the required dimensions. Use the returned dimensions rather than predicting them.

A successful capture replaces the **action surface** with the returned image dimensions and captured physical rectangle. `canvas.display_width_px` and `canvas.display_height_px` match the image, `canvas.physical` gives that rectangle's `x`, `y`, `width`, and `height`, and `canvas.scale` is physical width divided by image width. Pointer coordinates refer to this latest action surface, including after a crop. Before the first successful capture they refer to the desktop canvas. A later successful capture changes their interpretation; there is no stale-frame rejection.

For a pointer point `(x, y)`, each coordinate is clamped to `[0, image dimension - 1]`, then mapped to `(physical.x + x * canvas.scale, physical.y + y * canvas.scale)`. The same width-derived scale is used on both axes, even when output-height rounding or the area cap means the image's height ratio differs. The Windows driver subsequently normalizes physical coordinates for the virtual desktop and rounds to integer input units. Click and scroll without coordinates use the current pointer position. Capture regions and pointer coordinates therefore use different frames after a crop or scaling.

Examples (assuming no area limit affects the output):

- A physical desktop `(-1920, 0, 3840, 1080)` with desktop canvas width 1920 has scale 2 and height 540. A full capture at `scale: 1` produces 1920 × 540; pointer `(0, 0)` maps to physical `(-1920, 0)`, and `(960, 270)` maps to `(0, 540)`.
- On that same desktop, `region: {x: 1000, y: 50, width: 400, height: 200}` maps to physical bounds `(80, 100, 800, 400)`. With `scale: 0.5`, the returned image is 960 × 480, its action origin maps to `(80, 100)`, and pointer `(480, 240)` maps to `(480, 300)`. The next `region` is still measured against the 1920 × 540 desktop canvas.
- A region `{x: 1900, y: 500, width: 100, height: 100}` intersects only the lower-right portion of that desktop, producing physical bounds `(1880, 1000, 40, 80)` before output resizing. A region starting at `x: 1920` has no intersection and fails.

The action surface is a capture-time mapping, not a promise that the desktop or a window has remained stationary. Capture again before acting when the display layout or target has changed.
