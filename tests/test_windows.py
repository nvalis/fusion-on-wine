from dataclasses import replace
import json
import unittest
from unittest.mock import call, patch

from internal.helpers.windows import WindowInfo, Watcher, associate_clients, is_overlay


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.owner = WindowInfo(
            1, 123, "Autodesk Fusion", (500, 100, 1200, 900), None, None,
            "_NET_WM_WINDOW_TYPE_NORMAL",
        )

    def test_recognizes_monitor_sized_overlay(self):
        overlay = WindowInfo(
            2, 123, "Fusion360", (0, 0, 2560, 1440), 1, 0.298,
            "_NET_WM_WINDOW_TYPE_DIALOG",
        )
        self.assertTrue(is_overlay(overlay, self.owner))

    def test_rejects_unrelated_or_non_overlay_windows(self):
        base = WindowInfo(
            2, 123, "Fusion360", (0, 0, 2560, 1440), 1, 0.298,
            "_NET_WM_WINDOW_TYPE_DIALOG",
        )
        cases = (
            (base, None),
            (replace(base, pid=456), self.owner),
            (replace(base, owner=None), self.owner),
            (replace(base, title="Create file"), self.owner),
            (replace(base, opacity=1), self.owner),
            (replace(base, kind="_NET_WM_WINDOW_TYPE_NORMAL"), self.owner),
        )
        for window, owner in cases:
            with self.subTest(window=window, owner=owner):
                self.assertFalse(is_overlay(window, owner))


class OpacityTests(unittest.TestCase):
    def setUp(self):
        self.watcher = Watcher.__new__(Watcher)
        self.watcher.opacity_applied = {}
        # No overlay title, owner, type, or matching compositor geometry.
        self.info = WindowInfo(7, 123, "Any title", (0, 0, 2560, 1440), None, 0.3, "")
        self.client = {
            "address": "0x123", "class": "fusion360.exe", "pid": 123,
            "title": self.info.title, "at": [50, 50], "size": [800, 600],
        }
        self.infos = {7: self.info}
        self.clients = {7: self.client}
        self.setprop = self.enterContext(patch("internal.helpers.windows.setprop"))
        self.enterContext(patch("builtins.print"))

    def sync(self, info=None, clients=None):
        self.watcher.sync_opacity(
            {7: info or self.info}, self.clients if clients is None else clients,
        )

    def assert_opacity(self, alpha):
        self.assertEqual(self.setprop.call_args_list, [
            call("0x123", prop, alpha)
            for prop in ("opacity", "opacity_inactive", "opacity_fullscreen")
        ])

    def test_translates_request_without_overlay_heuristics(self):
        clients = associate_clients(self.infos, [self.client], {})
        self.sync(clients=clients)
        self.assert_opacity("0.3")

    def test_unchanged_request_does_not_repeat_ipc(self):
        self.sync()
        self.setprop.reset_mock()
        self.sync()
        self.setprop.assert_not_called()

    def test_opacity_changes_include_zero_and_one(self):
        self.sync()
        for value in (0.0, 1.0, 0.6):
            with self.subTest(value=value):
                self.setprop.reset_mock()
                self.sync(replace(self.info, opacity=value))
                self.assert_opacity(str(value))

    def test_property_removal_restores_opacity_once(self):
        self.sync()
        self.setprop.reset_mock()
        removed = replace(self.info, opacity=None)
        self.sync(removed)
        self.assert_opacity("1.0")
        self.setprop.reset_mock()
        self.sync(removed)
        self.setprop.assert_not_called()

    def test_windows_without_opacity_are_left_alone(self):
        self.sync(replace(self.info, opacity=None))
        self.setprop.assert_not_called()

    def test_unmatched_client_is_retried(self):
        self.sync(clients={})
        self.setprop.assert_not_called()
        self.sync()
        self.assert_opacity("0.3")

    def test_temporary_missing_client_does_not_lose_removal(self):
        self.sync()
        self.sync(clients={})
        self.setprop.reset_mock()
        self.sync(replace(self.info, opacity=None))
        self.assert_opacity("1.0")

    def test_failed_ipc_is_retried(self):
        self.setprop.side_effect = RuntimeError("IPC not ready")
        with self.assertRaises(RuntimeError):
            self.sync()
        self.setprop.side_effect = None
        self.setprop.reset_mock()
        self.sync()
        self.assert_opacity("0.3")

    def test_new_compositor_address_receives_request_again(self):
        self.sync()
        self.setprop.reset_mock()
        self.sync(clients={7: {**self.client, "address": "0x456"}})
        self.assertEqual(self.setprop.call_args_list, [
            call("0x456", prop, "0.3")
            for prop in ("opacity", "opacity_inactive", "opacity_fullscreen")
        ])
        self.assertEqual(self.watcher.opacity_applied, {(7, "0x456"): 0.3})

    def test_disappeared_window_is_forgotten(self):
        self.sync()
        self.watcher.sync_opacity({}, {})
        self.assertEqual(self.watcher.opacity_applied, {})

    def test_opacity_runs_on_mixed_dpi_desktop(self):
        self.enterContext(patch.object(self.watcher, "root", create=True))
        # Supply a real snapshot through the existing X11 reader boundary.
        self.watcher.root.query_tree.return_value.children = [object()]
        self.watcher.read = lambda window: self.info
        self.watcher.client_addresses = {}
        self.watcher.next_geometry_monitors = 0
        self.enterContext(patch("internal.helpers.windows.hypr", side_effect=[
            json.dumps([self.client]), json.dumps([{"scale": 1}, {"scale": 1.5}]),
        ]))
        self.watcher.sync_windows()
        self.assertFalse(self.watcher.geometry_enabled)
        self.assert_opacity("0.3")

    def test_does_not_associate_other_processes_or_apps(self):
        for unrelated in (
            {**self.client, "pid": 456},
            {**self.client, "class": "another.exe"},
        ):
            with self.subTest(client=unrelated):
                clients = associate_clients(self.infos, [unrelated], {})
                self.sync(clients=clients)
                self.setprop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
