"""Send to Bambu Studio: add-in installation, environment hand-off, and launch wrapper."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from internal.helpers import bambu_studio
from internal.runtime import (ADDIN_MARKER, ROOT, Settings, addin_fingerprint, brand_addin_icons,
                              install_addins)

WRAPPER = ROOT / "internal/helpers/bambu_studio.py"
ADDIN = ROOT / "internal/addins/SendToBambuStudio"


def load_addin():
    """Import the add-in with stand-ins for Fusion's adsk modules."""
    adsk = types.ModuleType("adsk")
    adsk.core = types.SimpleNamespace(CommandEventHandler=object, CommandCreatedEventHandler=object)
    adsk.fusion = types.SimpleNamespace()
    with patch.dict(sys.modules, {"adsk": adsk, "adsk.core": adsk.core, "adsk.fusion": adsk.fusion}):
        spec = importlib.util.spec_from_file_location("send_to_bambu", ADDIN / "SendToBambuStudio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class AddinInstallTests(unittest.TestCase):
    def setUp(self):
        self.prefix = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addins = self.prefix / "drive_c/users/me/AppData/Roaming/Autodesk/Autodesk Fusion 360/API/AddIns"
        (self.prefix / "drive_c/users/Public/AppData/Roaming").mkdir(parents=True)
        self.addins.mkdir(parents=True)
        # Keep these tests independent of the host's Bambu Studio installation.
        self.enterContext(patch("internal.helpers.bambu_studio.icon_source", return_value=None))

    def test_installs_and_replaces_managed_copy_when_it_changes(self):
        install_addins(self.prefix)
        target = self.addins / "SendToBambuStudio"
        self.assertTrue((target / "SendToBambuStudio.manifest").is_file())
        self.assertEqual((target / ADDIN_MARKER).read_text(), addin_fingerprint(ADDIN))
        (target / "stale.py").write_text("")
        install_addins(self.prefix)
        self.assertTrue((target / "stale.py").exists(), "an unchanged add-in is not copied again")
        with patch("internal.runtime.addin_fingerprint", return_value="newer"):
            install_addins(self.prefix)
        self.assertFalse((target / "stale.py").exists())
        self.assertEqual([p.name for p in self.addins.iterdir()], ["SendToBambuStudio"])

    def test_fingerprint_follows_the_logo(self):
        logo = Path(self.enterContext(tempfile.TemporaryDirectory())) / "logo.svg"
        logo.write_text("<svg/>")
        before = addin_fingerprint(ADDIN, logo)
        self.assertNotEqual(before, addin_fingerprint(ADDIN))
        os.utime(logo, ns=(0, 0))
        self.assertNotEqual(before, addin_fingerprint(ADDIN, logo))

    def test_failure_warns_without_stopping_the_launch(self):
        with patch("internal.runtime.shutil.copytree", side_effect=OSError("disk full")), \
             patch("builtins.print") as printed:
            install_addins(self.prefix)
        self.assertIn("disk full", printed.call_args.args[0])
        self.assertEqual(list(self.addins.iterdir()), [])

    def test_user_folder_of_same_name_is_left_alone(self):
        target = self.addins / "SendToBambuStudio"
        target.mkdir()
        (target / "mine.py").write_text("user code")
        with patch("builtins.print") as printed:
            install_addins(self.prefix)
        self.assertEqual([p.name for p in target.iterdir()], ["mine.py"])
        self.assertIn("not managed", printed.call_args.args[0])


class EnvironmentTests(unittest.TestCase):
    def env(self, environment, **kwargs):
        home = Path("/data")
        settings = Settings(home, home / "prefix", home / "runtime", home / "cache",
                            home / "logs", home / "python", home / "config.json")
        with patch.dict("os.environ", environment, clear=True):
            return settings.env(**kwargs)

    def test_fusion_gets_wrapper_export_dir_and_the_values_it_replaced(self):
        env = self.env({"WAYLAND_DISPLAY": "wayland-1", "__GL_MaxFramesAllowed": "3", "HOME": "/home/me"},
                       fusion=True)
        self.assertEqual(env["FUSION_WINE_SLICER_LAUNCHER"], str(WRAPPER))
        self.assertEqual(env["FUSION_WINE_EXPORT_DIR"], "/data/exports")
        self.assertNotIn("WAYLAND_DISPLAY", env)
        original = json.loads(env["FUSION_WINE_HOST_ENV"])
        self.assertEqual(original["WAYLAND_DISPLAY"], "wayland-1")
        self.assertIsNone(original["WINEPREFIX"])
        self.assertNotIn("HOME", original)
        self.assertNotIn("__GL_MaxFramesAllowed", original, "an explicit user value is unchanged")

    def test_setup_tools_do_not(self):
        env = self.env({"WAYLAND_DISPLAY": "wayland-1"})
        self.assertNotIn("FUSION_WINE_SLICER_LAUNCHER", env)
        self.assertNotIn("FUSION_WINE_HOST_ENV", env)

    def test_launcher_import_path_stays_out_of_wine(self):
        self.assertNotIn("PYTHONPATH", self.env({"PYTHONPATH": str(ROOT)}))
        self.assertEqual(self.env({"PYTHONPATH": f"{ROOT}:/mine"}, fusion=True)["PYTHONPATH"], "/mine")

    def test_wrapper_restores_the_session_environment(self):
        host = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "HOME": "/home/me", "PYTHONPATH": "/mine",
                "WINEDEBUG": "+relay", "__GL_SYNC_TO_VBLANK": "0", "DXVK_HUD": "fps", "FUSION_BAMBU_STUDIO": "x"}
        fusion = self.env({**host, "PYTHONPATH": f"{ROOT}:/mine"}, fusion=True, hyprland="sig")
        # Wine's loader adds variables of its own to Fusion's Unix environment.
        fusion.update(WINELOADERNOEXEC="1", WINEPRELOADRESERVE="0-1")
        # Unrecorded WINE* variables go too; they only affect Wine.
        expected = {k: v for k, v in host.items() if k != "WINEDEBUG"}
        self.assertEqual(bambu_studio.host_environment(fusion), expected)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def desktop(self, text, name="BambuStudio.desktop", base="home"):
        path = self.directory / base / "applications" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\nName=Bambu Studio\n" + text)
        return {"XDG_DATA_HOME": str(self.directory / "home"),
                "XDG_DATA_DIRS": str(self.directory / "system"), "PATH": "/nonexistent"}

    def test_without_a_launcher_record_wine_state_is_stripped(self):
        for record in ({}, {"FUSION_WINE_HOST_ENV": "not json"}, {"FUSION_WINE_HOST_ENV": "[]"}):
            env = bambu_studio.host_environment({
                **record, "WINEPREFIX": "/prefix", "DXVK_CONFIG_FILE": "/c", "__GL_MaxFramesAllowed": "1",
                "QTWEBENGINE_DISABLE_SANDBOX": "1", "FUSION_WINE_EXPORT_DIR": "/exports",
                "DISPLAY": ":0", "HOME": "/home/me", "FUSION_BAMBU_STUDIO": "kept",
            })
            self.assertEqual(env, {"DISPLAY": ":0", "HOME": "/home/me", "FUSION_BAMBU_STUDIO": "kept"})

    def test_x11_only_host_stays_x11(self):
        record = json.dumps({"WINEPREFIX": None})
        env = bambu_studio.host_environment({"DISPLAY": ":0", "WINEPREFIX": "/p", "FUSION_WINE_HOST_ENV": record})
        self.assertEqual(env, {"DISPLAY": ":0"})

    def test_user_menu_entry_is_used(self):
        env = self.desktop("Exec=/usr/bin/env SSL_CERT_FILE=/etc/ca.crt /usr/bin/bambu-studio %U\n")
        self.desktop("Exec=/usr/bin/other %U\n", base="system")
        self.assertEqual(bambu_studio.command(["/x/a b.stl", "/x/c.stl"], env), [
            "/usr/bin/env", "SSL_CERT_FILE=/etc/ca.crt", "/usr/bin/bambu-studio", "/x/a b.stl", "/x/c.stl"])

    def test_flatpak_forwarding_markers_are_kept(self):
        self.assertEqual(bambu_studio.entry_command(
            "/usr/bin/flatpak run --file-forwarding com.bambulab.BambuStudio @@u %U @@", ["/x/a.stl"]),
            ["/usr/bin/flatpak", "run", "--file-forwarding", "com.bambulab.BambuStudio", "@@u", "/x/a.stl", "@@"])

    def test_single_file_codes_and_missing_codes(self):
        self.assertEqual(bambu_studio.entry_command("bambu-studio %f", ["a", "b"]), ["bambu-studio", "a"])
        self.assertEqual(bambu_studio.entry_command('"/opt/Bambu Studio/run" --x %i', ["a"]),
                         ["/opt/Bambu Studio/run", "--x", "a"])

    def test_running_instance_receives_files_over_dbus(self):
        listing = subprocess.CompletedProcess([], 0, stdout=(
            'array [\n  string "org.freedesktop.DBus"\n'
            '  string "com.BambuLab.BambuStudio.InstanceCheck.Object1234"\n]\n'))
        with patch("internal.helpers.bambu_studio.subprocess.run",
                   side_effect=[listing, subprocess.CompletedProcess([], 0, stderr="")]) as run:
            self.assertTrue(bambu_studio.send_to_running_instance(['/x/a b.stl', '/x/"q".stl'], {}))
        name = "com.BambuLab.BambuStudio.InstanceCheck.Object1234"
        self.assertEqual(run.call_args.args[0], [
            "dbus-send", "--session", "--type=method_call", f"--dest={name}",
            "/com/BambuLab/BambuStudio/InstanceCheck/Object1234", f"{name}.AnotherInstance",
            'string:"bambu-studio";"/x/a b.stl";"/x/\\"q\\".stl"'])

    def test_no_running_instance(self):
        listing = subprocess.CompletedProcess([], 0, stdout='array [ string "org.freedesktop.DBus" ]')
        with patch("internal.helpers.bambu_studio.subprocess.run", return_value=listing) as run:
            self.assertFalse(bambu_studio.send_to_running_instance(["/x/a.stl"], {}))
        run.assert_called_once()
        with patch("internal.helpers.bambu_studio.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(bambu_studio.send_to_running_instance(["/x/a.stl"], {}))

    def test_override_and_missing_install(self):
        env = self.desktop("Exec=/usr/bin/bambu-studio %U\n")
        self.assertEqual(bambu_studio.command(["a"], {**env, "FUSION_BAMBU_STUDIO": "my-slicer --flag"}),
                         ["my-slicer", "--flag", "a"])
        empty = {"XDG_DATA_HOME": str(self.directory / "none"), "XDG_DATA_DIRS": str(self.directory / "none"),
                 "PATH": str(self.directory)}
        self.assertIsNone(bambu_studio.command(["a"], empty))

    def test_icon_prefers_svg_then_largest_png(self):
        env = self.desktop("Exec=bambu-studio %U\nIcon=BambuStudio\n")
        theme = self.directory / "system/icons/hicolor"
        # Unrecognised directory names must not stop the launcher.
        for size in ("32x32", "192x192", "xx", "64x64@2"):
            (theme / size / "apps").mkdir(parents=True)
            (theme / size / "apps/BambuStudio.png").write_bytes(b"")
        self.assertEqual(bambu_studio.icon_source(env), theme / "192x192/apps/BambuStudio.png")
        (theme / "scalable/apps").mkdir(parents=True)
        (theme / "scalable/apps/BambuStudio.svg").write_text("<svg/>")
        self.assertEqual(bambu_studio.icon_source(env), theme / "scalable/apps/BambuStudio.svg")

    def test_wrapper_execs_with_clean_environment(self):
        fake = self.directory / "fake-slicer"
        out = self.directory / "out"
        fake.write_text(f'#!/bin/sh\n{{ printf "%s\\n" "$@"; env; }} > {out}\n')
        fake.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "HOME": "/home/me", "WINEPREFIX": "/p", "FUSION_BAMBU_STUDIO": str(fake),
               "FUSION_WINE_HOST_ENV": json.dumps({"WAYLAND_DISPLAY": "wayland-1", "WINEPREFIX": None})}
        subprocess.run([WRAPPER, "/exports/a b.stl"], env=env, check=True, timeout=10)
        lines = out.read_text().splitlines()
        self.assertEqual(lines[0], "/exports/a b.stl")
        self.assertIn("WAYLAND_DISPLAY=wayland-1", lines)
        self.assertFalse(any(line.startswith(("WINE", "FUSION_WINE_")) for line in lines))


class BrandedIconTests(unittest.TestCase):
    def test_rendered_logo_replaces_generic_icons(self):
        resources = Path(self.enterContext(tempfile.TemporaryDirectory())) / "resources"
        resources.mkdir()
        (resources / "16x16.png").write_bytes(b"generic")
        render = lambda source, size, path: path.write_bytes(str(size).encode()) or True
        with patch("internal.helpers.bambu_studio.render_icon", side_effect=render):
            brand_addin_icons(resources, Path("/logo.svg"))
        self.assertEqual({p.name: p.read_bytes() for p in resources.iterdir()},
                         {"16x16.png": b"16", "16x16@2x.png": b"32", "32x32.png": b"32", "32x32@2x.png": b"64"})

    def test_generic_icons_kept_when_rendering_fails(self):
        resources = Path(self.enterContext(tempfile.TemporaryDirectory())) / "resources"
        resources.mkdir()
        (resources / "16x16.png").write_bytes(b"generic")
        with patch("internal.helpers.bambu_studio.render_icon", return_value=False):
            brand_addin_icons(resources, Path("/logo.svg"))
        self.assertEqual([p.name for p in resources.parent.iterdir()], ["resources"])
        self.assertEqual((resources / "16x16.png").read_bytes(), b"generic")


class AddinHelperTests(unittest.TestCase):
    def setUp(self):
        self.addin = load_addin()

    def test_unix_paths_map_to_drive_z(self):
        self.assertEqual(self.addin.windows_path("/data/exports/a.3mf"), "Z:\\data\\exports\\a.3mf")

    def test_names_are_safe_file_names(self):
        self.assertEqual(self.addin.safe_name("Bracket v3 (final)"), "Bracket_v3_final")
        self.assertEqual(self.addin.safe_name("../.."), "design")
        self.assertEqual(self.addin.safe_name(""), "design")

    def test_command_line_follows_windows_quoting(self):
        self.assertEqual(self.addin.command_line("Z:\\a b\\x.py", "/x/a.stl", "/x/c d.stl"),
                         '"Z:\\a b\\x.py" /x/a.stl "/x/c d.stl"')
        self.assertEqual(self.addin.command_line('/x/"q".stl', "/x/end\\ dir\\"),
                         '/x/\\"q\\".stl "/x/end\\ dir\\\\"')

    def test_selection_names_get_distinct_file_stems(self):
        self.assertEqual(self.addin.unique_stems(["Body1", "Body1", "Body 1", "Body1"]),
                         ["Body1", "Body1_2", "Body_1", "Body1_3"])

    def test_old_exports_and_loose_files_are_removed(self):
        exports = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("old", "new"):
            (exports / name).mkdir()
            (exports / name / "a.stl").write_bytes(b"")
            (exports / (name + ".3mf")).write_bytes(b"")
        for name in ("old", "old.3mf"):
            os.utime(exports / name, (0, 0))
        self.addin.remove_old_exports(str(exports))
        self.assertEqual(sorted(p.name for p in exports.iterdir()), ["new", "new.3mf"])

    def test_selection_maps_faces_to_bodies_and_skips_duplicates(self):
        fusion = self.addin.adsk.fusion

        def kind(name):
            return type(name, (), {"cast": classmethod(lambda cls, e: e if isinstance(e, cls) else None)})
        for name in ("BRepFace", "BRepEdge", "BRepVertex", "BRepBody", "Occurrence", "Component", "Sketch"):
            setattr(fusion, name, kind(name))

        def make(cls, **values):
            item = cls()
            item.__dict__.update(values)
            return item
        body = make(fusion.BRepBody, name="Bracket", entityToken="b1")
        component = make(fusion.Component, name="Wheel", entityToken="c1")
        occurrence = make(fusion.Occurrence, component=component, entityToken="o1")
        selected = [make(fusion.BRepFace, body=body), body, occurrence, make(fusion.Sketch)]
        selections = types.SimpleNamespace(count=len(selected),
                                           item=lambda i: types.SimpleNamespace(entity=selected[i]))
        app = types.SimpleNamespace(userInterface=types.SimpleNamespace(activeSelections=selections),
                                    activeDocument=types.SimpleNamespace(name="Assembly v3"))
        design = types.SimpleNamespace(rootComponent="root")
        self.assertEqual(self.addin.export_targets(app, design), [(body, "Bracket"), (occurrence, "Wheel")])
        selections.count = 0
        self.assertEqual(self.addin.export_targets(app, design), [("root", "Assembly v3")])


if __name__ == "__main__":
    unittest.main()
