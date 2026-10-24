"""Opt-in Hyprland geometry regression using only a synthetic X11 palette.

Uses an unused workspace without switching to it, moving the pointer, clicking,
or opening a Wine prefix. Checks initial placement and later displacement.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from Xlib import X, Xutil, display

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from internal.runtime import ROOT, hyprland_signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=int, default=98, help="Unused positive workspace ID")
    args = parser.parse_args()
    if args.workspace <= 0:
        parser.error("Use a positive workspace ID")
    signature = hyprland_signature()
    if not signature:
        parser.error("This check requires a Hyprland session")
    env = dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE=signature)

    def hypr(*arguments):
        return subprocess.check_output(["hyprctl", *arguments], env=env, text=True, timeout=3).strip()

    if any(w["id"] == args.workspace for w in json.loads(hypr("-j", "workspaces"))):
        parser.error("Choose a workspace that does not already exist")
    monitors = json.loads(hypr("-j", "monitors"))
    if not monitors or any(m["scale"] != 1 for m in monitors):
        parser.error("Geometry repair requires scale-1 monitors")
    monitor = monitors[0]
    requested = (monitor["x"] + 120, monitor["y"] + 180)
    displaced = (requested[0] + 300, requested[1] + 160)
    d = display.Display()
    root = d.screen().root
    window = root.create_window(
        *displaced, 250, 453, 0, X.CopyFromParent, X.InputOutput, X.CopyFromParent,
        override_redirect=True, background_pixel=0xffffff,
    )
    title = f"Synthetic palette geometry regression {os.getpid()}"
    window.set_wm_name(title)
    window.set_wm_class("fusion360.exe", "fusion360.exe")
    window.set_wm_hints(flags=Xutil.InputHint, input=0)
    window.change_property(d.intern_atom("_NET_WM_PID"), d.intern_atom("CARDINAL"),
                           32, [os.getpid()])
    # Write the raw ICCCM position fields. python-xlib's size-hints helper omits them.
    window.change_property(d.intern_atom("WM_NORMAL_HINTS"), d.intern_atom("WM_SIZE_HINTS"), 32, [
        Xutil.PPosition | Xutil.PMinSize | Xutil.PMaxSize,
        *requested, 0, 0, 250, 453, 250, 453, 0, 0, 0, 0, 0, 0, 0, 0, X.StaticGravity,
    ])
    process = None
    with tempfile.TemporaryFile(mode="w+") as log:
        try:
            window.map()
            d.sync()
            deadline = time.monotonic() + 10
            while True:
                clients = [c for c in json.loads(hypr("-j", "clients"))
                           if c["pid"] == os.getpid() and c["title"] == title]
                if len(clients) == 1:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Synthetic palette did not appear")
                time.sleep(.05)
            address = "address:" + clients[0]["address"]
            assert hypr("dispatch", "movetoworkspacesilent", f"{args.workspace},{address}") == "ok"
            assert hypr("dispatch", "movewindowpixel", f"exact {displaced[0]} {displaced[1]},{address}") == "ok"
            process = subprocess.Popen(
                [sys.executable, ROOT / "internal/helpers/windows.py", "--pid", str(os.getpid())],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )

            def wait_for_repair():
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Window helper exited")
                    client = next((c for c in json.loads(hypr("-j", "clients"))
                                   if c["address"] == clients[0]["address"]), None)
                    g = window.get_geometry()
                    if client and tuple(client["at"]) == requested and (g.x, g.y) == requested:
                        return
                    if time.monotonic() > deadline:
                        raise RuntimeError("Palette position did not converge to WM_NORMAL_HINTS")
                    time.sleep(.05)

            wait_for_repair()
            assert hypr("dispatch", "movewindowpixel", f"exact {displaced[0]} {displaced[1]},{address}") == "ok"
            wait_for_repair()
            print("PASS: initial and later displacement repaired in X11 and Hyprland")
        except Exception:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=5)
            window.destroy()
            d.sync()
            d.close()


if __name__ == "__main__":
    main()
