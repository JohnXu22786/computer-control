"""Unit tests for surface geometry (model-space canvas <-> physical pixels)."""

import unittest

from computer_control.geometry import Rect, Surface


class TestRect(unittest.TestCase):
    def test_as_bbox_rounds_edges(self):
        self.assertEqual(Rect(0.4, 0.4, 1.2, 1.2).as_bbox(), (0, 0, 2, 2))

    def test_as_bbox_negative_origin(self):
        self.assertEqual(Rect(-10.2, -5.6, 4.0, 3.0).as_bbox(), (-11, -6, -6, -2))

    def test_contains(self):
        r = Rect(0, 0, 100, 100)
        self.assertTrue(r.contains(50, 50))
        self.assertFalse(r.contains(100, 100))
        self.assertFalse(r.contains(-1, 50))

    def test_clamp_point(self):
        r = Rect(0, 0, 100, 100)
        self.assertEqual(r.clamp_point(-20, 150), (0, 99))
        small = Rect(10, 20, 0.5, 0.5)
        self.assertEqual(small.clamp_point(5, 5), (10, 20))
        self.assertEqual(small.clamp_point(50, 50), (10, 20))

    def test_from_bbox(self):
        r = Rect.from_bbox((10, 20, 110, 220))
        self.assertEqual(r.x, 10.0)
        self.assertEqual(r.y, 20.0)
        self.assertEqual(r.width, 100.0)
        self.assertEqual(r.height, 200.0)


class TestSurface(unittest.TestCase):
    def test_from_physical_uniform_scaling(self):
        s = Surface.from_physical(canvas_width=1920, x=0, y=0, w=3840, h=2160)
        self.assertEqual(s.canvas_width, 1920)
        self.assertEqual(s.canvas_height, 1080)
        self.assertEqual(s.scale, 2.0)

    def test_canvas_height_derived_from_aspect(self):
        s = Surface.from_physical(canvas_width=1280, x=0, y=0, w=1920, h=1200)
        self.assertEqual(s.canvas_height, 800)

    def test_roundtrip_model_physical(self):
        s = Surface.from_physical(1920, 0, 0, 3840, 2160)
        px, py = s.to_physical(100.0, 200.0)
        self.assertAlmostEqual(px, 200.0)
        self.assertAlmostEqual(py, 400.0)
        mx, my = s.to_model(px, py)
        self.assertAlmostEqual(mx, 100.0)
        self.assertAlmostEqual(my, 200.0)

    def test_negative_origin_multimonitor(self):
        s = Surface.from_physical(1920, x=-1920, y=-1080, w=1920, h=1080)
        self.assertEqual(s.to_physical(0, 0), (-1920.0, -1080.0))
        # the far corner clamps to canvas-1, one model pixel before the edge
        self.assertEqual(s.to_physical(1920, 1080), (-1.0, -1.0))
        self.assertEqual(s.to_physical(1920, 1080, clamp=False), (0.0, 0.0))
        self.assertEqual(s.region_to_physical_bbox(None), (-1920, -1080, 0, 0))
        self.assertEqual(
            s.region_to_physical_bbox({"x": 0, "y": 0, "width": 10, "height": 10}),
            (-1920, -1080, -1910, -1070),
        )

    def test_region_to_physical_bbox_scaled(self):
        s = Surface.from_physical(1920, 0, 0, 3840, 2160)
        self.assertEqual(
            s.region_to_physical_bbox({"x": 100, "y": 100, "width": 100, "height": 50}),
            (200, 200, 400, 300),
        )

    def test_clamp_point_in_model_space(self):
        s = Surface.from_physical(1920, 0, 0, 3840, 2160)
        self.assertEqual(s.clamp_point(-5, 5000), (0, 1079))
        self.assertEqual(s.clamp_point(2000, 300), (1919, 300))
        pt = s.clamp_point(10.4, 20.6)
        self.assertEqual(pt, (10, 21))
        self.assertIsInstance(pt[0], int)
        self.assertIsInstance(pt[1], int)

    def test_clamped_physical_conversion(self):
        s = Surface.from_physical(1920, 0, 0, 3840, 2160)
        x, y = s.to_physical(500.0, 500.0, clamp=True)
        self.assertEqual((x, y), (1000.0, 1000.0))
        x, y = s.to_physical(99999.0, -10.0, clamp=True)
        # clamping happens in model space: canvas-1 = 1919 -> physical 3838
        self.assertEqual(x, 3838.0)
        self.assertEqual(y, 0.0)


if __name__ == "__main__":
    unittest.main()
