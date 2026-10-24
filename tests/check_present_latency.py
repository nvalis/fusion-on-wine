"""Opt-in X11/Sway presentation check using an isolated prefix and D3D11 fixture.

Requires python-xlib. Put DXVK's x64 d3d11.dll/dxgi.dll beside the fixture.
Checks the visible X11 owner, not Wine's cached GDI GetPixel backing store.
A pass does not replace interactive Fusion validation (multiple nested surfaces).
"""
import argparse
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time

from Xlib import X, display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--toplevel", action="store_true", help="Control: no child viewport")
    parser.add_argument("--disable-sync", action="store_true", help="A/B comparison with the workaround off")
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    if (prefix / ".fusion-on-wine-prefix.json").exists() or list(prefix.glob(
            "drive_c/users/*/AppData/Local/Autodesk/webdeploy/production/*/Fusion360.exe")):
        parser.error("Use an isolated test prefix, never the installed Fusion prefix")
    runtime, exe = args.runtime.resolve(), args.exe.resolve()
    env = dict(os.environ, WINEPREFIX=str(prefix), WINESERVER=str(runtime / "bin/wineserver"),
               WINEARCH="win64", WINEDEBUG="-all", WINEDLLOVERRIDES="d3d11,dxgi=n;mscoree,mshtml=",
               FUSION_WINE_SYNC_PRESENT="0" if args.disable_sync else "1")
    for key in ("WAYLAND_DISPLAY", "WINEDLLPATH", "WINELOADER", "FUSION_WINE_READBACK_PRESENT"):
        env.pop(key, None)
    d = display.Display()
    samples, failures = 0, []
    owner = None
    # No screenshots, model data, or credentials are captured by this checker.
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen([runtime / "bin/wine", exe] + (["--toplevel"] if args.toplevel else []),
                                   env=env, cwd=exe.parent, stdout=subprocess.PIPE, stderr=log)
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                if not select.select([process.stdout], [], [], 1)[0]:
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline().decode(errors="replace").strip()
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                if line == "ready":
                    owners = [w for w in d.screen().root.query_tree().children
                              if w.get_wm_name() == "Isolated presentation latency probe"]
                    if len(owners) != 1:
                        raise RuntimeError("Expected exactly one fixture window")
                    owner = owners[0]
                    if env.get("SWAYSOCK"):
                        subprocess.run(["swaymsg", f"[id={owner.id}] floating enable, resize set 420 340, focus"],
                                       check=True, stdout=subprocess.DEVNULL)
                elif line.startswith("frame "):
                    if owner is None:
                        raise RuntimeError("Frame received before fixture window")
                    frame = int(line.split()[1])
                    time.sleep(0.3)
                    image = owner.get_image(100, 100, 1, 1, X.ZPixmap, 0xffffffff)
                    order = "little" if d.display.info.image_byte_order == X.LSBFirst else "big"
                    pixel = int.from_bytes(image.data[:4], order) & 0xffffff
                    expected = (0xff0000, 0x00ff00, 0x0000ff)[frame % 3]
                    samples += 1
                    print(f"frame {frame}: expected={expected:06x} visible={pixel:06x}")
                    if pixel != expected:
                        failures.append(frame)
                else:
                    print(line)
            result = process.poll()
            if result is None:
                result = process.wait(timeout=5)
            if result or samples != 6 or failures:
                raise RuntimeError(f"Presentation check failed: exit={result}, samples={samples}, stale={failures}")
            print("PASS: all six frames visible without a subsequent present")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            d.close()


if __name__ == "__main__":
    main()
