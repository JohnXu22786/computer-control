"""Unit tests for configuration loading, validation and rule schema checking."""

import json
import os
import tempfile
import unittest

from computer_control.config import ConfigError, apply_overrides, default_config, from_dict, load_config


class TestDefaults(unittest.TestCase):
    def test_default_values(self):
        c = default_config()
        self.assertEqual(c.platform.name, "auto")
        self.assertEqual(c.capture.default_width, 1920)
        self.assertEqual(c.capture.default_format, "png")
        self.assertEqual(c.safety.emergency_hotkey, "ctrl+alt+f12")
        self.assertEqual(c.safety.confirm_threshold, "high")
        self.assertEqual(c.safety.confirm_timeout_s, 30)
        self.assertEqual(c.safety.idle_timeout_s, 0)
        self.assertEqual(c.safety.idle_action, "standby")
        self.assertEqual(c.safety.default_rule, "allow")
        self.assertEqual(c.safety.rules, [])
        self.assertEqual(c.a11y.default_level, "standard")
        self.assertEqual(c.runtime.batch_gap_ms, 150)

    def test_default_rules_are_not_dangerous_by_default(self):
        # High-risk chords must pass the gate (they are confirmation-gated, not denied).
        self.assertEqual(default_config().safety.default_rule, "allow")


class TestFromDict(unittest.TestCase):
    def test_partial_overrides(self):
        c = from_dict({"capture": {"default_width": 1280}, "safety": {"confirm_timeout_s": 5}})
        self.assertEqual(c.capture.default_width, 1280)
        self.assertEqual(c.safety.confirm_timeout_s, 5)
        self.assertEqual(c.capture.default_format, "png")  # untouched default

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"no_such_section": {}})

    def test_unknown_nested_key_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"capture": {"no_such_option": 1}})

    def test_invalid_enum_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"capture": {"default_format": "gif"}})
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"confirm_threshold": "critical"}})

    def test_invalid_numbers_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"confirm_timeout_s": -1}})
        with self.assertRaises(ConfigError):
            from_dict({"runtime": {"batch_gap_ms": -5}})
        with self.assertRaises(ConfigError):
            from_dict({"capture": {"default_width": 0}})

    def test_invalid_hotkey_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"emergency_hotkey": "not+a+valid+key+name+"}})
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"emergency_hotkey": "ctrl+ctrl"}})  # duplicate

    def test_empty_hotkey_disables(self):
        c = from_dict({"safety": {"emergency_hotkey": ""}})
        self.assertEqual(c.safety.emergency_hotkey, "")

    def test_rule_schema_validation(self):
        good = [{"match": {"tool": "keyboard.*", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"}]
        c = from_dict({"safety": {"rules": good}})
        self.assertEqual(len(c.safety.rules), 1)

        with self.assertRaises(ConfigError):
            from_dict({"safety": {"rules": [{"match": {"tool": "x"}, "effect": "banana"}]}})
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"rules": [{"match": {"tool": "x", "argument": {"name": "k", "matcher": "nope", "value": 1}}, "effect": "deny"}]}})
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"rules": [{"match": {"tool": "x"}, "effect": "deny", "extra": 1}]}})
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"rules": ["not-a-dict"]}})

    def test_idle_action_enum(self):
        with self.assertRaises(ConfigError):
            from_dict({"safety": {"idle_action": "explode"}})

    def test_platform_name_enum(self):
        c = from_dict({"platform": {"name": "dry-run"}})
        self.assertEqual(c.platform.name, "dry-run")
        with self.assertRaises(ConfigError):
            from_dict({"platform": {"name": "amiga"}})

    def test_a11y_level_enum(self):
        with self.assertRaises(ConfigError):
            from_dict({"a11y": {"default_level": "verbose"}})


class TestLoadConfig(unittest.TestCase):
    def test_load_file_and_merge(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"capture": {"default_width": 1024}}, f)
            c = load_config(path)
            self.assertEqual(c.capture.default_width, 1024)
            self.assertEqual(c.safety.emergency_hotkey, "ctrl+alt+f12")

    def test_load_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config(os.path.join(tempfile.gettempdir(), "definitely-missing-config-xyz.json"))

    def test_load_bad_json_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(ConfigError):
                load_config(path)


class TestApplyOverrides(unittest.TestCase):
    def test_apply_overrides_replaces_rules(self):
        base = default_config()
        patched = apply_overrides(base, {"safety": {"rules": [{"match": {"tool": "pointer.*"}, "effect": "deny"}]}})
        self.assertEqual(len(patched.safety.rules), 1)
        self.assertEqual(len(base.safety.rules), 0)  # original untouched

    def test_apply_overrides_validates(self):
        with self.assertRaises(ConfigError):
            apply_overrides(default_config(), {"capture": {"default_format": "bmp"}})


if __name__ == "__main__":
    unittest.main()
