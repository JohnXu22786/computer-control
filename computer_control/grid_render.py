"""Render a coordinate grid over a final capture-relative image."""

from PIL import Image, ImageDraw, ImageFont

from .grid_geometry import grid_interval, grid_ticks


def draw_grid(image: Image.Image) -> tuple:
    """Return an annotated copy and axis intervals in capture action units.

    Desktop capture action units are pixels of the final image. The input is
    left untouched so callers may retain the original capture. Raises
    ValueError when the image is too small for readable annotations.
    """
    width, height = image.size
    intervals = {"x": max(20, grid_interval(width)), "y": max(20, grid_interval(height))}
    # A label band and readable content cannot coexist on very small images.
    if min(width, height) < 40 or max(width, height) < 64:
        raise ValueError("Image is too small for a readable coordinate grid")
    x_ticks = grid_ticks(width, width, intervals["x"])
    y_ticks = grid_ticks(height, height, intervals["y"])
    # Alternating light and dark dashes remain visible over either background
    # without masking whole rows or columns of captured content.
    result = image.copy()
    draw = ImageDraw.Draw(result)
    light, dark = (230, 230, 230), (25, 25, 25)
    if image.mode == "1":
        light, dark = 255, 0
    elif image.mode == "L":
        light, dark = 230, 25
    for _, x in x_ticks:
        for y in range(0, height, 8):
            draw.line((x, y, x, min(y + 3, height - 1)), fill=light)
            draw.line((x, min(y + 4, height - 1), x, min(y + 7, height - 1)), fill=dark)
    for _, y in y_ticks:
        for x in range(0, width, 8):
            draw.line((x, y, min(x + 3, width - 1), y), fill=light)
            draw.line((min(x + 4, width - 1), y, min(x + 7, width - 1), y), fill=dark)

    font = ImageFont.load_default()
    # Limit bands on small captures so they never consume the whole image.
    band_height = min(14, height // 4)
    band_width = min(32, width // 4)
    if band_height and band_width:
        def draw_label(position, text):
            box = draw.textbbox(position, text, font=font)
            draw.rectangle(box, fill=light)
            draw.text(position, text, font=font, fill=dark)

        draw_label((1, 0), "0,0")
        next_x = band_width
        for value, x in x_ticks[1:]:
            label = str(value)
            box = draw.textbbox((0, 0), label, font=font)
            label_width = box[2] - box[0]
            draw.line((x, max(0, band_height - 3), x, band_height - 1), fill=dark)
            if x >= next_x and x + label_width < width:
                draw_label((x, 0), label)
                next_x = x + label_width + 3
        next_y = max(band_height, 12)
        for value, y in y_ticks[1:]:
            draw.line((max(0, band_width - 3), y, band_width - 1, y), fill=dark)
            if y >= next_y and y + 11 < height and draw.textbbox((1, y), str(value), font=font)[2] < width:
                draw_label((1, y), str(value))
                next_y = y + 12
    return result, intervals
