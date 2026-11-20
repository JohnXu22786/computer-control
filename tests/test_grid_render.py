"""Image annotation in the capture-relative action frame."""
import unittest

from PIL import Image

from computer_control.grid_render import draw_grid


class TestGridRender(unittest.TestCase):
    def test_grid_lines_follow_image_coordinates(self):
        original = Image.new("RGB", (200, 100), (120, 120, 120))
        result, interval = draw_grid(original)
        self.assertEqual(interval, {"x": 20, "y": 20})
        self.assertEqual(result.size, original.size)
        self.assertNotEqual(result.getpixel((40, 53)), original.getpixel((40, 53)))
        self.assertNotEqual(result.getpixel((53, 40)), original.getpixel((53, 40)))
        self.assertEqual(result.getpixel((53, 53)), original.getpixel((53, 53)))
        self.assertEqual(original.getpixel((40, 53)), (120, 120, 120))

    def test_grayscale_image(self):
        result, interval = draw_grid(Image.new("L", (200, 100), 128))
        self.assertEqual(result.size, (200, 100))
        self.assertEqual(result.mode, "L")
        self.assertGreaterEqual(min(interval.values()), 1)

    def test_tiny_images_raise_explicit_error(self):
        for size in [(1, 1), (2, 7), (7, 2), (16, 16), (15, 64), (64, 15), (63, 63), (64, 16), (16, 64), (39, 64)]:
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "too small"):
                    draw_grid(Image.new("L", size, 128))

    def test_binary_grayscale_keeps_contrasting_marks(self):
        result, _ = draw_grid(Image.new("1", (200, 100), 1))
        self.assertEqual(result.mode, "1")
        self.assertEqual(result.getpixel((40, 53)), 0)
