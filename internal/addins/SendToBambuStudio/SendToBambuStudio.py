"""Fusion add-in: export the design as STL and open it in the native Bambu Studio.

The fusion-on-wine launcher installs this add-in and provides its settings
through the environment. Wine runs the Linux launch wrapper natively.
"""
import ctypes
from ctypes import wintypes
import datetime
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback

import adsk.core
import adsk.fusion

COMMAND_ID = "fusionOnWineSendToBambuStudio"
COMMAND_NAME = "Send to Bambu Studio"
# Utilities > Make holds Fusion's own 3D Print command; fall back to Add-Ins.
PANEL_IDS = ("SolidMakePanel", "ToolsMakePanel", "SolidScriptsAddinsPanel")
EXPORT_LIFETIME = 24 * 60 * 60

handlers = []


def log(message):
    directory = os.environ.get("FUSION_WINE_LOG_DIR")
    if not directory:
        return
    try:
        with open(windows_path(directory) + "\\bambu-studio-addin.log", "a", encoding="utf-8") as file:
            file.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def windows_path(unix_path):
    """Wine maps the Unix root to drive Z:."""
    return "Z:" + unix_path.replace("/", "\\")


def safe_name(name):
    name = re.sub(r"[^\w.-]+", "_", name or "").strip("._")
    return name[:80] or "design"


def export_targets(app, design):
    """The selected bodies, occurrences, and components; otherwise the whole design.

    Clicking a model selects a face by default, so a face, edge, or vertex
    stands for its body. Other selections, such as sketches, are ignored.
    """
    selections = app.userInterface.activeSelections
    targets, seen = [], set()
    for index in range(selections.count):
        entity = selections.item(index).entity
        for kind in (adsk.fusion.BRepFace, adsk.fusion.BRepEdge, adsk.fusion.BRepVertex):
            item = kind.cast(entity)
            if item:
                entity = item.body
                break
        for kind in (adsk.fusion.BRepBody, adsk.fusion.Occurrence, adsk.fusion.Component):
            item = kind.cast(entity)
            if item:
                key = getattr(item, "entityToken", None) or id(item)
                if key not in seen:
                    seen.add(key)
                    targets.append((item, item.component.name if kind is adsk.fusion.Occurrence else item.name))
                break
    return targets or [(design.rootComponent, app.activeDocument.name)]


def unique_stems(names):
    """Safe file stems; selections of the same name would otherwise overwrite each other."""
    stems = []
    for name in names:
        base = stem = safe_name(name)
        number = 2
        while stem in stems:
            stem, number = f"{base}_{number}", number + 1
        stems.append(stem)
    return stems


def remove_old_exports(directory):
    """Folders from earlier sends, and loose files from older add-in versions."""
    cutoff = time.time() - EXPORT_LIFETIME
    for entry in os.scandir(directory):
        try:
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
        except OSError:
            pass


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def command_line(*arguments):
    """Quote for Wine's Windows-style command-line splitting, including backslash runs."""
    return subprocess.list2cmdline(arguments)


def launch_native(program, *arguments):
    """Start a Linux program without waiting.

    Wine execs non-PE files natively but returns no usable process handle, so
    subprocess.Popen fails while polling or cleaning up.
    """
    command = ctypes.create_unicode_buffer(command_line(program, *arguments))
    startup = STARTUPINFO(cb=ctypes.sizeof(STARTUPINFO))
    info = PROCESS_INFORMATION()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CreateProcessW(None, command, None, None, False, 0, None, None,
                                   ctypes.byref(startup), ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    for handle in (info.hProcess, info.hThread):
        if handle:
            kernel32.CloseHandle(handle)


def send_to_bambu_studio():
    app = adsk.core.Application.get()
    ui = app.userInterface
    launcher = os.environ.get("FUSION_WINE_SLICER_LAUNCHER")
    exports = os.environ.get("FUSION_WINE_EXPORT_DIR")
    if not launcher or not exports:
        ui.messageBox("Start Fusion through fusion-on-wine to send designs to Bambu Studio.", COMMAND_NAME)
        return
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox("Switch to the Design workspace first.", COMMAND_NAME)
        return
    os.makedirs(windows_path(exports), exist_ok=True)
    remove_old_exports(windows_path(exports))
    targets = export_targets(app, design)
    name = targets[0][1] if len(targets) == 1 else app.activeDocument.name
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # mkdtemp keeps repeated clicks within one second apart.
    created = tempfile.mkdtemp(prefix=f"{safe_name(name)}-{stamp}-", dir=windows_path(exports))
    folder = f"{exports}/{os.path.basename(created)}"
    started = time.monotonic()
    for (target, _), stem in zip(targets, unique_stems(n for _, n in targets)):
        # STL rather than 3MF: Bambu Studio reports Fusion's settings-free 3MF as
        # an invalid project and asks how to open it, but adds STL bodies as objects.
        options = design.exportManager.createSTLExportOptions(target, windows_path(f"{folder}/{stem}.stl"))
        options.isBinaryFormat = True
        options.isOneFilePerBody = True
        options.unitType = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
        options.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
        if not design.exportManager.execute(options):
            log(f"Fusion did not export {stem}")
    log(f"Export of {len(targets)} selection(s) took {time.monotonic() - started:.1f} s")
    files = sorted(f"{folder}/{n}" for n in os.listdir(created) if n.lower().endswith(".stl"))
    if not files:
        shutil.rmtree(created, ignore_errors=True)
        ui.messageBox("Fusion exported no bodies. Check that the design or selection has visible solid bodies.",
                      COMMAND_NAME)
        return
    launch_native(windows_path(launcher), *files)
    log(f"Sent {len(files)} file(s) from {folder}")


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            send_to_bambu_studio()
        except Exception:
            log(traceback.format_exc())
            adsk.core.Application.get().userInterface.messageBox(
                "Sending to Bambu Studio failed:\n" + traceback.format_exc(), COMMAND_NAME)


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        execute = ExecuteHandler()
        args.command.execute.add(execute)
        handlers.append(execute)


def run(context):
    try:
        ui = adsk.core.Application.get().userInterface
        definition = ui.commandDefinitions.itemById(COMMAND_ID)
        if definition:
            definition.deleteMe()
        definition = ui.commandDefinitions.addButtonDefinition(
            COMMAND_ID, COMMAND_NAME,
            "Export the selected bodies or components, or the whole design, to Bambu Studio. "
            "A running Bambu Studio adds them to its current plate.",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources"))
        created = CreatedHandler()
        definition.commandCreated.add(created)
        handlers.append(created)
        panel = next((p for p in map(ui.allToolbarPanels.itemById, PANEL_IDS) if p), None)
        if not panel:
            log("No toolbar panel found; tried " + ", ".join(PANEL_IDS))
            return
        control = panel.controls.itemById(COMMAND_ID)
        if control:
            control.deleteMe()
        control = panel.controls.addCommand(definition)
        control.isPromoted = True
        control.isPromotedByDefault = True
        log(f"Added to {panel.id}")
    except Exception:
        log(traceback.format_exc())


def stop(context):
    try:
        ui = adsk.core.Application.get().userInterface
        for panel_id in PANEL_IDS:
            panel = ui.allToolbarPanels.itemById(panel_id)
            control = panel and panel.controls.itemById(COMMAND_ID)
            if control:
                control.deleteMe()
        definition = ui.commandDefinitions.itemById(COMMAND_ID)
        if definition:
            definition.deleteMe()
    except Exception:
        log(traceback.format_exc())
    handlers.clear()
