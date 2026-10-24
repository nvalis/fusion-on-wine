"""Opt-in real Fusion hover check on an already-open disposable prefix.

Moves the pointer only. It never clicks, types, saves, or captures screenshots.
Open a shaded model in the diagnostic instance and focus it before running.
Choose a face point and a blank viewport point; the face must visibly highlight.
Do not use the mouse or change focus during the check.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# Allow the same direct-script invocation as check_present_latency.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from internal.runtime import load_settings


DIAGNOSTIC_MARKER = ".fusion-on-wine-diagnostic-prefix.json"


def point(value):
    try:
        x, y = map(int, value.split(","))
        if x < 0 or y < 0:
            raise ValueError
        return x, y
    except ValueError:
        raise argparse.ArgumentTypeError("Expected nonnegative window-relative x,y coordinates") from None


def close_color(actual, expected, tolerance):
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


def validate_prefix(prefix, configured_prefix, pid):
    prefix = prefix.resolve()
    if prefix == configured_prefix.resolve():
        raise RuntimeError("Never test the configured Fusion prefix. Use a disposable copy.")
    marker = prefix / DIAGNOSTIC_MARKER
    if not marker.is_file() or json.loads(marker.read_text()).get("purpose") != "graphical-regression":
        raise RuntimeError("The disposable prefix needs an explicit graphical-regression marker. See README.md.")
    process = Path("/proc") / str(pid)
    if process.stat().st_uid != os.geteuid():
        raise RuntimeError("The diagnostic process must belong to the current user.")
    values = (process / "environ").read_bytes().split(b"\0")
    value = next((v.removeprefix(b"WINEPREFIX=") for v in values if v.startswith(b"WINEPREFIX=")), None)
    if value is None or Path(os.fsdecode(value)).resolve() != prefix:
        raise RuntimeError("The selected process does not use the disposable prefix.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True, help="Diagnostic Fusion360.exe host process ID")
    parser.add_argument("--hover", type=point, required=True, help="Interior face point, relative to the main window")
    parser.add_argument("--neutral", type=point, required=True, help="Blank viewport point, relative to the main window")
    parser.add_argument("--sample", type=point, help="Pixel on the highlighted face, defaults to --hover")
    parser.add_argument("--transitions", type=int, default=40)
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds after each single pointer move")
    parser.add_argument("--tolerance", type=int, default=2, help="Allowed per-channel RGB rounding difference")
    args = parser.parse_args()
    if args.transitions < 2 or not 0.1 <= args.delay <= 10 or not 0 <= args.tolerance <= 10:
        parser.error("Use at least two transitions, delay 0.1 to 10, and tolerance 0 to 10")
    validate_prefix(args.prefix, load_settings().prefix, args.pid)

    from Xlib import X, display
    from Xlib.ext import xtest

    d = display.Display()
    root = d.screen().root
    pid_atom = d.intern_atom("_NET_WM_PID")
    client_list = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    candidates = ([d.create_resource_object("window", int(xid)) for xid in client_list.value]
                  if client_list is not None else root.query_tree().children)
    owners = []
    for window in candidates:
        pid = window.get_full_property(pid_atom, X.AnyPropertyType)
        if pid is None or int(pid.value[0]) != args.pid:
            continue
        if "Autodesk Fusion" not in (window.get_wm_name() or "") or window.get_attributes().map_state != X.IsViewable:
            continue
        g = window.get_geometry()
        owners.append((g.width * g.height, window))
    if not owners:
        d.close()
        raise RuntimeError("No visible diagnostic Fusion main window found")
    owner = max(owners, key=lambda item: item[0])[1]
    geometry = owner.get_geometry()
    origin = root.translate_coords(owner, 0, 0)
    points = (args.hover, args.neutral, args.sample or args.hover)
    if any(x + 1 >= geometry.width or y >= geometry.height for x, y in points):
        d.close()
        raise RuntimeError("All points and their one-pixel calibration nudge must fit inside the window")
    sample_x, sample_y = points[2]
    visual_id = owner.get_attributes().visual
    visual = next(v for depth in d.screen().allowed_depths for v in depth.visuals if v.visual_id == visual_id)
    masks = (visual.red_mask, visual.green_mask, visual.blue_mask)
    if not all(masks):
        d.close()
        raise RuntimeError("The checker requires a TrueColor RGB visual")
    shifts = [((mask & -mask).bit_length() - 1) for mask in masks]
    original_pointer = root.query_pointer()
    last_pointer = None
    restore_pointer = True

    def guard():
        nonlocal restore_pointer
        current = owner.get_geometry()
        location = root.translate_coords(owner, 0, 0)
        if (current.width, current.height, location.x, location.y) != (geometry.width, geometry.height, origin.x, origin.y):
            restore_pointer = False
            raise RuntimeError("Diagnostic window moved or resized; check aborted")
        # Wine can intentionally clear core X11 focus while the compositor
        # still focuses its owner. Check compositor focus before the EWMH fallback.
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and not os.environ.get("SWAYSOCK"):
            focused = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"], timeout=3))
            if focused.get("pid") != args.pid:
                restore_pointer = False
                raise RuntimeError("Diagnostic Fusion lost compositor focus; check aborted")
        focus = d.get_input_focus().focus
        if not hasattr(focus, "id") or focus.id == root.id:
            active = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
            if active is not None and len(active.value) and active.value[0]:
                focus = d.create_resource_object("window", int(active.value[0]))
        for _ in range(16):
            if not hasattr(focus, "id"):
                restore_pointer = False
                raise RuntimeError("Diagnostic Fusion lost keyboard focus; check aborted")
            if focus.id == owner.id:
                break
            pid = focus.get_full_property(pid_atom, X.AnyPropertyType)
            if pid is not None and int(pid.value[0]) == args.pid:
                break
            parent = focus.query_tree().parent
            if parent.id == focus.id or focus.id == root.id:
                restore_pointer = False
                raise RuntimeError("Diagnostic Fusion lost keyboard focus; check aborted")
            focus = parent
        else:
            restore_pointer = False
            raise RuntimeError("Could not verify diagnostic focus")
        if last_pointer is not None:
            pointer = root.query_pointer()
            if (pointer.root_x, pointer.root_y) != last_pointer:
                restore_pointer = False
                raise RuntimeError("External pointer movement detected; check aborted")

    def move(where):
        nonlocal last_pointer
        guard()
        x, y = where
        last_pointer = origin.x + x, origin.y + y
        xtest.fake_input(d, X.MotionNotify, x=last_pointer[0], y=last_pointer[1])
        d.sync()
        time.sleep(args.delay)
        guard()

    def sample():
        image = owner.get_image(sample_x, sample_y, 1, 1, X.ZPixmap, 0xffffffff)
        order = "little" if d.display.info.image_byte_order == X.LSBFirst else "big"
        value = int.from_bytes(image.data[:4], order)
        return tuple(((value & mask) >> shift) * 255 // (mask >> shift) for mask, shift in zip(masks, shifts))

    def reference(where):
        # Calibration deliberately submits repeated same-state motions. Actual
        # transitions below submit exactly one motion before reading the owner.
        for _ in range(3):
            move(where)
            move((where[0] + 1, where[1]))
        return sample()

    try:
        neutral = reference(args.neutral)
        hover = reference(args.hover)
        if close_color(neutral, hover, args.tolerance * 2):
            raise RuntimeError("No distinct hover colour. Choose a face with a visible highlight and a matching sample point.")
        reference(args.neutral)
        failures = []
        for frame in range(args.transitions):
            highlighting = frame % 2 == 0
            move(args.hover if highlighting else args.neutral)
            actual, expected = sample(), hover if highlighting else neutral
            if not close_color(actual, expected, args.tolerance):
                failures.append(frame)
                print(f"frame {frame}: expected={expected} visible={actual}")
        if failures:
            raise RuntimeError(f"Hover check failed: {len(failures)}/{args.transitions} stale transitions")
        print(f"PASS: {args.transitions} hover transitions visible without a subsequent mouse event")
    finally:
        if restore_pointer:
            xtest.fake_input(d, X.MotionNotify, x=original_pointer.root_x, y=original_pointer.root_y)
            d.sync()
        d.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
