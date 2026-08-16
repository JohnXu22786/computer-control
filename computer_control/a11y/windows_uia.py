"""Windows UI Automation bridge (optional: requires ``comtypes``).

Fetches the desktop accessibility tree, keeps a node-id -> element index for
the most recent snapshot, and performs semantic activation (invoke/toggle/
select patterns) and text entry (value pattern) with a bounding-box pixel
fallback.

Everything is wrapped so that a missing comtypes, a headless session or an
uncooperative application degrades to ``available == False`` instead of
crashing the plugin.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from computer_control.a11y.tree import build_node, summarize
from computer_control.drivers.base import DriverError

# Property / pattern ids are read from the generated module, never hardcoded.
_TREE_SCOPE_CHILDREN = 2

# Common control types, mostly for readable role names.
CONTROL_TYPE_NAMES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator",
    50039: "semanticzoom",
}


class UiaBridge:
    """Lazy COM client. Create it any time; it only touches COM on first use."""

    def __init__(self):
        self._auto = None
        self._module = None
        self._nodes = {}
        self._lock = threading.RLock()
        self._walk_cap = 5000
        self._init_error = None

    # ------------------------------------------------------------ lifecycle

    def _ensure(self):
        if self._auto is not None:
            return True
        if self._init_error is not None:
            return False
        try:
            import comtypes
            import comtypes.client

            comtypes.client.GetModule("UIAutomationCore.dll")
            import comtypes.gen.UIAutomationClient as uia_gen

            auto = self._create_automation(comtypes, uia_gen)
            self._auto = auto
            self._module = uia_gen
            return True
        except Exception as exc:  # pragma: no cover - environment dependent
            self._init_error = str(exc)
            return False

    @staticmethod
    def _create_automation(comtypes, uia_gen):
        """Instantiate the UIA client object. Prefer the registered ProgID,
        fall back to the class id with the in-process context (which is how
        uiautomationcore.dll registers itself on most systems)."""
        try:
            return comtypes.client.CreateObject("UIAutomationClient.CUIAutomation",
                                                interface=uia_gen.IUIAutomation)
        except Exception:
            clsid = getattr(uia_gen.CUIAutomation, "_reg_clsid_", None)
            if clsid is None:
                clsid = comtypes.GUID("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
            return comtypes.CoCreateInstance(clsid, interface=uia_gen.IUIAutomation,
                                             clsctx=comtypes.CLSCTX_INPROC_SERVER)

    @property
    def available(self) -> bool:
        return self._ensure()

    def _require(self):
        if not self._ensure():
            raise DriverError(
                "Windows UI Automation is unavailable (%s); install the optional "
                "'comtypes' dependency and ensure an interactive desktop session" % self._init_error,
                code="backend_unavailable",
            )
        return self._auto, self._module

    def close(self):
        self._nodes.clear()
        self._auto = None

    # ------------------------------------------------------------- snapshot

    def snapshot(self, options: dict) -> dict:
        with self._lock:
            auto, mod = self._require()
            try:
                root = auto.GetRootElement()
            except Exception as exc:
                raise DriverError("cannot reach the UIA root element: %s" % exc, code="backend_unavailable")

            self._walk_cap = options.get("hard_walk_cap", 5000)
            self._nodes = {}
            next_id = {"value": 1}

            def walk(element, budget):
                if budget["remaining"] <= 0:
                    budget["truncated"] = True
                    return None
                node = build_node(
                    role=_control_role(mod, _prop(element, mod.UIA_ControlTypePropertyId)),
                    name=str(_prop(element, mod.UIA_NamePropertyId, "") or ""),
                )
                rect = _bounding_rect(element)
                if rect is not None and options.get("include_rects", True):
                    node["rect"] = [rect[0], rect[1], rect[2], rect[3]]
                node["id"] = next_id["value"]
                next_id["value"] += 1
                budget["remaining"] -= 1
                self._nodes[node["id"]] = element

                try:
                    condition = auto.CreateTrueCondition()
                    elements = element.FindAll(_TREE_SCOPE_CHILDREN, condition)
                    count = elements.Length
                    children = []
                    for i in range(count):
                        child = elements.GetElement(i)
                        sub = walk(child, budget)
                        if sub is not None:
                            children.append(sub)
                    if children:
                        node["children"] = children
                except Exception:
                    pass  # elements that cannot enumerate children just have none
                return node

            budget = {"remaining": self._walk_cap, "truncated": False}
            raw = walk(root, budget)
            if raw is None:
                raise DriverError("accessibility tree is empty", code="backend_unavailable")

            summary, count, truncated = summarize(
                raw,
                preset=options.get("level", "standard"),
                depth=options.get("depth"),
                max_nodes=options.get("max_nodes"),
                include_rects=options.get("include_rects", True),
                name_cap=options.get("max_name_len"),
            )
            return {"tree": summary, "node_count": count,
                    "truncated": truncated or budget["truncated"], "generated_at": time.time()}

    # ------------------------------------------------------ element lookup

    def _element(self, node_id: int):
        with self._lock:
            element = self._nodes.get(node_id)
        if element is None:
            raise DriverError(
                "element %s is not part of the current snapshot" % node_id,
                code="unknown_node",
            )
        return element

    # ------------------------------------------------------------ activate

    def activate(self, node_id: int, method: str = "auto") -> dict:
        with self._lock:
            auto, mod = self._require()
            element = self._element(node_id)
            position = self._center_of(element)

            if method != "pointer":
                result = self._invoke_patterns(element, mod)
                if result is not None:
                    return {"node_id": node_id, "method_used": result, "position": position}

            if position is None:
                raise DriverError(
                    "element %s has no bounding box and no invokable pattern" % node_id,
                    code="element_stale",
                )
            return {"node_id": node_id, "method_used": "pointer", "position": position}

    def _invoke_patterns(self, element, mod) -> Optional[str]:
        for pattern_id, iface_name, call in (
            (10000, "IUIAutomationInvokePattern", "Invoke"),
            (10001, "IUIAutomationTogglePattern", "Toggle"),
            (10010, "IUIAutomationSelectionItemPattern", "Select"),
        ):
            try:
                variant = element.GetCurrentPattern(pattern_id)
                obj = variant.value if hasattr(variant, "value") else variant
                iface = getattr(mod, iface_name, None)
                if iface is None:
                    continue
                pattern = obj.QueryInterface(iface)
                getattr(pattern, call)()
                return {10000: "invoke", 10001: "toggle", 10010: "select"}[pattern_id]
            except Exception:
                continue
        return None

    # ----------------------------------------------------------- set text

    def set_text(self, node_id: int, text: str) -> dict:
        with self._lock:
            auto, mod = self._require()
            element = self._element(node_id)
            try:
                variant = element.GetCurrentPattern(10002)  # ValuePattern
                obj = variant.value if hasattr(variant, "value") else variant
                iface = getattr(mod, "IUIAutomationValuePattern", None)
                if iface is not None:
                    pattern = obj.QueryInterface(iface)
                    pattern.SetValue(text)
                    return {"node_id": node_id, "method_used": "value", "chars": len(text)}
            except Exception:
                pass
            position = self._center_of(element)
            if position is None:
                raise DriverError(
                    "element %s has no value pattern and no bounding box for the click fallback" % node_id,
                    code="element_stale",
                )
            return {"node_id": node_id, "method_used": "pointer_type", "position": position}

    # -------------------------------------------------------------- helpers

    def _center_of(self, element):
        rect = _bounding_rect(element)
        if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
            return None
        return {"x": (rect[0] + rect[2]) / 2.0, "y": (rect[1] + rect[3]) / 2.0}


def _prop(element, property_id, default=None):
    try:
        variant = element.GetCurrentPropertyValue(property_id)
        if hasattr(variant, "value"):
            value = variant.value
        else:
            value = variant
        if value is None:
            return default
        if isinstance(value, int):
            return value
        return default if value is None else value
    except Exception:
        return default


def _bounding_rect(element):
    try:
        rect = element.GetCurrentBoundingRectangle()
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if right <= left or bottom <= top:
            return None
        return (int(left), int(top), int(right), int(bottom))
    except Exception:
        return None


def _control_role(mod, control_type):
    try:
        return CONTROL_TYPE_NAMES.get(int(control_type or 0), "custom")
    except (TypeError, ValueError):
        return "custom"
