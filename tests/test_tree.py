"""Unit tests for accessibility-tree summarization (token-cost levels), flattening
and node lookup."""

import json
import unittest

from computer_control.a11y.tree import (
    LEVEL_PRESETS,
    build_node,
    flatten_nodes,
    summarize,
)


def sample_tree():
    return {
        "id": 1,
        "role": "window",
        "name": "Main Window",
        "rect": [0, 0, 800, 600],
        "children": [
            {
                "id": 2,
                "role": "menu",
                "name": "File menu",
                "children": [
                    {"id": 3, "role": "menuitem", "name": "Open"},
                    {"id": 4, "role": "menuitem", "name": "Save As"},
                ],
            },
            {
                "id": 5,
                "role": "pane",
                "name": "Workspace",
                "children": [
                    {
                        "id": 6,
                        "role": "button",
                        "name": "Run the long-running analysis pipeline now",
                        "rect": [10, 10, 120, 30],
                    },
                    {"id": 7, "role": "edit", "name": ""},
                ],
            },
        ],
    }


class TestSummarize(unittest.TestCase):
    def test_skeleton_caps_depth_and_names(self):
        node, count, truncated = summarize(sample_tree(), preset="skeleton")
        self.assertFalse(truncated)
        self.assertEqual(count, 3)  # root + menu + pane; their children pruned by depth
        # depth 2: grandchildren of depth-2 nodes are dropped
        menu = node["children"][0]
        self.assertIsNone(menu.get("children"))
        pane = node["children"][1]
        self.assertIsNone(pane.get("children"))

    def test_standard_keeps_deeper_children(self):
        node, count, truncated = summarize(sample_tree(), preset="standard")
        self.assertEqual(count, 7)
        menu = node["children"][0]
        self.assertEqual([c["id"] for c in menu["children"]], [3, 4])

    def test_max_nodes_truncation(self):
        node, count, truncated = summarize(sample_tree(), preset="full", max_nodes=3)
        self.assertTrue(truncated)
        self.assertLessEqual(count, 3)

    def test_depth_zero(self):
        node, count, truncated = summarize(sample_tree(), preset="full", depth=1)
        self.assertIsNone(node.get("children"))
        self.assertEqual(count, 1)
        self.assertFalse(truncated)

    def test_include_rects_false(self):
        node, _, _ = summarize(sample_tree(), preset="full", include_rects=False)
        self.assertNotIn("rect", node)
        self.assertNotIn("rect", node["children"][1]["children"][0])

    def test_explicit_parameters_override_preset(self):
        node, count, truncated = summarize(sample_tree(), preset="full", depth=1)
        self.assertEqual(count, 1)
        self.assertIsNone(node.get("children"))

    def test_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            summarize(sample_tree(), preset="verbose")

    def test_json_serializable(self):
        node, _, _ = summarize(sample_tree(), preset="full")
        json.dumps(node)  # must not raise

    def test_empty_tree(self):
        node, count, truncated = summarize({"id": 1, "role": "root", "name": ""}, preset="full")
        self.assertEqual(count, 1)
        self.assertFalse(truncated)

    def test_name_truncation_uses_ellipsis(self):
        long_tree = {
            "id": 1, "role": "window", "name": "",
            "children": [{"id": 2, "role": "button", "name": "R" * 200}],
        }
        node, _, _ = summarize(long_tree, preset="skeleton")
        name = node["children"][0]["name"]
        self.assertTrue(name.endswith("..."))
        self.assertLessEqual(len(name), LEVEL_PRESETS["skeleton"]["name_cap"])


class TestFlatten(unittest.TestCase):
    def test_flatten_unique_ids(self):
        flat = flatten_nodes(sample_tree())
        ids = [n["id"] for n in flat]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(flat), 7)

    def test_flatten_finds_by_id(self):
        flat = flatten_nodes(sample_tree())
        by_id = {n["id"]: n for n in flat}
        self.assertEqual(by_id[6]["name"], "Run the long-running analysis pipeline now")


class TestBuildNode(unittest.TestCase):
    def test_build_node_dict(self):
        node = build_node("button", "OK", rect=(1, 2, 3, 4))
        self.assertEqual(node, {"role": "button", "name": "OK", "rect": [1, 2, 3, 4]})
        node = build_node("button", "OK")
        self.assertNotIn("rect", node)
        self.assertNotIn("id", node)  # ids are assigned by the tree walker


if __name__ == "__main__":
    unittest.main()
