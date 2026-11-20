"""Grid intervals and tick positions for a capture-relative action surface.

The desktop action frame starts at the captured image's origin, regardless of
its physical desktop origin. Extents and pixel dimensions describe the final
capture, after cropping, scaling and area limiting.
"""


def grid_interval(extent: int) -> int:
    """Choose a 1/2/5 interval aiming for nine divisions along an axis."""
    if extent <= 1:
        return 1
    power = 1
    while power * 10 <= extent:
        power *= 10
    candidates = set()
    for base in (power // 10, power, power * 10):
        if base:
            candidates.update((base, 2 * base, 5 * base))
    # Compare division counts exactly, preferring the larger step on a tie.
    return min(candidates, key=lambda step: (abs(extent / step - 9), -step))


def grid_ticks(action_extent: int, pixels: int, interval: int) -> list:
    """Return (action coordinate, pixel) pairs inside a final image axis.

    The frame origin is zero. Pixel positions are rounded to nearest, with
    half pixels rounded up; points rounding beyond the last pixel are omitted.
    """
    if action_extent <= 0 or pixels <= 0 or interval <= 0:
        return []
    ticks = []
    for coordinate in range(0, action_extent, interval):
        pixel = (2 * coordinate * pixels + action_extent) // (2 * action_extent)
        if pixel < pixels:
            ticks.append((coordinate, pixel))
    return ticks
