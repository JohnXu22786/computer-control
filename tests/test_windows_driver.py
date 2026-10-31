"""Focused tests for Windows SendInput event construction."""

import unittest
from PIL import Image
from types import SimpleNamespace
from unittest.mock import Mock, patch

import computer_control.drivers.windows as windows_module
from computer_control.drivers.windows import (
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    WindowsDriver,
)


class TestWindowsAbsoluteMouse(unittest.TestCase):
    def test_absolute_move_uses_virtual_desktop_coordinates(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        driver._virtual_screen = (-1920, 0, 3840, 1080)
        sent = []
        driver._send_input = sent.append

        # This physical point is on a secondary monitor left of the primary.
        driver._send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                           dx=-1920, dy=540)

        self.assertEqual(len(sent), 1)
        mouse = sent[0].mi
        self.assertEqual(mouse.dwFlags,
                         MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
        self.assertEqual(mouse.dx, 0)
        self.assertEqual(mouse.dy, round(540 * 65535 / 1079))

    def test_encode_uses_explicit_capture_height(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        payload = driver._encode(Image.new("RGB", (2, 2160)), 1, 1024,
                                 "png", 85, False)

        self.assertEqual((payload.width, payload.height), (1, 1024))


class TestWindowsUiaProbe(unittest.TestCase):
    def test_probe_closes_thread_local_uia_state(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        bridge = SimpleNamespace(available=True, close=Mock())
        driver._uia_bridge = lambda: bridge

        self.assertTrue(driver._probe_uia())

        bridge.close.assert_called_once_with()


class TestWindowsDpiAwareness(unittest.TestCase):
    def test_false_context_result_uses_per_monitor_fallback(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        driver._user32 = SimpleNamespace(
            SetProcessDpiAwarenessContext=Mock(return_value=False),
            SetProcessDPIAware=Mock(return_value=False),
        )
        set_process_dpi_awareness = Mock(return_value=0)
        shcore = SimpleNamespace(SetProcessDpiAwareness=set_process_dpi_awareness)

        with patch.object(windows_module.ctypes, "WinDLL", return_value=shcore, create=True):
            self.assertEqual(driver._set_dpi_awareness(), "per-monitor")

        set_process_dpi_awareness.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
