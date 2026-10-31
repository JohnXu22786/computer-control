"""Regression tests for the optional stop indicator."""

import sys
import threading
import types
import unittest
from unittest import mock

from computer_control.overlay import StopIndicator


class FakeRoot:
    def __init__(self):
        self.initialization_started = threading.Event()
        self.allow_initialization = threading.Event()
        self.poll_scheduled = threading.Event()
        self.withdraw_count = 0
        self.deiconify_count = 0
        self.poll_callback = None

    def __getattr__(self, name):
        if name in {"overrideredirect", "attributes", "lift", "destroy", "update_idletasks", "geometry"}:
            return lambda *args: None
        raise AttributeError(name)

    def withdraw(self):
        self.withdraw_count += 1

    def deiconify(self):
        self.deiconify_count += 1

    def after(self, _delay, callback):
        self.poll_callback = callback
        self.poll_scheduled.set()

    def mainloop(self):
        pass

    def winfo_reqwidth(self):
        return 100

    def winfo_reqheight(self):
        return 30

    def winfo_screenwidth(self):
        return 800

    def winfo_screenheight(self):
        return 600


class TestStopIndicator(unittest.TestCase):
    def test_failed_tk_initialization_does_not_accumulate_commands(self):
        fake_tk = types.SimpleNamespace(Tk=mock.Mock(side_effect=RuntimeError("headless")))
        with mock.patch.dict(sys.modules, {"tkinter": fake_tk}), mock.patch(
            "importlib.util.find_spec", return_value=object()
        ):
            indicator = StopIndicator()
            indicator._thread.join(timeout=1)

            # Exercise the interval after failure is recorded but before the thread exits.
            with mock.patch.object(indicator._thread, "is_alive", return_value=True):
                indicator.show()
                indicator.hide()

            self.assertEqual(indicator._queue.qsize(), 0)

    def test_show_during_tk_initialization_is_displayed_after_startup(self):
        root = FakeRoot()

        def create_root():
            root.initialization_started.set()
            if not root.allow_initialization.wait(timeout=2):
                raise RuntimeError("test did not release Tk initialization")
            return root

        fake_tk = types.SimpleNamespace(
            Tk=create_root,
            Label=lambda *_args, **_kwargs: types.SimpleNamespace(pack=lambda: None),
        )
        with mock.patch.dict(sys.modules, {"tkinter": fake_tk}), mock.patch(
            "importlib.util.find_spec", return_value=object()
        ):
            indicator = StopIndicator("ctrl+alt+f12")
            self.assertTrue(root.initialization_started.wait(timeout=1))

            # StopIndicator's Tk thread has started but has not initialized its root.
            indicator.show()
            root.allow_initialization.set()

            self.assertTrue(root.poll_scheduled.wait(timeout=1))
            root.poll_callback()

            self.assertEqual(root.deiconify_count, 1)
            indicator.close()


if __name__ == "__main__":
    unittest.main()
