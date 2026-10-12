#!/usr/bin/env python3
"""Draw known premultiplied ARGB samples directly through X11."""
import argparse
import os
import select
import time
from PIL import Image
from Xlib import X, Xatom, display


def sample_image():
    image = Image.new("RGBA", (400, 160), (0, 0, 0, 0))
    colors = [(255, 0, 0, 255), (128, 0, 0, 128), (0, 128, 0, 128), (0, 0, 128, 128)]
    for index, color in enumerate(colors):
        image.paste(color, (index * 100, 0, index * 100 + 100, 80))
    for index, alpha in enumerate((0, 64, 128, 255)):
        image.paste((alpha // 5, alpha // 5, alpha // 5, alpha), (index * 100, 80, index * 100 + 100, 160))
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    args = parser.parse_args()
    connection = display.Display()
    screen = connection.screen()
    root = screen.root
    if args.x is None:
        args.x = max(0, (screen.width_in_pixels - 440) // 2)
    if args.y is None:
        args.y = max(0, (screen.height_in_pixels - 200) // 2)
    # XWayland's 32-bit TrueColor visuals have 8-bit alpha above the RGB masks.
    visual = next(v for depth in screen.allowed_depths if depth.depth == 32
                  for v in depth.visuals if v.visual_class == X.TrueColor
                  and (v.red_mask, v.green_mask, v.blue_mask) == (0xff0000, 0xff00, 0xff))
    colormap = root.create_colormap(visual.visual_id, X.AllocNone)
    background = root.create_window(args.x, args.y, 440, 200, 0, screen.root_depth,
                                    X.InputOutput, X.CopyFromParent, background_pixel=screen.white_pixel,
                                    override_redirect=True, event_mask=X.ExposureMask)
    overlay = root.create_window(args.x + 20, args.y + 20, 400, 160, 0, 32,
                                 X.InputOutput, visual.visual_id, colormap=colormap,
                                 background_pixel=0, border_pixel=0, override_redirect=True,
                                 event_mask=X.ExposureMask)
    for window, title in ((background, "Alpha probe background"), (overlay, "Alpha probe ARGB")):
        window.set_wm_name(title)
        window.set_wm_class("alpha-probe", "AlphaProbe")
        window.change_property(connection.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
    overlay.set_wm_transient_for(background)
    background.map()
    overlay.map()
    gc = overlay.create_gc()
    image = sample_image()
    data = image.tobytes("raw", "BGRA")
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            overlay.put_image(gc, 0, 0, 400, 160, X.ZPixmap, 32, 0, data)
            connection.flush()
            select.select([connection.fileno()], [], [], 0.5)
            while connection.pending_events():
                connection.next_event()
    finally:
        overlay.destroy()
        background.destroy()
        gc.free()
        colormap.free()
        connection.close()


if __name__ == "__main__":
    main()
