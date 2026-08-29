"""Accessibility-tree processing: hierarchical summarization with token-cost
levels, node flattening and lookup.

The platform backend (windows_uia) produces a raw dict tree; this module
turns it into a model-facing summary. Everything here is pure logic so it is
fully unit-testable without a desktop.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

LEVEL_PRESETS = {
    "skeleton": {"depth": 2, "max_nodes": 80, "name_cap": 32},
    "standard": {"depth": 4, "max_nodes": 400, "name_cap": 64},
    "full": {"depth": 12, "max_nodes": 2000, "name_cap": 256},
}

_ELLIPSIS = "..."


def build_node(role: str, name: str, rect: Optional[tuple] = None) -> dict:
    """Build a raw node dict. ``rect`` is (left, top, right, bottom).
    The id is assigned by the tree walker."""
    role_str = str(role or "custom")
    name_str = str(name or "")
    if not name_str.strip():
        name_str = role_str
    node = {"role": role_str, "name": name_str}
    if rect is not None:
        node["rect"] = [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])]
    return node


def summarize(tree: dict, preset: str = "standard", depth: Optional[int] = None,
              max_nodes: Optional[int] = None, include_rects: bool = True,
              name_cap: Optional[int] = None) -> Tuple[dict, int, bool]:
    """Trim a raw tree to the requested summary profile.

    ``depth`` counts levels including the root (depth=1 keeps the root only).
    ``max_nodes`` caps the total number of nodes; names are capped at
    ``name_cap``. Returns (summarized_tree, node_count, truncated) where
    ``truncated`` means nodes were dropped because the node budget ran out
    (depth pruning is a normal, silent part of the profile).
    """
    if preset not in LEVEL_PRESETS:
        raise ValueError("unknown summary level %r" % preset)
    profile = LEVEL_PRESETS[preset]
    max_depth = profile["depth"] if depth is None else max(1, int(depth))
    node_limit = profile["max_nodes"] if max_nodes is None else max(1, int(max_nodes))
    cap = profile["name_cap"] if name_cap is None else max(1, int(name_cap))

    budget = {"remaining": node_limit, "truncated": False}

    def walk(node: dict, levels_left: int) -> dict:
        out = {
            "id": node.get("id"),
            "role": node.get("role", "custom"),
            "name": _cap(node.get("name", ""), cap),
        }
        if include_rects and "rect" in node:
            out["rect"] = node["rect"]
        budget["remaining"] -= 1
        children = node.get("children", [])
        if children and levels_left > 1:
            kept = []
            for child in children:
                if budget["remaining"] <= 0:
                    budget["truncated"] = True
                    break
                kept.append(walk(child, levels_left - 1))
            if kept:
                out["children"] = kept
        return out

    result = walk(tree, max_depth)
    count = node_limit - budget["remaining"]
    return result, count, budget["truncated"]


def _cap(name: str, limit: int) -> str:
    if name is None:
        return ""
    text = str(name)
    if len(text) <= limit:
        return text
    if limit <= len(_ELLIPSIS):
        return text[:limit]
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def flatten_nodes(tree: dict) -> List[dict]:
    """Flatten a tree into a list of nodes (pre-order)."""
    out: List[dict] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        children = node.get("children", [])
        stack.extend(reversed(children))
    return out


def index_by_id(tree: dict) -> dict:
    """Map node id -> node for the whole tree."""
    return {n["id"]: n for n in flatten_nodes(tree) if n.get("id") is not None}
