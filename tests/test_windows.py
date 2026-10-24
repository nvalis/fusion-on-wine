from dataclasses import replace
import json
import unittest
from unittest.mock import call, patch

from Xlib import Xutil

from internal.helpers.windows import (
    WindowInfo, Watcher, associate_clients, geometry_commands, is_overlay, requested_geometry,
)


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


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.actual = (3708, 613, 250, 453)
        # Real WM_NORMAL_HINTS from the misplaced sketch palette.
        self.hints = [564, 4844, 349, 0, 0, 250, 453, 250, 453, 0, 0, 0, 0, 0, 0, 0, 0, 10]
        self.info = WindowInfo(
            7, 123, "Fusion360", self.actual, 1, None, "_NET_WM_WINDOW_TYPE_DIALOG", True,
            requested_geometry(self.actual, self.hints),
        )
        self.client = {
            "address": "0x123", "class": "fusion360.exe", "pid": 123, "title": "Fusion360",
            "at": [3708, 613], "size": [250, 453], "floating": True,
        }

    def test_restores_win32_position_not_compositor_position(self):
        self.assertEqual(self.info.requested_rect, (4844, 349, 250, 453))
        self.assertEqual(geometry_commands(self.info, self.client), [
            "dispatch movewindowpixel exact 4844 349,address:0x123",
        ])

    def test_association_still_uses_actual_geometry(self):
        other = replace(self.info, xid=8, rect=(2971, 179, 300, 1160))
        other_client = {**self.client, "address": "0x456", "at": [2971, 179], "size": [300, 1160]}
        clients = associate_clients({7: self.info, 8: other}, [other_client, self.client], {})
        self.assertEqual(clients[7]["address"], "0x123")
        self.assertEqual(clients[8]["address"], "0x456")

    def test_repair_is_idempotent(self):
        fixed = {**self.client, "at": [4844, 349]}
        self.assertEqual(geometry_commands(self.info, fixed), [])

    def test_managed_windows_are_not_repositioned(self):
        self.assertEqual(geometry_commands(replace(self.info, override_redirect=False), self.client), [])

    def test_missing_or_short_hints_preserve_actual_geometry(self):
        for hints in (None, [], [1, 2], [0] * 8):
            with self.subTest(hints=hints):
                self.assertEqual(requested_geometry(self.actual, hints), self.actual)

    def test_unflagged_values_are_ignored(self):
        self.hints[0] = 0
        self.assertEqual(requested_geometry(self.actual, self.hints), self.actual)

    def test_negative_monitor_coordinates(self):
        self.hints[1:3] = [(-1400) & 0xffffffff, (-200) & 0xffffffff]
        self.assertEqual(requested_geometry(self.actual, self.hints), (-1400, -200, 250, 453))

    def test_explicit_size_and_position(self):
        self.hints[:5] = [Xutil.USPosition | Xutil.USSize, 100, 200, 300, 400]
        self.assertEqual(requested_geometry(self.actual, self.hints), (100, 200, 300, 400))

    def test_fixed_size_restores_compositor_resize(self):
        self.assertEqual(requested_geometry((3708, 613, 500, 600), self.hints), (4844, 349, 250, 453))

    def test_resizable_hints_do_not_force_a_size(self):
        self.hints[7:9] = [1000, 1000]
        self.assertEqual(requested_geometry(self.actual, self.hints), (4844, 349, 250, 453))

    def test_invalid_sizes_are_ignored(self):
        for width, height in ((0, 0), (0xffffffff, 453), (250, 100000)):
            with self.subTest(size=(width, height)):
                self.hints[5:9] = [width, height, width, height]
                self.assertEqual(requested_geometry(self.actual, self.hints), (4844, 349, 250, 453))


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
