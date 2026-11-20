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

    def test_grayscale_and_tiny_images(self):
        for size in [(200, 100), (1, 1), (2, 7), (7, 2), (16, 16)]:
            with self.subTest(size=size):
                original = Image.new("L", size, 128)
                result, interval = draw_grid(original)
                self.assertEqual(result.size, size)
                self.assertEqual(result.mode, "L")
                self.assertGreaterEqual(min(interval.values()), 1)
                if min(size) < 16 or max(size) < 64:
                    self.assertEqual(result.tobytes(), original.tobytes())

    def test_binary_grayscale_keeps_contrasting_marks(self):
        result, _ = draw_grid(Image.new("1", (200, 100), 1))
        self.assertEqual(result.mode, "1")
        self.assertEqual(result.getpixel((40, 53)), 0)
