"""Unit tests for the action registry: validation, coercion, ranges and risk."""

import unittest
from unittest.mock import Mock

from computer_control.engine import Engine
from computer_control.actions import (
    ACTION_REGISTRY,
    ValidationError,
    clean_arguments,
    get_spec,
    risk_for,
)


class TestRegistry(unittest.TestCase):
    def test_all_expected_tools_present(self):
        expected = {
            "screen.capture", "pointer.move", "pointer.click", "pointer.drag",
            "pointer.scroll", "keyboard.press", "keyboard.combo", "keyboard.type",
            "wait.pause", "a11y.snapshot", "a11y.activate", "a11y.input",
            "batch.execute",
        }
        self.assertEqual(set(ACTION_REGISTRY.keys()), expected)

    def test_specs_are_well_formed(self):
        for name, spec in ACTION_REGISTRY.items():
            self.assertTrue(spec.summary)
            self.assertIn(spec.risk, ("benign", "moderate", "high"))
            self.assertIsInstance(spec.params, list)
            self.assertTrue(spec.params)
            self.assertEqual(spec.name, name)

    def test_examples_validate(self):
        for name, spec in ACTION_REGISTRY.items():
            for example in spec.examples:
                try:
                    clean_arguments(name, example)
                except ValidationError as exc:
                    self.fail("example for %s invalid: %s" % (name, exc))


class TestValidation(unittest.TestCase):
    def test_missing_required(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_arguments("keyboard.type", {})
        self.assertIn("text", str(ctx.exception))

    def test_unknown_parameter_rejected(self):
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.click", {"button": "left", "frobnicate": 1})

    def test_type_coercion_numeric_strings(self):
        args = clean_arguments("pointer.move", {"x": "10.5", "y": 20})
        self.assertEqual(args["x"], 10.5)
        self.assertEqual(args["y"], 20)

    def test_type_rejects_junk(self):
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.move", {"x": "abc", "y": 1})
        with self.assertRaises(ValidationError):
            clean_arguments("wait.pause", {"ms": "soon"})

    def test_bool_coercion(self):
        args = clean_arguments("keyboard.type", {"text": "hi", "submit": "true"})
        self.assertIs(args["submit"], True)

    def test_ranges(self):
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.click", {"times": 9})
        with self.assertRaises(ValidationError):
            clean_arguments("wait.pause", {"ms": 10**9})
        with self.assertRaises(ValidationError):
            clean_arguments("screen.capture", {"scale": 5.0})
        with self.assertRaises(ValidationError):
            clean_arguments("screen.capture", {"quality": 200})
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.scroll", {"amount": 500})

    def test_button_enum(self):
        for b in ("left", "middle", "right"):
            clean_arguments("pointer.click", {"button": b})
        with self.assertRaises(ValidationError):
            clean_arguments("pointer.click", {"button": "side"})

    def test_region_shape(self):
        ok = clean_arguments("screen.capture", {"region": {"x": 0, "y": 0, "width": 10, "height": 10}})
        self.assertEqual(ok["region"]["width"], 10)
        with self.assertRaises(ValidationError):
            clean_arguments("screen.capture", {"region": {"x": 0, "y": 0}})
        with self.assertRaises(ValidationError):
            clean_arguments("screen.capture", {"region": {"x": 0, "y": 0, "width": -1, "height": 10}})

    def test_combo_accepts_list_or_string(self):
        args = clean_arguments("keyboard.combo", {"keys": "ctrl+shift+esc"})
        self.assertEqual(args["keys"], ["ctrl", "shift", "esc"])
        args = clean_arguments("keyboard.combo", {"keys": ["ctrl", "c"]})
        self.assertEqual(args["keys"], ["ctrl", "c"])
        with self.assertRaises(ValidationError):
            clean_arguments("keyboard.combo", {"keys": []})

    def test_combo_string_accepts_numpad_plus_key(self):
        args = clean_arguments("keyboard.combo", {"keys": "ctrl+numpad_+"})
        self.assertEqual(args["keys"], ["ctrl", "numpad_+"])

    def test_combo_string_rejects_malformed_or_unknown_keys(self):
        for keys in ("ctrl++q", "ctrl+notakey"):
            with self.subTest(keys=keys), self.assertRaises(ValidationError):
                clean_arguments("keyboard.combo", {"keys": keys})

    def test_key_names_validated(self):
        with self.assertRaises(ValidationError):
            clean_arguments("keyboard.press", {"key": "hyper"})
        clean_arguments("keyboard.press", {"key": "F5"})
        clean_arguments("keyboard.combo", {"keys": ["ctrl", "a"]})

    def test_text_length_limits(self):
        clean_arguments("keyboard.type", {"text": "x" * 10000})
        with self.assertRaises(ValidationError):
            clean_arguments("keyboard.type", {"text": "x" * 10001})

    def test_batch_items_must_be_a_list(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_arguments("batch.execute", {"items": 1})
        self.assertTrue(any("items" in issue for issue in ctx.exception.issues))

    def test_batch_tool_rejects_non_list_items_before_execution(self):
        gate = Mock()
        engine = Engine(Mock(), Mock(), gate, Mock(), lambda: None)

        result = engine.run_tool("batch.execute", {"items": 1})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        gate.evaluate.assert_not_called()

    def test_batch_validation(self):
        items = [{"tool": "wait.pause", "arguments": {"ms": 1}}, {"tool": "screen.capture", "arguments": {}}]
        args = clean_arguments("batch.execute", {"items": items})
        self.assertEqual(len(args["items"]), 2)
        with self.assertRaises(ValidationError):
            clean_arguments("batch.execute", {"items": []})
        # unknown tools are structurally valid; they become per-item errors at run time
        clean_arguments("batch.execute", {"items": [{"tool": "nope.nope", "arguments": {}}]})
        with self.assertRaises(ValidationError):
            clean_arguments("batch.execute", {"items": [{"tool": "keyboard.type", "arguments": {}}]})  # missing required
        with self.assertRaises(ValidationError):
            clean_arguments("batch.execute", {"items": [{"tool": 42, "arguments": {}}]})

    def test_nested_batch_validation_is_bounded(self):
        nested = {"tool": "wait.pause", "arguments": {"ms": 1}}
        for _ in range(1200):
            nested = {"tool": "batch.execute", "arguments": {"items": [nested]}}

        with self.assertRaises(ValidationError):
            clean_arguments("batch.execute", {"items": [nested]})

    def test_shallow_nested_batch_validation(self):
        nested = {
            "tool": "batch.execute",
            "arguments": {
                "items": [{"tool": "wait.pause", "arguments": {"ms": 1}}],
            },
        }
        args = clean_arguments("batch.execute", {"items": [nested]})
        self.assertEqual(args["items"][0]["tool"], "batch.execute")

    def test_defaults_filled(self):
        args = clean_arguments("pointer.click", {})
        self.assertEqual(args["button"], "left")
        self.assertEqual(args["times"], 1)
        self.assertIsNone(args["x"])
        args = clean_arguments("screen.capture", {})
        self.assertEqual(args["format"], "png")
        self.assertEqual(args["scale"], 1.0)


class TestRisk(unittest.TestCase):
    def test_risk_for_batch_inherits_max(self):
        self.assertEqual(risk_for("batch.execute", {"items": [{"tool": "screen.capture", "arguments": {}}]}), "benign")
        self.assertEqual(
            risk_for("batch.execute", {"items": [{"tool": "screen.capture", "arguments": {}}, {"tool": "keyboard.combo", "arguments": {"keys": ["win", "l"]}}]}),
            "high",
        )

    def test_risk_for_none_arguments(self):
        self.assertEqual(risk_for("keyboard.combo", None), "moderate")
        self.assertEqual(risk_for("screen.capture", None), "benign")
        self.assertEqual(risk_for("batch.execute", None), "benign")


class TestGetSpec(unittest.TestCase):
    def test_get_spec_unknown(self):
        self.assertIsNone(get_spec("no.such.tool"))


if __name__ == "__main__":
    unittest.main()
