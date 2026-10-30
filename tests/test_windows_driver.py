"""Focused tests for Windows SendInput event construction."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
