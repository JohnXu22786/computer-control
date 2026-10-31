"""Focused tests for Windows SendInput event construction."""

import unittest
from PIL import Image
from types import SimpleNamespace
from unittest.mock import Mock, patch

from computer_control.drivers.base import DriverError
import computer_control.drivers.windows as windows_module
from computer_control.drivers.windows import (
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
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


class TestWindowsKeyCombo(unittest.TestCase):
    def test_key_up_failure_retries_and_releases_remaining_keys(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        released = []
        release_error = DriverError("release failed")

        def key_up(vk, extended=False):
            released.append(vk)
            if vk == 0x10 and released.count(vk) == 1:
                raise release_error

        driver._key_down = Mock()
        driver._key_up = key_up

        with patch.object(windows_module.time, "sleep"):
            with self.assertRaises(DriverError) as raised:
                driver.key_combo(["ctrl", "shift", "a"])

        self.assertIs(raised.exception, release_error)
        self.assertEqual(released, [ord("A"), 0x10, 0x11, 0x10])


class TestWindowsInputRelease(unittest.TestCase):
    def test_key_press_retries_one_shot_key_up_failure(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        release_error = DriverError("key release failed")
        releases = []

        driver._key_down = Mock()

        def key_up(vk, extended=False):
            releases.append((vk, extended))
            if len(releases) == 1:
                raise release_error

        driver._key_up = key_up

        with patch.object(windows_module.time, "sleep"):
            with self.assertRaises(DriverError) as raised:
                driver.key_press("a")

        self.assertIs(raised.exception, release_error)
        self.assertEqual(len(releases), 2)
        driver._key_down.assert_called_once_with(ord("A"), False)

    def test_pointer_click_retries_one_shot_mouse_up_failure(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        release_error = DriverError("mouse release failed")
        sent_flags = []

        def send_mouse(flags, **kwargs):
            sent_flags.append(flags)
            if flags == MOUSEEVENTF_LEFTUP and sent_flags.count(flags) == 1:
                raise release_error

        driver._send_mouse = send_mouse

        with patch.object(windows_module.time, "sleep"):
            with self.assertRaises(DriverError) as raised:
                driver.pointer_click("left")

        self.assertIs(raised.exception, release_error)
        self.assertEqual(sent_flags, [MOUSEEVENTF_LEFTDOWN,
                                      MOUSEEVENTF_LEFTUP,
                                      MOUSEEVENTF_LEFTUP])

    def test_pointer_drag_failure_releases_button_and_preserves_action_error(self):
        driver = WindowsDriver.__new__(WindowsDriver)
        action_error = DriverError("pointer move failed")
        cleanup_error = DriverError("mouse release failed")
        sent_flags = []
        moves = []

        def pointer_move(*args, **kwargs):
            moves.append(args)
            if len(moves) == 2:
                raise action_error

        def send_mouse(flags, **kwargs):
            sent_flags.append(flags)
            if flags == MOUSEEVENTF_LEFTUP:
                raise cleanup_error

        driver.pointer_move = pointer_move
        driver._send_mouse = send_mouse

        with patch.object(windows_module.time, "sleep"):
            with self.assertRaises(DriverError) as raised:
                driver.pointer_drag(1, 2, 3, 4)

        self.assertIs(raised.exception, action_error)
        self.assertEqual(sent_flags, [MOUSEEVENTF_LEFTDOWN,
                                      MOUSEEVENTF_LEFTUP,
                                      MOUSEEVENTF_LEFTUP])


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
