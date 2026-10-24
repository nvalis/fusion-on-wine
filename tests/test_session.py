"""Application menus may omit the compositor signature but retain its Wayland socket."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from internal.runtime import ROOT, Settings, hyprland_signature, launch


class HyprlandSessionTests(unittest.TestCase):
    def setUp(self):
        self.environment = self.enterContext(patch.dict("os.environ", {
            "WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000",
        }, clear=True))
        self.enterContext(patch("internal.runtime.shutil.which", return_value="/usr/bin/hyprctl"))
        self.run = self.enterContext(patch("internal.runtime.subprocess.run"))
        self.instances = [
            {"instance": "nested", "wl_socket": "wayland-2"},
            {"instance": "desktop", "wl_socket": "wayland-1"},
        ]
        self.set_instances(self.instances)

    def set_instances(self, instances):
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps(instances))

    def test_application_menu_without_signature(self):
        self.assertEqual(hyprland_signature(), "desktop")
        self.assertNotIn("HYPRLAND_INSTANCE_SIGNATURE", self.environment)
        self.run.assert_called_once_with(
            ["hyprctl", "-j", "instances"], capture_output=True, text=True, timeout=3, check=True,
        )

    def test_explicit_signature_is_preserved(self):
        self.environment["HYPRLAND_INSTANCE_SIGNATURE"] = "explicit"
        self.assertEqual(hyprland_signature(), "explicit")
        self.run.assert_not_called()

    def test_sway_takes_precedence(self):
        self.environment.update(SWAYSOCK="/sway/socket", HYPRLAND_INSTANCE_SIGNATURE="stale")
        self.assertEqual(hyprland_signature(), "")
        self.run.assert_not_called()

    def test_absolute_wayland_socket(self):
        self.environment["WAYLAND_DISPLAY"] = "/run/user/1000/wayland-1"
        self.assertEqual(hyprland_signature(), "desktop")

    def test_different_runtime_socket_is_not_selected(self):
        self.environment["WAYLAND_DISPLAY"] = "/another/session/wayland-1"
        self.assertEqual(hyprland_signature(), "")

    def test_ambiguous_sessions_are_rejected(self):
        self.set_instances(self.instances + [{"instance": "duplicate", "wl_socket": "wayland-1"}])
        with self.assertRaisesRegex(RuntimeError, "Multiple Hyprland instances"):
            hyprland_signature()

    def test_missing_wayland_socket_does_not_guess(self):
        self.environment.pop("WAYLAND_DISPLAY")
        self.assertEqual(hyprland_signature(), "")
        self.run.assert_not_called()

    def test_other_compositors_are_left_alone(self):
        self.set_instances([])
        self.assertEqual(hyprland_signature(), "")

    def test_known_hyprland_session_does_not_silently_skip_helper(self):
        self.environment["XDG_CURRENT_DESKTOP"] = "Hyprland"
        self.set_instances([])
        with self.assertRaisesRegex(RuntimeError, "No Hyprland instance matches"):
            hyprland_signature()

    def test_missing_hyprctl_on_known_hyprland_session(self):
        self.environment["XDG_CURRENT_DESKTOP"] = "Hyprland"
        with patch("internal.runtime.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Check WAYLAND_DISPLAY and hyprctl"):
                hyprland_signature()

    def test_unavailable_instance_list(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("hyprctl", 3),
                        subprocess.CalledProcessError(1, "hyprctl")):
            with self.subTest(failure=failure):
                self.run.side_effect = failure
                self.assertEqual(hyprland_signature(), "")
                with patch.dict("os.environ", {"XDG_CURRENT_DESKTOP": "Hyprland"}):
                    with self.assertRaisesRegex(RuntimeError, "Could not discover"):
                        hyprland_signature()

    def test_menu_launch_passes_recovered_session_to_fusion_and_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = Settings(home, home / "prefix", home / "runtime", home / "cache",
                                home / "logs", home / "python", home / "config.json")
            self.environment["DISPLAY"] = ":0"
            self.run.side_effect = [self.run.return_value, subprocess.CompletedProcess([], 0)]
            process = Mock(pid=12345)
            process.wait.return_value = 0
            popen = self.enterContext(patch("internal.runtime.subprocess.Popen", return_value=process))
            self.enterContext(patch("internal.setup.host_check"))
            self.enterContext(patch("internal.runtime.check_runtime"))
            self.enterContext(patch("internal.runtime.find_executable", return_value=home / "Fusion360.exe"))
            self.enterContext(patch("internal.runtime.check_shader_compiler"))
            self.enterContext(patch("internal.runtime.prepare_preferences"))
            self.enterContext(patch("builtins.print"))
            self.assertEqual(launch(settings), 0)
            self.assertEqual(popen.call_count, 2)
            fusion, helper = popen.call_args_list
            self.assertEqual(helper.args[0], [
                settings.python, ROOT / "internal/helpers/windows.py", "--pid", "12345",
            ])
            for invocation in (fusion, helper):
                self.assertEqual(invocation.kwargs["env"]["HYPRLAND_INSTANCE_SIGNATURE"], "desktop")
                self.assertEqual(invocation.kwargs["env"]["WINEPREFIX"], str(settings.prefix))
                self.assertNotIn("WAYLAND_DISPLAY", invocation.kwargs["env"])
            self.assertNotIn("HYPRLAND_INSTANCE_SIGNATURE", self.environment)

    def test_malformed_instance_list(self):
        self.run.return_value.stdout = "invalid json"
        self.assertEqual(hyprland_signature(), "")
        self.set_instances({"invalid": "object"})
        self.assertEqual(hyprland_signature(), "")


if __name__ == "__main__":
    unittest.main()
