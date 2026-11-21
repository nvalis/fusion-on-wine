#!/usr/bin/env python3
"""Start the native Bambu Studio for the Send to Bambu Studio Fusion add-in.

Wine runs this file natively when the add-in starts it, with Fusion's Unix
environment: Wine, DXVK, and launcher settings that must not reach a native
application, and no Wayland socket. The launcher records the desktop
session's values it replaced, and this wrapper puts them back. The launcher
also imports this module to find Bambu Studio and its icon. Standard library only.
"""
import configparser
import datetime
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

DESKTOP_ENTRIES = ("BambuStudio.desktop", "com.bambulab.BambuStudio.desktop", "bambu-studio.desktop")
PRIVATE_PREFIXES = ("WINE", "DXVK_", "FUSION_WINE_", "QTWEBENGINE_", "__GL_")
FIELD_CODES = {"%f", "%F", "%u", "%U"}
# A running Bambu Studio owns this name with its executable's path hash appended
# and loads the files another instance sends to AnotherInstance. Bambu Studio's
# --single-instance option is rejected by its own argument parser, so the
# message is sent here directly.
INSTANCE_NAME = re.compile(r'"(com\.BambuLab\.BambuStudio\.InstanceCheck\.Object\d+)"')


def host_environment(env):
    """The desktop session's environment, as it was before the launcher prepared Wine."""
    try:
        original = json.loads(env.get("FUSION_WINE_HOST_ENV", ""))
    except ValueError:
        original = None
    if not isinstance(original, dict):
        # Not started from a launcher-managed Fusion: drop what such setups add.
        return {k: v for k, v in env.items() if not k.startswith(PRIVATE_PREFIXES)}
    # Wine's loader adds WINE* variables of its own; drop any the launcher did not record.
    clean = {k: v for k, v in env.items() if k not in original and not k.startswith(("WINE", "FUSION_WINE_"))}
    clean.update((k, v) for k, v in original.items() if isinstance(v, str))
    return clean


def data_dirs(env):
    home = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return [Path(home), *(Path(p) for p in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p)]


def desktop_entry(env):
    """The user's Bambu Studio menu entry, so launches match the menu exactly."""
    for directory in data_dirs(env):
        for name in DESKTOP_ENTRIES:
            path = directory / "applications" / name
            if not path.is_file():
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if entry.get("Exec") and entry.get("Hidden", "false") != "true":
                return entry
    return None


def entry_command(exec_line, files):
    """Expand a desktop Exec line's field codes."""
    tokens = [t.replace("%%", "%") for t in shlex.split(exec_line)
              if t not in ("%i", "%c", "%k")]
    position = next((i for i, t in enumerate(tokens) if t in FIELD_CODES), None)
    if position is None:
        return [*tokens, *files]
    expanded = files[:1] if tokens[position] in ("%f", "%u") else files
    return [*tokens[:position], *expanded, *tokens[position + 1:]]


def command(files, env):
    if env.get("FUSION_BAMBU_STUDIO"):
        return [*shlex.split(env["FUSION_BAMBU_STUDIO"]), *files]
    entry = desktop_entry(env)
    if entry:
        return entry_command(entry["Exec"], files)
    program = shutil.which("bambu-studio", path=env.get("PATH"))
    return [program, *files] if program else None


def instance_message(files):
    """Bambu Studio's escape_strings_cstyle list: quoted, semicolon-separated, own path first."""
    quoted = ('"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' for a in ("bambu-studio", *files))
    return ";".join(quoted)


def running_instance(env):
    try:
        listing = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    match = INSTANCE_NAME.search(listing.stdout)
    return match.group(1) if match else None


def log(message):
    directory = os.environ.get("FUSION_WINE_LOG_DIR")
    if not directory:
        return
    try:
        with open(os.path.join(directory, "bambu-studio-addin.log"), "a", encoding="utf-8") as file:
            file.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} wrapper: {message}\n")
    except OSError:
        pass


def send_to_running_instance(files, env):
    """Add the files to an open Bambu Studio; False when none is running."""
    name = running_instance(env)
    if not name:
        log("no running Bambu Studio on the session bus")
        return False
    path = "/" + name.replace(".", "/")
    message = instance_message(files)
    try:
        result = subprocess.run(
            ["dbus-send", "--session", "--type=method_call", f"--dest={name}", path,
             f"{name}.AnotherInstance", f"string:{message}"],
            capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"dbus-send failed: {error}")
        return False
    log(f"sent to {name}: exit {result.returncode} {result.stderr.strip()}")
    return result.returncode == 0


def icon_size(path):
    """Width from a hicolor directory such as 256x256 or 256x256@2; 0 if unrecognised."""
    match = re.match(r"(\d+)x\d+", path.parts[-3])
    return int(match.group(1)) if match else 0


def icon_source(env):
    """Bambu Studio's installed icon from the hicolor theme, preferring SVG."""
    entry = desktop_entry(env)
    name = entry.get("Icon", "") if entry else ""
    if not name:
        return None
    if os.path.isabs(name):
        return Path(name) if Path(name).is_file() else None
    for directory in data_dirs(env):
        theme = directory / "icons/hicolor"
        svg = theme / "scalable/apps" / (name + ".svg")
        if svg.is_file():
            return svg
        sizes = sorted(theme.glob(f"*x*/apps/{name}.png"), key=icon_size, reverse=True)
        if sizes:
            return sizes[0]
    pixmap = Path("/usr/share/pixmaps") / (name + ".png")
    return pixmap if pixmap.is_file() else None


def render_icon(source, size, destination):
    """Render with whichever converter the host has; return False to keep the bundled icon."""
    if source.suffix == ".svg" and shutil.which("rsvg-convert"):
        arguments = ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(destination), str(source)]
    elif shutil.which("magick"):
        arguments = ["magick", "-background", "none", str(source), "-resize", f"{size}x{size}", str(destination)]
    else:
        return False
    try:
        return subprocess.run(arguments, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify(message, env):
    print(message, file=sys.stderr)
    if shutil.which("notify-send", path=env.get("PATH")):
        subprocess.run(["notify-send", "Send to Bambu Studio", message], env=env, check=False)


def main(files):
    env = host_environment(os.environ)
    if not env.get("FUSION_BAMBU_STUDIO") and send_to_running_instance(files, env):
        return 0
    arguments = command(files, env)
    if not arguments:
        notify("Bambu Studio was not found. Install it or set FUSION_BAMBU_STUDIO.", env)
        return 127
    program = shutil.which(arguments[0], path=env.get("PATH")) or arguments[0]
    log("starting " + shlex.join(arguments))
    try:
        os.execve(program, arguments, env)
    except OSError as error:
        notify(f"Could not start Bambu Studio: {error}", env)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
