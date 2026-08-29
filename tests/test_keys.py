"""Key-name parsing tests: aliases, virtual-key codes, shortcut shortening,
hotkey specs and parser edge cases shared by configuration, validation and
drivers."""

import unittest

from computer_control.keys import (UnknownKeyError, is_modifier, parse_hotkey,
                                   parse_key, parse_key_full)


class TestParseKey(unittest.TestCase):
    def test_letters_and_digits(self):
        self.assertEqual(parse_key("a"), 0x41)  # VK_A
        self.assertEqual(parse_key("A"), 0x41)
        self.assertEqual(parse_key("z"), 0x5A)
        self.assertEqual(parse_key("0"), 0x30)
        self.assertEqual(parse_key("9"), 0x39)

    def test_common_aliases(self):
        cases = {
            "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
            "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
            "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "capslock": 0x14, "numlock": 0x90, "printscreen": 0x2C, "pause": 0x13,
        }
        for name, vk in cases.items():
            self.assertEqual(parse_key(name), vk, name)
            self.assertEqual(parse_key(name.upper()), vk, name)

    def test_f_keys(self):
        self.assertEqual(parse_key("f1"), 0x70)
        self.assertEqual(parse_key("f12"), 0x7B)
        self.assertEqual(parse_key("f24"), 0x87)

    def test_numpad_keys(self):
        self.assertEqual(parse_key("numpad0"), 0x60)
        self.assertEqual(parse_key("numpad9"), 0x69)
        self.assertEqual(parse_key("numpad_enter"), 0x0D)
        self.assertEqual(parse_key("numpad_*"), 0x6A)
        self.assertEqual(parse_key("numpad_+"), 0x6B)
        self.assertEqual(parse_key("numpad_-"), 0x6D)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(parse_key("  Enter  "), 0x0D)
        self.assertEqual(parse_key("NumLock"), 0x90)

    def test_rejects_garbage(self):
        for bad in ("", "  ", "??", "ctrl+alt", "f0", "f25", "win1", 42, None,
                    "supercalifragilistic"):
            with self.assertRaises(UnknownKeyError, msg=repr(bad)):
                parse_key(bad)


class TestParseKeyFull(unittest.TestCase):
    def test_extended_flag_only_on_numpad_enter(self):
        vk, extended = parse_key_full("numpad_enter")
        self.assertEqual(vk, 0x0D)
        self.assertTrue(extended)
        for name in ("enter", "left", "f5", "a"):
            vk, extended = parse_key_full(name)
            self.assertFalse(extended, name)


class TestParseHotkey(unittest.TestCase):
    def test_empty_disables(self):
        self.assertEqual(parse_hotkey(""), [])
        self.assertEqual(parse_hotkey("   "), [])
        self.assertEqual(parse_hotkey(None), [])

    def test_standard_combo(self):
        self.assertEqual(parse_hotkey("ctrl+alt+f12"), ["ctrl", "alt", "f12"])
        self.assertEqual(parse_hotkey("  Ctrl + Shift + Q "), ["ctrl", "shift", "q"])

    def test_duplicate_components_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("ctrl+ctrl+q")

    def test_empty_component_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("ctrl++q")
        with self.assertRaises(ValueError):
            parse_hotkey("+ctrl")

    def test_unknown_key_rejected(self):
        with self.assertRaises(UnknownKeyError):
            parse_hotkey("ctrl+alt+notakey")


class TestIsModifier(unittest.TestCase):
    def test_modifier_names(self):
        for name in ("ctrl", "alt", "shift", "win", "lctrl", "rctrl", "lalt", "ralt",
                     "lshift", "rshift", "lwin", "rwin", "super", "meta"):
            self.assertTrue(is_modifier(name), name)
            self.assertTrue(is_modifier(name.upper()), name)
        for name in ("left", "enter", "f5", "a", "space", "backspace"):
            self.assertFalse(is_modifier(name))


if __name__ == "__main__":
    unittest.main()