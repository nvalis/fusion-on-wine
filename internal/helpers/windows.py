#!/usr/bin/env python3
"""Repair Fusion overlay opacity, palette geometry, and focus on Hyprland."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import time
from dataclasses import dataclass

from Xlib import X, Xutil, display, error, protocol


@dataclass(frozen=True)
class WindowInfo:
    xid: int
    pid: int
    title: str
    rect: tuple[int, int, int, int]
    owner: int | None
    opacity: float | None
    kind: str
    override_redirect: bool = False
    requested_rect: tuple[int, int, int, int] | None = None


def requested_geometry(rect, hints):
    """Wine's size hints retain Win32 intent when the compositor moves an X window."""
    if hints is None or len(hints) < 9:
        return rect
    x, y, width, height = rect
    flags, hx, hy, hw, hh, min_w, min_h, max_w, max_h = map(int, hints[:9])
    if flags & (Xutil.PPosition | Xutil.USPosition):
        # Xlib returns CARD32 values, including signed coordinates encoded in them.
        x = hx if hx < 0x80000000 else hx - 0x100000000
        y = hy if hy < 0x80000000 else hy - 0x100000000
    if flags & (Xutil.PSize | Xutil.USSize):
        if 0 < hw <= 65535 and 0 < hh <= 65535:
            width, height = hw, hh
    elif flags & Xutil.PMinSize and flags & Xutil.PMaxSize:
        if (min_w, min_h) == (max_w, max_h) and 0 < min_w <= 65535 and 0 < min_h <= 65535:
            width, height = min_w, min_h
    return x, y, width, height


def is_overlay(window, owner):
    """Exclude ordinary palettes, tooltips, and unrelated application windows."""
    if owner is None or window.owner != owner.xid or window.pid != owner.pid:
        return False
    if "Autodesk Fusion" not in owner.title or window.title != "Fusion360":
        return False
    if window.kind != "_NET_WM_WINDOW_TYPE_DIALOG":
        return False
    if window.opacity is None or not 0 <= window.opacity < 1:
        return False
    # Fusion can size this dimming surface to the monitor, not its owner.
    # The exact title, transient owner, window type, PID, and opacity identify
    # it more reliably than geometry, which varies with tiling and display size.
    return True


def match_client(info, clients):
    for client in clients:
        if client["class"].lower() != "fusion360.exe" or client["pid"] != info.pid:
            continue
        rect = (*client["at"], *client["size"])
        if client["title"] == info.title and all(abs(a - b) <= 2 for a, b in zip(rect, info.rect)):
            return client
    return None


def associate_clients(infos, clients, previous):
    """Keep known identities; never guess between duplicate unmatched titles."""
    clients = [c for c in clients if c["class"].lower() == "fusion360.exe" and c.get("mapped", True)]
    by_address = {c["address"]: c for c in clients}
    result = {}
    used = set()

    def compatible(info, client):
        return info.pid == client["pid"] and info.title == client["title"]

    def assign(xid, client):
        result[xid] = client
        used.add(client["address"])

    for xid, info in infos.items():
        client = by_address.get(previous.get(xid))
        if client is not None and client["address"] not in used and compatible(info, client):
            assign(xid, client)

    pairs = {}
    for xid, info in infos.items():
        if xid in result:
            continue
        pairs[xid] = [c for c in clients if c["address"] not in used and compatible(info, c)
                      and all(abs(a - b) <= 2 for a, b in zip(info.rect, (*c["at"], *c["size"])))]
    for xid, matches in pairs.items():
        if len(matches) == 1 and sum(matches[0] in candidates for candidates in pairs.values()) == 1:
            assign(xid, matches[0])

    for xid, info in infos.items():
        if xid in result:
            continue
        windows = [other for other in infos.values() if other.xid not in result
                   and (other.pid, other.title) == (info.pid, info.title)]
        matches = [c for c in clients if c["address"] not in used and compatible(info, c)]
        if len(windows) == len(matches) == 1:
            assign(xid, matches[0])
    return result


def geometry_commands(info, client, owner_client=None):
    """Restore application-requested geometry without guessing offsets from the owner."""
    if not info.override_redirect or not client.get("floating") or client.get("fullscreen", 0):
        return []
    rect = info.requested_rect or info.rect
    if client.get("hidden") or min(rect[2:]) <= 1:
        return []
    address = "address:" + client["address"]
    commands = []
    if owner_client is not None and client["workspace"]["id"] != owner_client["workspace"]["id"]:
        workspace = owner_client["workspace"]["name"]
        if workspace.startswith("special:"):
            # Do not put batch-command delimiters from a custom name into IPC.
            name = workspace[len("special:"):]
            target = workspace if name and all(c.isalnum() or c in "_.-" for c in name) else None
        else:
            target = str(owner_client["workspace"]["id"])
        if target is not None:
            commands.append(f"dispatch movetoworkspacesilent {target},{address}")
    x, y, width, height = rect
    if any(abs(a - b) > 2 for a, b in zip(client["at"], (x, y))):
        commands.append(f"dispatch movewindowpixel exact {x} {y},{address}")
    if any(abs(a - b) > 2 for a, b in zip(client["size"], (width, height))):
        commands.append(f"dispatch resizewindowpixel exact {width} {height},{address}")
    return commands


def is_dialog(window, overlay, owner):
    return (
        window.pid == owner.pid
        and window.xid not in (overlay.xid, owner.xid)
        and window.owner in (None, owner.xid)
        and window.kind in ("_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG")
        and bool(window.title)
        and window.title != "Fusion360"
        and not window.title.startswith(("QTView:", "Msg:"))
        and "Autodesk Fusion" not in window.title
    )


def is_focusable_palette(window):
    """Exclude dimmers and notifications, but allow compact editable command HUDs."""
    minimum_height = 16 if window.title.startswith("HUD:") else 50
    return (
        window.kind in ("_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG")
        and bool(window.title)
        and not window.title.startswith(("QTView:", "Msg:"))
        and "Autodesk Fusion" not in window.title
        and window.rect[2] >= 100 and window.rect[3] >= minimum_height
        and (window.opacity is None or window.opacity == 1)
    )


def hypr(*args):
    result = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError("Hyprland IPC command failed")
    return result.stdout.strip()


def setprop(address, name, value):
    # Hyprland 0.56 moved setprop to a dispatcher. Support the older IPC form too.
    if hypr("dispatch", "setprop", f"address:{address} {name} {value}") == "ok":
        return
    if hypr("setprop", f"address:{address}", name, value) != "ok":
        raise RuntimeError("Hyprland rejected window property")


class Watcher:
    def __init__(self, pid):
        self.pid = pid
        self.pidfd = os.pidfd_open(pid)
        self.display = display.Display()
        self.root = self.display.screen().root
        self.atoms = {name: self.display.intern_atom(name) for name in (
            "_NET_WM_PID", "_NET_WM_WINDOW_OPACITY", "_NET_WM_WINDOW_TYPE",
            "WM_TRANSIENT_FOR", "WM_NAME", "_NET_WM_NAME", "WM_CLASS",
            "WM_PROTOCOLS", "WM_TAKE_FOCUS", "WM_NORMAL_HINTS",
        )}
        self.events = None
        signature = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        for base in (Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")), Path("/tmp")):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(str(base / "hypr" / signature / ".socket2.sock"))
            except OSError:
                connection.close()
            else:
                connection.setblocking(False)
                self.events = connection
                break
        self.event_buffer = b""
        self.focus_pending = True
        self.focus_deadline = time.monotonic() + 3
        self.next_focus_poll = 0
        self.pending = {}
        self.handled = {}
        self.opacity_applied = {}
        self.tracked = set()
        self.next_scan = 0
        self.geometry_pending = True
        self.next_geometry_poll = 0
        self.next_geometry_monitors = 0
        self.geometry_enabled = False
        self.client_addresses = {}
        self.geometry_reported = set()
        self.root.change_attributes(event_mask=X.SubstructureNotifyMask)
        for window in self.root.query_tree().children:
            self.register(window)
        self.display.flush()

    def read(self, window):
        try:
            if "fusion360.exe" not in tuple(c.lower() for c in (window.get_wm_class() or ())):
                return None
            attributes = window.get_attributes()
            if attributes.map_state != X.IsViewable:
                return None
            pid = window.get_full_property(self.atoms["_NET_WM_PID"], X.AnyPropertyType)
            if pid is None or int(pid.value[0]) != self.pid:
                return None
            opacity = window.get_full_property(self.atoms["_NET_WM_WINDOW_OPACITY"], X.AnyPropertyType)
            kind = window.get_full_property(self.atoms["_NET_WM_WINDOW_TYPE"], X.AnyPropertyType)
            owner = window.get_wm_transient_for()
            geometry = window.get_geometry()
            rect = (geometry.x, geometry.y, geometry.width, geometry.height)
            requested = None
            if attributes.override_redirect:
                # python-xlib's get_wm_normal_hints discards the old position and
                # size fields. Wine still publishes its Win32 geometry there.
                hints = window.get_full_property(self.atoms["WM_NORMAL_HINTS"], X.AnyPropertyType)
                if hints is not None and hints.format == 32:
                    requested = requested_geometry(rect, hints.value)
            return WindowInfo(
                window.id, self.pid, window.get_wm_name() or "",
                rect,
                owner.id if owner else None,
                int(opacity.value[0]) / 0xffffffff if opacity is not None else None,
                self.display.get_atom_name(int(kind.value[0])) if kind is not None and len(kind.value) else "",
                bool(attributes.override_redirect), requested,
            )
        except error.XError:
            return None

    def register(self, window):
        if window.id in self.tracked or self.read(window) is None:
            return
        try:
            window.change_attributes(event_mask=X.PropertyChangeMask)
        except error.XError:
            return
        self.tracked.add(window.id)
        # A newly mapped dialog may arrive after its overlay. Retry both.
        for xid in self.tracked:
            self.pending[xid] = time.monotonic() + 10

    def reconcile(self):
        # MapNotify can precede Wine's PID, class, title, or transient metadata.
        # Recheck mapped windows instead of depending on one event at startup.
        windows = self.root.query_tree().children
        infos = {}
        for window in windows:
            self.register(window)
            info = self.read(window)
            if info is not None:
                infos[info.xid] = info
        for info in infos.values():
            if is_overlay(info, infos.get(info.owner)):
                self.pending.setdefault(info.xid, time.monotonic() + 10)
        self.geometry_pending = True
        self.display.flush()

    def sync_opacity(self, infos, clients):
        """Translate Wine's X11 opacity requests independently of modal heuristics."""
        for xid, info in infos.items():
            client = clients.get(xid)
            if client is None:
                continue
            key = (xid, client["address"])
            # Removing the X11 property restores full opacity. Leave windows
            # without a request alone unless we previously changed them.
            if info.opacity is None and key not in self.opacity_applied:
                continue
            if key in self.opacity_applied and self.opacity_applied[key] == info.opacity:
                continue
            alpha = str(info.opacity if info.opacity is not None else 1.0)
            for prop in ("opacity", "opacity_inactive", "opacity_fullscreen"):
                setprop(client["address"], prop, alpha)
            if info.opacity is None:
                self.opacity_applied.pop(key, None)
            else:
                self.opacity_applied[key] = info.opacity
            print(f"Synchronized X11 window opacity {alpha}", flush=True)
        self.opacity_applied = {
            key: value for key, value in self.opacity_applied.items()
            if key[0] in infos and (key[0] not in clients or clients[key[0]]["address"] == key[1])
        }

    def sync_windows(self):
        infos = {}
        for window in self.root.query_tree().children:
            info = self.read(window)
            if info is not None:
                infos[info.xid] = info
        clients = associate_clients(infos, json.loads(hypr("-j", "clients")), self.client_addresses)
        self.client_addresses = {xid: c["address"] for xid, c in clients.items()}
        # Opacity is dimensionless. It also works on mixed-DPI desktops where
        # the geometry repair below deliberately does not run.
        self.sync_opacity(infos, clients)
        now = time.monotonic()
        if now >= self.next_geometry_monitors:
            monitors = json.loads(hypr("-j", "monitors"))
            # X11 pixel coordinates and Hyprland logical coordinates agree at scale 1.
            # Leave mixed-DPI conversion to the compositor rather than guessing.
            self.geometry_enabled = bool(monitors) and all(m["scale"] == 1 for m in monitors)
            self.next_geometry_monitors = now + 1
        if not self.geometry_enabled:
            return
        commands = []
        repaired = []
        for xid, info in infos.items():
            client = clients.get(xid)
            if client is None:
                continue
            owner = infos.get(info.owner)
            owner_client = clients.get(info.owner) if owner is not None and "Autodesk Fusion" in owner.title else None
            changes = geometry_commands(info, client, owner_client)
            if changes:
                commands.extend(changes)
                repaired.append(xid)
        if commands:
            result = hypr("--batch", ";".join(commands))
            if any(line.strip() != "ok" for line in result.splitlines() if line.strip()):
                raise RuntimeError("Hyprland rejected palette geometry")
            for xid in repaired:
                if xid not in self.geometry_reported:
                    print("Repaired unmanaged palette geometry", flush=True)
                    self.geometry_reported.add(xid)

    def fix(self, info):
        owner = self.read(self.display.create_resource_object("window", info.owner)) if info.owner else None
        if not is_overlay(info, owner):
            return True
        clients = json.loads(hypr("-j", "clients"))
        client = match_client(info, clients)
        if client is None:
            return False  # XWayland has mapped it, but Hyprland has not registered it yet.
        key = (info.xid, client["address"])
        state = self.handled.setdefault(key, {})
        if not state.get("no_focus"):
            setprop(client["address"], "no_focus", "on")
            state["no_focus"] = True
        dialogs = []
        for window in self.root.query_tree().children:
            candidate = self.read(window)
            if candidate is not None and is_dialog(candidate, info, owner):
                candidate_client = match_client(candidate, clients)
                if candidate_client is not None:
                    dialogs.append((window, candidate_client))
        if not dialogs:
            return False
        window, dialog = dialogs[-1]
        if state.get("dialog") != dialog["address"]:
            active = json.loads(hypr("-j", "activewindow"))
            if active.get("pid") == self.pid:
                if hypr("dispatch", "focuswindow", "address:" + dialog["address"]) != "ok":
                    return False
                if hypr("dispatch", "bringactivetotop") != "ok":
                    return False
            # Keep the X11 order consistent with Hyprland's floating-window order.
            window.configure(stack_mode=X.Above)
            self.display.flush()
            state["dialog"] = dialog["address"]
            print("Restacked modal dialog above overlay", flush=True)
        return True

    def queue_focus(self):
        self.focus_pending = True
        self.focus_deadline = time.monotonic() + 3

    def sync_focus(self):
        active = json.loads(hypr("-j", "activewindow"))
        if active.get("pid") != self.pid or active.get("class", "").lower() != "fusion360.exe":
            return True
        for window in self.root.query_tree().children:
            info = self.read(window)
            if info is None or match_client(info, [active]) is None:
                continue
            if not window.get_attributes().override_redirect or not is_focusable_palette(info):
                return True
            protocols = window.get_wm_protocols() or []
            if self.atoms["WM_TAKE_FOCUS"] not in protocols:
                return True
            focus = self.display.get_input_focus().focus
            if getattr(focus, "id", focus) != window.id:
                # Hyprland deliberately skips WM_TAKE_FOCUS for override-redirect
                # windows. Wine relies on it, so complete that handoff ourselves.
                window.send_event(protocol.event.ClientMessage(
                    window=window.id, client_type=self.atoms["WM_PROTOCOLS"],
                    data=(32, [self.atoms["WM_TAKE_FOCUS"], X.CurrentTime, 0, 0, 0]),
                ))
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                self.display.flush()
                print("Repaired unmanaged palette X11 focus", flush=True)
            return True
        return False  # Native metadata and compositor state can arrive separately.

    def run(self):
        while True:
            descriptors = [self.display.fileno(), self.pidfd]
            if self.events is not None:
                descriptors.append(self.events)
            timeout = 0.02 if self.geometry_pending else 0.1 if self.pending or self.focus_pending else 0.5
            ready, _, _ = select.select(descriptors, [], [], timeout)
            if self.pidfd in ready:
                return
            if self.events is not None and self.events in ready:
                chunk = self.events.recv(65536)
                if not chunk:
                    self.events.close()
                    self.events = None
                else:
                    self.event_buffer += chunk
                    while b"\n" in self.event_buffer:
                        line, self.event_buffer = self.event_buffer.split(b"\n", 1)
                        if line.startswith((b"activewindow>>", b"activewindowv2>>")):
                            self.queue_focus()
            if self.events is None and time.monotonic() >= self.next_focus_poll:
                self.queue_focus()
                self.next_focus_poll = time.monotonic() + 0.5
            while self.display.pending_events():
                event = self.display.next_event()
                window = getattr(event, "window", None)
                if window is None:
                    continue
                xid = window.id
                if event.type == X.MapNotify:
                    self.geometry_pending = True
                    self.register(window)
                    if xid in self.tracked:
                        self.queue_focus()
                elif event.type in (X.DestroyNotify, X.UnmapNotify):
                    self.tracked.discard(xid)
                    self.pending.pop(xid, None)
                    self.client_addresses.pop(xid, None)
                    self.geometry_reported.discard(xid)
                    self.handled = {k: v for k, v in self.handled.items() if k[0] != xid}
                elif xid in self.tracked and (
                    event.type == X.ConfigureNotify
                    or event.type == X.PropertyNotify and event.atom in self.atoms.values()
                ):
                    self.pending[xid] = time.monotonic() + 10
                    self.geometry_pending = True
                    if event.type == X.PropertyNotify and event.atom == self.atoms["_NET_WM_WINDOW_OPACITY"]:
                        self.queue_focus()
            if time.monotonic() >= self.next_scan:
                self.reconcile()
                self.next_scan = time.monotonic() + 1
            if self.geometry_pending and time.monotonic() >= self.next_geometry_poll:
                try:
                    self.sync_windows()
                except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, error.XError):
                    pass
                self.geometry_pending = False
                self.next_geometry_poll = time.monotonic() + 0.02
            if self.focus_pending:
                try:
                    done = self.sync_focus()
                except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, error.XError):
                    done = False
                if done or time.monotonic() > self.focus_deadline:
                    self.focus_pending = False
            for xid, deadline in list(self.pending.items()):
                info = self.read(self.display.create_resource_object("window", xid))
                try:
                    done = info is None or self.fix(info)
                except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, error.XError):
                    done = False
                if done or time.monotonic() > deadline:
                    self.pending.pop(xid, None)
                    if not done:
                        print("Could not repair a modal overlay; waiting for another window event", flush=True)

    def close(self):
        if self.events is not None:
            self.events.close()
        self.display.close()
        os.close(self.pidfd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="Fusion process to watch")
    args = parser.parse_args()
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    # A closing Fusion instance must not prevent a new instance's helper starting.
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    log_dir = Path(os.environ.get("FUSION_WINE_LOG_DIR", state / "fusion-on-wine/logs"))
    lock_path = log_dir / f"overlays-{args.pid}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        watcher = None
        try:
            watcher = Watcher(args.pid)
            watcher.run()
        except ProcessLookupError:
            return
        finally:
            if watcher is not None:
                watcher.close()


if __name__ == "__main__":
    main()
