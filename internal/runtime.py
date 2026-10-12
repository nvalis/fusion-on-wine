"""Launch, configuration, desktop integration, and read-only diagnostics."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


# Paths and environment

ROOT = Path(__file__).resolve().parent.parent
FUSION_DLL_OVERRIDES = "d3dcompiler_47,d3dcompiler_43=n,b"


def manifest():
    return json.loads((ROOT / "internal/versions.json").read_text())


def xdg(variable, default):
    return Path(os.environ.get(variable, str(Path.home() / default))).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    home: Path
    prefix: Path
    runtime: Path
    cache: Path
    logs: Path
    python: Path
    config: Path
    previous_callback: str = ""

    @property
    def wine(self):
        return self.runtime / "bin/wine"

    @property
    def server(self):
        return self.runtime / "bin/wineserver"

    def env(self, *, fusion=False):
        env = dict(os.environ)
        env.update(WINEPREFIX=str(self.prefix), WINESERVER=str(self.server), WINEARCH="win64",
                   WINEDEBUG=env.get("WINEDEBUG", "-all,+err"), QTWEBENGINE_DISABLE_SANDBOX="1",
                   QTWEBENGINE_CHROMIUM_FLAGS=env.get("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox"),
                   FUSION_WINE_LOG_DIR=str(self.logs))
        env.pop("WAYLAND_DISPLAY", None)
        # Never let a caller's system-Wine overrides mix libraries with this runtime.
        env.pop("WINEDLLPATH", None)
        env.pop("WINELOADER", None)
        env.pop("WINEDLLOVERRIDES", None)
        if fusion:
            # Fusion ships its own compilers. Wine's compiler breaks OGS shading and redraws.
            env["WINEDLLOVERRIDES"] = FUSION_DLL_OVERRIDES
        return env


def load_settings(*, installing=False, home=None):
    config = xdg("XDG_CONFIG_HOME", ".config") / "fusion-on-wine/config.json"
    saved = json.loads(config.read_text()) if config.exists() else {}
    default_home = xdg("XDG_DATA_HOME", ".local/share") / "fusion-on-wine"
    home = Path(home or os.environ.get("FUSION_WINE_HOME", saved.get("home", default_home))).expanduser().resolve()
    if home != Path(saved.get("home", home)).expanduser().resolve():
        saved = {}
    prefix = Path(os.environ.get("FUSION_WINE_PREFIX", saved.get("prefix", home / "prefix"))).expanduser().resolve()
    runtime = Path(os.environ.get("FUSION_WINE_RUNTIME", saved.get("runtime", home / "runtime" / ("wine-" + manifest()["wine"]["version"])))).expanduser().resolve()
    logs = xdg("XDG_STATE_HOME", ".local/state") / "fusion-on-wine/logs"
    # Preserve the original local setup when this launcher replaces its old shell script.
    # Fresh clones have neither of these ignored directories and use XDG paths instead.
    legacy_prefix = Path.home() / ".local/share/wineprefixes/fusion"
    if not installing and not saved and not any(k in os.environ for k in ("FUSION_WINE_HOME", "FUSION_WINE_PREFIX", "FUSION_WINE_RUNTIME")):
        if (ROOT / "wine-runner/bin/wine").is_file() and legacy_prefix.is_dir():
            prefix, runtime, logs = legacy_prefix, ROOT / "wine-runner", ROOT / "logs"
    python = home / "python/bin/python"
    if not python.is_file():
        python = Path(sys.executable)
    return Settings(home, prefix, runtime, home / "cache", logs, python, config, saved.get("previous_callback", ""))


def save_settings(settings, previous_callback=""):
    settings.config.parent.mkdir(parents=True, exist_ok=True)
    contents = {"home": str(settings.home), "prefix": str(settings.prefix), "runtime": str(settings.runtime),
                "previous_callback": previous_callback or settings.previous_callback}
    temp = settings.config.with_suffix(".tmp")
    temp.write_text(json.dumps(contents, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(settings.config)


def find_executable(prefix, name="Fusion360.exe"):
    pattern = f"drive_c/users/*/AppData/Local/Autodesk/webdeploy/production/*/{name}"
    if name != "Fusion360.exe":
        pattern = f"drive_c/users/*/AppData/Local/Autodesk/webdeploy/production/*/**/{name}"
    files = list(prefix.glob(pattern))
    if not files and name != "Fusion360.exe":
        for folder in (prefix / "drive_c/Program Files/Autodesk", prefix / "drive_c/Program Files (x86)/Autodesk"):
            if folder.is_dir():
                files.extend(folder.rglob(name))
    if not files:
        raise RuntimeError(f"{name} is not installed. Run ./install.sh first.")
    return max(files, key=lambda p: p.stat().st_mtime)


def fusion_version(executable):
    path = executable.parent / "version.json"
    try:
        data = json.loads(path.read_text())
        return ".".join(str(data[k]) for k in ("major", "minor", "build"))
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def active_prefix_processes(prefix):
    """Do not alter a prefix while its applications or background services run."""
    prefix = prefix.resolve()
    result = []
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if int(process.name) == os.getpid():
                continue
            environment = (process / "environ").read_bytes().split(b"\0")
            value = next((item[len(b"WINEPREFIX="):] for item in environment if item.startswith(b"WINEPREFIX=")), None)
            if value is not None and Path(os.fsdecode(value)).resolve() == prefix:
                command = (process / "comm").read_text().strip().lower()
                executable = os.fsdecode((process / "cmdline").read_bytes().split(b"\0")[0]).lower()
                if command.startswith("wine") or command.endswith(".exe") or executable.endswith(".exe"):
                    result.append(int(process.name))
        except (OSError, ValueError):
            pass
    return result

# Desktop entries

APP_ENTRY = "fusion-on-wine.desktop"
CALLBACK_ENTRY = "fusion-on-wine-adskidmgr.desktop"
SCHEME = "x-scheme-handler/adskidmgr"


def desktop_argument(value):
    # Desktop-entry Exec quoting is not shell quoting. Percent signs are field codes.
    value = str(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def install_entries(settings):
    directory = xdg("XDG_DATA_HOME", ".local/share") / "applications"
    directory.mkdir(parents=True, exist_ok=True)
    command = desktop_argument(ROOT / "fusion.sh")
    (directory / APP_ENTRY).write_text(
        "[Desktop Entry]\nType=Application\nName=Autodesk Fusion (Wine)\n"
        "Comment=Unofficial Fusion launcher with a private Wine runtime\n"
        f"Exec={command} launch\nIcon=applications-engineering\nTerminal=false\n"
        "Categories=Graphics;Engineering;\nStartupWMClass=fusion360.exe\n")
    (directory / CALLBACK_ENTRY).write_text(
        "[Desktop Entry]\nType=Application\nName=Fusion Autodesk sign-in callback\n"
        f"Exec={command} callback %u\nNoDisplay=true\nTerminal=false\nMimeType={SCHEME};\n")
    old = subprocess.run(["xdg-mime", "query", "default", SCHEME], capture_output=True, text=True)
    previous = old.stdout.strip() if old.returncode == 0 else ""
    if previous == CALLBACK_ENTRY:
        previous = settings.previous_callback
    save_settings(settings, previous)
    subprocess.run(["xdg-mime", "default", CALLBACK_ENTRY, SCHEME], check=True)
    subprocess.run(["update-desktop-database", str(directory)], check=False)


def uninstall_entries(settings):
    directory = xdg("XDG_DATA_HOME", ".local/share") / "applications"
    current = subprocess.run(["xdg-mime", "query", "default", SCHEME], capture_output=True, text=True)
    if current.stdout.strip() == CALLBACK_ENTRY and settings.previous_callback:
        subprocess.run(["xdg-mime", "default", settings.previous_callback, SCHEME], check=True)
    for name in (APP_ENTRY, CALLBACK_ENTRY):
        (directory / name).unlink(missing_ok=True)
    subprocess.run(["update-desktop-database", str(directory)], check=False)
    # Prefixes, models, credentials, runtime, and configuration are deliberately kept.
    print("Removed this project's desktop entries. Application data was not deleted.")
    print("Preserved data directory:", settings.home)

# First-install preferences

def decode_xml(data):
    encoding = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be" if data.startswith(b"\xfe\xff") else "utf-8"
    return encoding, data.decode(encoding)


def replace_values(path, values):
    data = path.read_bytes()
    encoding, text = decode_xml(data)
    changed = text
    for tag, value in values.items():
        pattern = re.compile(rf'(<{tag}\b[^>]*\bValue=")[^"]*(")')
        if len(pattern.findall(changed)) != 1:
            raise RuntimeError(f"Cannot safely update {tag} in {path.name}; its XML format differs.")
        changed = pattern.sub(lambda m: m.group(1) + value + m.group(2), changed)
    if changed == text:
        return
    backup = path.with_name(path.name + ".before-fusion-on-wine")
    if not backup.exists():
        backup.write_bytes(data)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(changed.encode(encoding))
    temp.replace(path)


def replace_value(path, tag, value):
    replace_values(path, {tag: value})


def prepare_preferences(prefix, *, force_graphics=False):
    homes = list(prefix.glob("drive_c/users/*/AppData/Roaming"))
    homes = [p for p in homes if p.parents[1].name.lower() not in ("public", "default", "default user")]
    if len(homes) != 1:
        raise RuntimeError("Could not identify a single Wine user profile for graphics configuration.")
    options = homes[0] / "Autodesk/Neutron Platform/Options"
    options.mkdir(parents=True, exist_ok=True)
    machine = options / "NMachineSpecificOptions.xml"
    if not machine.exists():
        text = ('<?xml version="1.0" encoding="UTF-16"?>\r\n<OptionGroups>\r\n'
                '  <BootstrapOptionsGroup><driverOptionId Value="VirtualDeviceDx11"/></BootstrapOptionsGroup>\r\n'
                '  <CompatibilityGroup><graphicsApiOptionId Value="D3D11"/></CompatibilityGroup>\r\n'
                '</OptionGroups>\r\n')
        machine.write_bytes(text.encode("utf-16"))
    elif force_graphics:
        replace_values(machine, {"driverOptionId": "VirtualDeviceDx11", "graphicsApiOptionId": "D3D11"})
    # Account settings appear only after sign-in. Apply this default once per account.
    for path in options.glob("*/NGlobalOptions.xml"):
        marker = path.parent / ".fusion-on-wine-environment-initialized"
        if marker.exists():
            continue
        try:
            root = ET.parse(path).getroot()
            if any(e.tag == "EnvironmentIDId" for e in root.iter()):
                replace_value(path, "EnvironmentIDId", "PhotoBooth")
                marker.touch()
        except (ET.ParseError, RuntimeError) as error:
            print(f"Environment default skipped: {error}")

def bundled_shader_compiler(executable):
    for folder in (executable.parent, executable.parent / "d3dcompiler_v2", executable.parent / "d3dcompiler_v1"):
        if folder.is_dir():
            for path in folder.iterdir():
                if path.name.lower() == "d3dcompiler_47.dll" and path.is_file():
                    return path
    raise RuntimeError("Fusion's bundled d3dcompiler_47.dll is missing. Repair the Autodesk installation; "
                       "Wine's shader compiler is not a working replacement.")

# Commands

@contextmanager
def installation_guard(settings):
    lock_path = settings.home / "install.lock"
    lock = lock_path.open("r") if lock_path.exists() else None
    try:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Installation is in progress; wait before changing settings or launching Fusion.") from None
        yield
    finally:
        if lock is not None:
            lock.close()


def check_runtime(settings):
    from .setup import fingerprint
    if not settings.wine.is_file() or not settings.server.is_file():
        raise RuntimeError("Private Wine is missing. Run ./install.sh.")
    version = subprocess.check_output([settings.wine, "--version"], text=True).strip()
    expected = "wine-" + manifest()["wine"]["version"]
    if version != expected:
        raise RuntimeError(f"Expected {expected}, found {version}. Do not mix runtime and driver versions.")
    marker = settings.runtime / ".fusion-on-wine-build.json"
    if marker.exists() and json.loads(marker.read_text()) != fingerprint(manifest()):
        raise RuntimeError("The runtime's patches differ from this checkout. Close Fusion and rerun installation.")


def launch(settings):
    from .setup import host_check
    host_check()
    check_runtime(settings)
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("No X11/XWayland display is available. Launch from your graphical desktop.")
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        if shutil.which("hyprctl") is None:
            raise RuntimeError("Hyprland's hyprctl is missing.")
        if subprocess.run([settings.python, "-c", "import Xlib"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            raise RuntimeError("Python Xlib is missing; install the helper environment first.")
    # A running install must not race application startup or runtime replacement.
    with installation_guard(settings):
        executable = find_executable(settings.prefix)
        bundled_shader_compiler(executable)
        prepare_preferences(settings.prefix)
        version = fusion_version(executable)
        if version != manifest()["fusion"]["tested_version"]:
            print(f"Warning: Fusion {version} is not the tested release {manifest()['fusion']['tested_version']}.")
        settings.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.logs.chmod(0o700)
        env = settings.env(fusion=True)
        with (settings.logs / "fusion.log").open("a") as log:
            process = subprocess.Popen([settings.wine, executable], cwd=executable.parent, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            with (settings.logs / "overlays.log").open("a") as log:
                try:
                    subprocess.Popen([settings.python, ROOT / "internal/helpers/windows.py", "--pid", str(process.pid)],
                                     env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                except OSError:
                    print("Warning: could not start the Hyprland helper. Run ./fusion.sh doctor.")
        print("Fusion started. Logs:", settings.logs)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # Do not discard a design merely because its launching terminal was interrupted.
        print("Launcher interrupted; Fusion was not force-stopped. Save through its UI.")
        return 130


def callback(settings, arguments):
    if len(arguments) != 1 or not arguments[0].startswith("adskidmgr:"):
        print("Expected one adskidmgr URL.", file=sys.stderr)
        return 2
    try:
        with installation_guard(settings):
            check_runtime(settings)
            executable = find_executable(settings.prefix, "AdskIdentityManager.exe")
            # Do not use check=True or print exceptions containing this command's URL.
            return subprocess.run([settings.wine, executable, arguments[0]], env=settings.env(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print("Could not start Autodesk Identity Manager. Check the installation with doctor.", file=sys.stderr)
        return 1


def doctor(settings):
    spec = manifest()
    print("Host:", sys.platform, os.uname().machine)
    print("Prefix:", settings.prefix)
    print("Runtime:", settings.runtime)
    print("Logs:", settings.logs)
    problems = []
    try:
        check_runtime(settings)
        print("Wine:", spec["wine"]["version"])
        marker = settings.runtime / ".fusion-on-wine-build.json"
        if not marker.is_file():
            print("Warning: legacy/manual runtime; this installer has no build record for it.")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        problems.append(str(error))
    try:
        executable = find_executable(settings.prefix)
        version = fusion_version(executable)
        print("Fusion:", version, "(tested: " + spec["fusion"]["tested_version"] + ")")
        print("Bundled shader compiler:", bundled_shader_compiler(executable))
        print("Fusion DLL overrides:", FUSION_DLL_OVERRIDES)
        print("DXVK device filter:", os.environ.get("DXVK_FILTER_DEVICE_NAME", "automatic"))
        if version != spec["fusion"]["tested_version"]:
            print("Warning: unvalidated Fusion release.")
    except RuntimeError as error:
        problems.append(str(error))
    try:
        marker = settings.prefix / ".fusion-on-wine-prefix.json"
        if marker.is_file():
            version = json.loads(marker.read_text()).get("dxvk", "missing")
            print("DXVK:", version, "(pinned: " + spec["dxvk"]["version"] + ")")
            if version != spec["dxvk"]["version"]:
                problems.append("DXVK differs from the pinned version. Save and close Fusion, then rerun installation "
                                "with --upgrade-graphics.")
        else:
            print("Warning: legacy/manual prefix; DXVK version is unrecorded.")
        options = list(settings.prefix.glob("drive_c/users/*/AppData/Roaming/Autodesk/Neutron Platform/Options/NMachineSpecificOptions.xml"))
        for path in options:
            values = {element.tag: element.get("Value") for element in ET.parse(path).getroot().iter()}
            print("Configured viewport:", values.get("driverOptionId", "automatic"))
            print("Configured UI:", values.get("graphicsApiOptionId", "automatic"))
            if values.get("driverOptionId") != "VirtualDeviceDx11" or values.get("graphicsApiOptionId") != "D3D11":
                print("Warning: graphics preferences differ from the all-DirectX defaults. "
                      "Use --upgrade-graphics to migrate an installer-managed prefix.")
    except (OSError, ValueError, ET.ParseError) as error:
        problems.append("Could not read graphics configuration: " + str(error))
    result = subprocess.run([settings.python, "-c", "import Xlib"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        problems.append("Python Xlib is missing. Rerun installation to prepare its private Python environment.")
    print("DISPLAY:", os.environ.get("DISPLAY", "missing"))
    if not os.environ.get("DISPLAY"):
        problems.append("No X11/XWayland display is available in this session.")
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        if shutil.which("hyprctl"):
            result = subprocess.run(["hyprctl", "version"], capture_output=True, text=True)
            print(result.stdout.splitlines()[0] if result.stdout else "Hyprland IPC unavailable")
            monitors = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True)
            try:
                if any(m["scale"] != 1 for m in json.loads(monitors.stdout)):
                    print("Warning: palette geometry compatibility fix is disabled on scaled monitors.")
            except (ValueError, KeyError, TypeError):
                problems.append("Could not query Hyprland monitors.")
        else:
            problems.append("Hyprland session detected but hyprctl is missing.")
    else:
        print("Warning: this compositor is not part of the validated setup; Hyprland fixes will not run.")
    for command in (["glxinfo", "-B"], ["vulkaninfo", "--summary"]):
        if shutil.which(command[0]):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                print(result.stdout.strip() if result.returncode == 0 else command[0] + " failed")
            except subprocess.TimeoutExpired:
                print(command[0] + " timed out")
        else:
            print(command[0] + " not installed; GPU capability report skipped")
    print("Graphics defaults: DirectX 11 viewport and UI; DXVK " + spec["dxvk"]["version"] + ".")
    print("DXVK requires Vulkan " + spec["dxvk"]["minimum_vulkan"] + " or newer.")
    for problem in problems:
        print("ERROR:", problem)
    return bool(problems)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Argparse errors may echo argument contents. Never feed callback URLs to it.
    if argv and argv[0] == "callback":
        try:
            return callback(load_settings(), argv[1:])
        except (OSError, ValueError):
            print("Could not load callback configuration.", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(prog="fusion.sh", description="Unofficial Autodesk Fusion installer and launcher for Wine")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("launch", help="Launch Fusion without changing normal desktop tiling")
    setup = sub.add_parser("install", help="Build private Wine and install Fusion")
    setup.add_argument("--deps", action="store_true", help="Install Arch-based host dependencies through sudo/pacman")
    setup.add_argument("--accept-third-party-licenses", action="store_true", help="Confirm acceptance of applicable vendor licenses")
    setup.add_argument("--data-dir", type=Path, help="Override the user-local application data directory")
    setup.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="Parallel Wine build jobs, default at most 4")
    setup.add_argument("--dry-run", action="store_true", help="Print the plan without downloads or changes")
    setup.add_argument("--refresh-fusion", action="store_true", help="Run Autodesk's current downloader again")
    setup.add_argument("--upgrade-graphics", action="store_true",
                       help="Explicitly upgrade DXVK and select DirectX 11 in an idle, installer-managed prefix")
    sub.add_parser("doctor", help="Report versions, graphics capabilities, and missing dependencies")
    sub.add_parser("desktop", help="Regenerate desktop entries after moving this repository")
    sub.add_parser("uninstall", help="Remove desktop entries while preserving application data")
    sub.add_parser("stop", help="Force-stop ONLY this Wine prefix; save work first")
    args = parser.parse_args(argv)
    command = args.command or "launch"
    try:
        settings = load_settings(installing=command == "install", home=getattr(args, "data_dir", None))
        if command == "install":
            from .setup import install
            if args.jobs < 1:
                raise RuntimeError("--jobs must be at least 1.")
            install(settings, args)
            return 0
        if command == "launch":
            return launch(settings)
        if command == "doctor":
            return int(doctor(settings))
        if command == "desktop":
            install_entries(settings)
        elif command == "uninstall":
            uninstall_entries(settings)
        elif command == "stop":
            check_runtime(settings)
            return subprocess.run([settings.server, "-k"], env=settings.env()).returncode
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("Error:", error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. No application prefix was deleted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
