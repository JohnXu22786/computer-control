"""Focused regressions for Windows UI Automation thread ownership."""

import sys
import threading
import types
import unittest
from unittest import mock

from computer_control.a11y.windows_uia import UiaBridge
from computer_control.drivers.base import DriverError


class _ElementCollection:
    def __init__(self, elements=()):
        self._elements = elements
        self.Length = len(elements)

    def GetElement(self, index):
        return self._elements[index]


class _TreeElement:
    def __init__(self, name, children=()):
        self.name = name
        self.children = children

    def GetCurrentPropertyValue(self, property_id):
        return {1: 50032, 2: self.name}.get(property_id)

    def GetCurrentBoundingRectangle(self):
        raise RuntimeError("no bounds")

    def FindAll(self, scope, condition):
        return _ElementCollection(self.children)


class _RootElement:
    def GetCurrentPropertyValue(self, property_id):
        return {1: "Desktop", 2: 50032}.get(property_id)

    def GetCurrentBoundingRectangle(self):
        raise RuntimeError("no bounds")

    def FindAll(self, scope, condition):
        return _ElementCollection()


class _Automation:
    def __init__(self, owner_thread):
        self.owner_thread = owner_thread

    def GetRootElement(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("COM object used from a different apartment thread")
        return _RootElement()

    def CreateTrueCondition(self):
        return object()


class TestUiaThreadOwnership(unittest.TestCase):
    def test_probe_thread_object_is_not_reused_for_worker_snapshot(self):
        comtypes = types.ModuleType("comtypes")
        comtypes.__path__ = []
        client = types.ModuleType("comtypes.client")
        client.GetModule = mock.Mock()
        initialized_on = []
        uninitialized_on = []
        comtypes.CoInitialize = lambda: initialized_on.append(threading.get_ident())
        comtypes.CoUninitialize = lambda: uninitialized_on.append(threading.get_ident())
        comtypes.client = client
        gen = types.ModuleType("comtypes.gen")
        gen.__path__ = []
        uia_gen = types.ModuleType("comtypes.gen.UIAutomationClient")
        uia_gen.UIA_ControlTypePropertyId = 1
        uia_gen.UIA_NamePropertyId = 2
        gen.UIAutomationClient = uia_gen

        bridge = UiaBridge()
        created_on = []

        def create_automation(_comtypes, _uia_gen):
            thread_id = threading.get_ident()
            created_on.append(thread_id)
            return _Automation(thread_id)

        bridge._create_automation = create_automation
        modules = {
            "comtypes": comtypes,
            "comtypes.client": client,
            "comtypes.gen": gen,
            "comtypes.gen.UIAutomationClient": uia_gen,
        }
        result = {}
        worker_thread_id = []

        with mock.patch.dict(sys.modules, modules):
            self.assertTrue(bridge.available)

            def take_snapshot():
                worker_thread_id.append(threading.get_ident())
                try:
                    result["snapshot"] = bridge.snapshot({})
                except Exception as exc:
                    result["error"] = exc
                finally:
                    bridge.close()

            worker = threading.Thread(target=take_snapshot, name="session-worker")
            worker.start()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        bridge.close()
        self.assertNotIn("error", result)
        self.assertIn("snapshot", result)
        self.assertNotEqual(created_on[0], worker_thread_id[0])
        self.assertEqual(created_on, [threading.get_ident(), worker_thread_id[0]])
        self.assertEqual(initialized_on, [threading.get_ident(), worker_thread_id[0]])
        self.assertEqual(sorted(uninitialized_on), sorted([threading.get_ident(), worker_thread_id[0]]))


class TestUiaActivationMethods(unittest.TestCase):
    def _bridge_with_bounded_element(self):
        bridge = UiaBridge()
        bridge._require = mock.Mock(return_value=(object(), object()))
        bridge._element = mock.Mock(return_value=object())
        bridge._center_of = mock.Mock(return_value={"x": 12.0, "y": 34.0})
        return bridge

    def test_pattern_mode_does_not_fall_back_to_pointer(self):
        bridge = self._bridge_with_bounded_element()
        bridge._invoke_patterns = mock.Mock(return_value=None)

        with self.assertRaises(DriverError) as raised:
            bridge.activate(7, method="pattern")

        self.assertEqual(raised.exception.code, "pattern_unavailable")
        bridge._invoke_patterns.assert_called_once()

    def test_pointer_skips_patterns_and_auto_keeps_pointer_fallback(self):
        bridge = self._bridge_with_bounded_element()
        bridge._invoke_patterns = mock.Mock(return_value=None)

        pointer = bridge.activate(7, method="pointer")

        self.assertEqual(pointer["method_used"], "pointer")
        bridge._invoke_patterns.assert_not_called()

        automatic = bridge.activate(7, method="auto")

        self.assertEqual(automatic["method_used"], "pointer")
        bridge._invoke_patterns.assert_called_once()

    def test_pattern_mode_reports_supported_pattern_failure(self):
        bridge = self._bridge_with_bounded_element()
        pattern = mock.Mock()
        pattern.value = pattern
        pattern.QueryInterface.return_value = pattern
        pattern.Invoke.side_effect = RuntimeError("invoke failed")
        element = mock.Mock()

        def get_current_pattern(pattern_id):
            if pattern_id == 10000:
                return pattern
            raise RuntimeError("pattern unavailable")

        element.GetCurrentPattern.side_effect = get_current_pattern
        bridge._element.return_value = element
        module = types.SimpleNamespace(
            IUIAutomationInvokePattern=object(),
            IUIAutomationTogglePattern=object(),
            IUIAutomationSelectionItemPattern=object(),
        )
        bridge._require.return_value = (object(), module)

        with self.assertRaises(DriverError) as raised:
            bridge.activate(7, method="pattern")

        self.assertEqual(raised.exception.code, "pattern_activation_failed")

        automatic = bridge.activate(7, method="auto")

        self.assertEqual(automatic["method_used"], "pointer")


class TestUiaSnapshotNodeVisibility(unittest.TestCase):
    def test_depth_pruned_node_is_not_actionable_and_new_snapshot_replaces_index(self):
        root = _TreeElement("Desktop", [
            _TreeElement("Visible child", [_TreeElement("Pruned grandchild")]),
        ])
        auto = mock.Mock()
        auto.GetRootElement.return_value = root
        auto.CreateTrueCondition.return_value = object()
        module = types.SimpleNamespace(
            UIA_ControlTypePropertyId=1,
            UIA_NamePropertyId=2,
        )
        bridge = UiaBridge()
        bridge._require = mock.Mock(return_value=(auto, module))
        bridge._center_of = mock.Mock(return_value={"x": 1.0, "y": 2.0})

        snapshot = bridge.snapshot({"level": "full", "depth": 2})

        self.assertEqual(snapshot["node_count"], 2)
        self.assertEqual(bridge._element(2), root.children[0])
        self.assertEqual(bridge.activate(2, method="pointer")["node_id"], 2)
        for name, action in (("activate", lambda: bridge.activate(3)),
                             ("input", lambda: bridge.set_text(3, "text"))):
            with self.subTest(action=name):
                with self.assertRaises(DriverError) as raised:
                    action()
                self.assertEqual(raised.exception.code, "unknown_node")

        auto.GetRootElement.return_value = _TreeElement("New desktop")
        bridge.snapshot({"level": "full"})
        with self.assertRaises(DriverError) as raised:
            bridge._element(2)
        self.assertEqual(raised.exception.code, "unknown_node")


if __name__ == "__main__":
    unittest.main()
