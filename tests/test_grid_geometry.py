"""Grid tick calculations in the latest capture-relative action frame."""

import unittest

from computer_control.grid_geometry import grid_interval, grid_ticks


class TestGridGeometry(unittest.TestCase):
    def test_interval_uses_readable_125_steps(self):
        for extent, expected in [(1, 1), (60, 10), (100, 10), (1920, 200), (3840, 500)]:
            with self.subTest(extent=extent):
                self.assertEqual(grid_interval(extent), expected)

    def test_ticks_map_capture_coordinates_to_final_pixels(self):
        self.assertEqual(grid_ticks(100, 51, 20), [(0, 0), (20, 10), (40, 20), (60, 31), (80, 41)])
        self.assertEqual(grid_ticks(7, 3, 2), [(0, 0), (2, 1), (4, 2)])

    def test_degenerate_dimensions_and_interval(self):
        for extent, pixels, interval in [(0, 10, 1), (10, 0, 1), (-1, 10, 1), (10, 10, 0)]:
            self.assertEqual(grid_ticks(extent, pixels, interval), [])
        self.assertEqual(grid_interval(0), 1)
