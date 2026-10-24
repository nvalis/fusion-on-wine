# Autodesk Fusion on Wine

An unofficial installer and launcher for Autodesk Fusion on x86-64 Linux.
It builds a private, patched Wine runtime and installs Fusion from Autodesk's official downloader.
The viewport and UI use DirectX 11 through DXVK, with Fusion's bundled shader compilers.
Autodesk does not support Fusion on Linux; you need your own Autodesk account and an applicable license.

## Install

Run from a terminal in your graphical desktop session.
Automatic host-package installation currently supports Arch-based distributions, including CachyOS.

```sh
git clone https://github.com/nvalis/fusion-on-wine.git
cd fusion-on-wine
./install.sh --dry-run
# Review applicable Autodesk, Microsoft runtime, and font licenses first.
./install.sh --deps --accept-third-party-licenses
./fusion.sh
```

Run as your normal user, not root.
`--deps` uses `sudo pacman -Syu --needed` and asks you to review the package transaction.
Omit it if dependencies are already installed.
Allow at least 20 GB free and expect a full Wine build.
Build parallelism defaults to at most four jobs; use `--jobs 2` on a memory-constrained machine.
Install your GPU's appropriate drivers through your distribution first.
DXVK 3.1.1 expects Vulkan 1.4 and the features listed in its [driver requirements](https://github.com/doitsujin/dxvk/wiki/Driver-support).

Wine, Winetricks, DXVK, and Python versions are recorded in [internal/versions.json](internal/versions.json).
Wine, Winetricks, and DXVK downloads have pinned SHA-256 checksums.
Autodesk's downloader is mutable and installs the release Autodesk currently provides.
The installer reports the detected version and warns if it differs from the tested release.

<details>
<summary>Manual dependencies for Debian/Ubuntu, not yet validated</summary>

Package names and compiler availability vary by release.
Use a recent distribution, install these dependencies yourself, then omit `--deps`.

```sh
sudo apt update
sudo apt install build-essential gcc-mingw-w64-i686 gcc-mingw-w64-x86-64 \
  g++-mingw-w64-i686 g++-mingw-w64-x86-64 flex bison pkg-config gettext \
  python3 python3-venv python3-pip cabextract unzip curl wget p7zip-full \
  xdg-utils desktop-file-utils xwayland mesa-utils vulkan-tools \
  libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxi-dev libxfixes-dev \
  libxrender-dev libxcomposite-dev libxinerama-dev libxxf86vm-dev \
  libfreetype-dev libfontconfig-dev libgnutls28-dev libgl-dev libvulkan-dev \
  libunwind-dev libasound2-dev libpulse-dev libdbus-1-dev libcups2-dev \
  libsdl2-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  libavcodec-dev libavformat-dev libswscale-dev zlib1g-dev libbz2-dev liblzma-dev
./install.sh --accept-third-party-licenses
```

</details>

## Use

Launch through `./fusion.sh` or the application menu entry "Autodesk Fusion (Wine)".
The launcher follows normal desktop tiling rules rather than forcing a floating window or fixed size.
On Sway, Wine contains Fusion's owned captionless `Fusion360` and `HUD:` palette surfaces inside their main X11 owner window.
Fusion still sees Windows popups and controls their placement and stacking; Sway no longer treats them as separate floating dialogs.
This uses neither a Wine virtual desktop nor a Sway placement helper or global window rules.
Palettes follow their owner across workspaces and are clipped to its bounds: they cannot extend outside the main window or onto a second monitor independently.
Ordinary titled dialogs, menus, and tooltips retain the existing handling; owned `Fusion360` modal overlay surfaces are contained too.
After updating the Wine patches, save and close Fusion and its Wine services, then rerun `./install.sh --accept-third-party-licenses` to rebuild the runtime.
The isolated integration fixture in `tests/wine_palettes.c` checks containment, movement, resizing, remapping, keyboard input, alpha-transparent holes, palette persistence during repeated parent repaints without hover, and owner destruction using `tests/check_wine_palettes.py` and a separate test prefix.
The checker's `--gpu` mode also exercises offscreen OpenGL presents and verifies that clicking the owner does not erase the palettes. Collapse/expand checks stop periodic parent/GPU redraws and use a deliberately different software backing colour to catch stale backgrounds revealed while idle.
Contained ARGB palettes use alpha-derived bounding masks rather than top-level compositor blending. A 50% alpha cutoff omits faint edges/shadows instead of turning their premultiplied pixels into black borders; opaque black UI is preserved. Masks refresh on opacity-only updates too; actual mask changes queue an owner/child application repaint so collapsing rows does not expose stale black software backing pixels beneath an idle GPU frame. Unchanged masks do not queue more repaints. This is binary transparency, not full fractional-alpha blending; translucent modal rendering still needs validation. Software repaints and offscreen GPU frame copies clip child windows, including the XRender destination path.
Real Fusion dragging, modal workflows, and rendering still need interactive validation.
Sign in yourself in the browser Fusion opens, and allow it to open the `adskidmgr` link when prompted.
The callback runs Identity Manager in the same prefix without logging the URL.
If browser sign-in succeeds but Fusion reports failure, save any work and restart Fusion before clearing credentials.
If Autodesk opened Fusion during installation, close it before using this launcher so the graphics defaults and helper load.

```sh
./fusion.sh doctor                  # Read-only versions, dependencies, and GPU report.
./fusion.sh desktop                 # Regenerate entries after moving this checkout.
./install.sh --refresh-fusion --accept-third-party-licenses
./fusion.sh uninstall               # Remove desktop entries, preserve application data.
./fusion.sh stop                    # Force-stop ONLY this prefix. SAVE WORK FIRST.
```

Reruns reuse matching runtime builds and completed prerequisite stages.
The installer refuses unmanaged prefixes/runtimes and running applications in the target prefix.
Do not launch Fusion during installation or remove a prefix to fix a build failure.
Inspect the logs first; a runtime interrupted during replacement can leave a `.new` directory for inspection.
A `.previous` runtime may remain after a rebuild.
Back up your prefix before deliberate Wine or DXVK migrations.

To migrate an existing installer-managed installation to the current graphics configuration, save your work and close all of its Wine applications first.
If background services remain, use `./fusion.sh stop` only after saving everything in that prefix.
Then run:

```sh
./install.sh --upgrade-graphics --accept-third-party-licenses
./fusion.sh
```

This explicitly updates DXVK and selects DirectX 11 for both the viewport and UI.
The installer backs up replaced DLLs and the original graphics preferences.
Ordinary launches preserve existing graphics preferences; they do not silently migrate an installation.
Unmanaged prefixes still require a separate, deliberate migration and are not modified by this command.

## Delayed hover highlighting (NVIDIA/XWayland)

`internal/patches/wine-present.patch` fixes a confirmed one-frame-behind viewport
copy on an RTX 2060 with NVIDIA 615.78.08, Sway/XWayland, and Fusion 2705.1.30.
Fusion's offscreen drawable already contained the correct highlight, while the
visible owner matched the previous frame even after two seconds without input.
Reducing DXVK frame latency, waiting for Vulkan presentation, and `XSync` alone
were insufficient. Reading one pixel of the source drawable before Wine's GDI
copy fixed highlighting in the interactive Fusion test.

The Fusion launcher enables `FUSION_WINE_SYNC_PRESENT=1`. The patch retains the
bounded Vulkan present-ID wait (when available), performs the X11 round trip
and one-pixel readback, then uses the existing copy and palette clipping.
This is a synchronous compatibility workaround with potential GPU-stall overhead,
not a general performance improvement. Installers and sign-in callbacks do not
inherit it. For comparison, launch with `FUSION_WINE_SYNC_PRESENT=0 ./fusion.sh`.
No mouse-event injection, periodic redraw helper, or Fusion DLL modification is used.

Existing installations need a Wine rebuild: save and close Fusion, stop remaining
prefix services with `./fusion.sh stop`, then run
`./install.sh --accept-third-party-licenses`. Patch hashes participate in the
runtime fingerprint, so an older runtime cannot silently pass the launch check.
`./fusion.sh doctor` reports whether synchronization is enabled.

Run non-graphical tests with `python -m unittest discover -s tests`.
For an opt-in graphical regression check, compile `tests/present_latency.cpp`
(the build command is in its header), put DXVK's x64 `d3d11.dll` and `dxgi.dll`
beside the executable, and run:

```sh
~/.local/share/fusion-on-wine/python/bin/python tests/check_present_latency.py \
  --runtime ~/.local/share/fusion-on-wine/runtime/wine-11.19 \
  --prefix /absolute/path/to/isolated-test-prefix \
  --exe /absolute/path/to/present_latency.exe
```

The checker briefly focuses its own window and verifies six single-present
colours in the visible X11 owner after 300 ms, without submitting extra frames.
Use `--disable-sync` for an A/B test or `--toplevel` for the non-child control.
Never use the Fusion prefix. The simple fixture does not reproduce every aspect
of Fusion's nested surfaces; real Fusion hover and palette checks remain necessary.

## Paths

- Data, runtime, prefix, Python environment, and build/download cache: `~/.local/share/fusion-on-wine/`.
- Logs: `~/.local/state/fusion-on-wine/logs/`.
- Configuration: `~/.config/fusion-on-wine/config.json`.
- Desktop entries: `~/.local/share/applications/fusion-on-wine*.desktop`.

XDG base-directory overrides are respected.
`--data-dir /absolute/path` chooses another installation directory and records it for later launches.
For manual administration, `FUSION_WINE_HOME`, `FUSION_WINE_PREFIX`, and `FUSION_WINE_RUNTIME` override configured paths.
A callback and its Fusion instance must use the same prefix.
Keep the checkout accessible at its registered path, or regenerate desktop entries after moving it.
Uninstalling preserves models, credentials, configuration, and the runtime.

## What's inside

```text
install.sh                    Install entry point
fusion.sh                     Launch and administration entry point
internal/
  setup.py                    Downloads, Wine build, and installation
  runtime.py                  Configuration, launch, sign-in, and doctor
  versions.json               Pinned versions and download checksums
  helpers/                    Hyprland window handling
  patches/                    Wine window fixes
LICENSE                       Project license and retained third-party notices
```

The Wine patch keeps captionless palettes application-controlled and fixes black per-pixel-alpha areas without making the browser or selection readout opaque.
The complete private runtime includes WoW64 support; it is not a driver mixed with another version of system Wine.
Windows prerequisites are Windows 11 settings, VC++ 2022, and core fonts, with DXVK 3.1.1.
New installations use `driverOptionId="VirtualDeviceDx11"` for the viewport and `graphicsApiOptionId="D3D11"` for the Qt UI.
Fusion launches with `WINEDLLOVERRIDES=d3dcompiler_47,d3dcompiler_43=n,b`, using its bundled native compilers rather than Wine's shader compiler.
These overrides do not apply to prerequisite installers or sign-in callbacks.
The launcher refuses to start if Fusion's bundled `d3dcompiler_47.dll` is missing.
The Photo Booth environment is set once after account settings appear, not reset on every launch.
Autodesk's Qt DLLs and certificate verification remain unchanged.

On Hyprland, the helper repairs modal overlay opacity, stacking, palette focus, and missed palette geometry updates.
It copies confirmed X11 geometry, refuses ambiguous window matches, and acts only on its Fusion process.
The geometry compatibility fix requires scale-1 monitors.

## Compatibility and privacy

The working configuration used CachyOS, Hyprland 0.56.2, XWayland 24.1.14, Wine 11.19, Mesa 26.2.4, an AMD Renoir GPU, and Fusion 2705.1.30.
Face selection, sketch line and point highlights, shading, sketch transitions, and the data panel worked with the all-DirectX configuration.
The window fixes remain necessary; unpatched Wine still renders a black background behind the model browser.
The new clean-machine installer still needs validation on a fresh machine.
Its source-built Wine is not a copy of that machine's distro Wine package.
Other hardware, compositors, and Fusion releases are not claimed to behave identically.

Validation explicitly selected the AMD GPU.
The launcher does not hardcode a GPU or Vulkan ICD; automatic multi-GPU selection remains unverified.
NVIDIA/Sway hover highlighting was subsequently validated with the offscreen presentation workaround above; this is not comprehensive NVIDIA workflow validation.
On multi-GPU systems, `DXVK_FILTER_DEVICE_NAME` can select a device by a substring of its name shown by `./fusion.sh doctor`.
DXVK can still log shared-handle errors even when the data panel renders correctly.
CAM, export, save-and-reopen, and long editing sessions have not been comprehensively verified.
The Qt WebEngine Windows sandbox does not work here, so the launcher disables it; use trusted content and accounts.
Private Wine does not automatically receive system Wine updates.

Never publish or distribute a signed-in prefix, credentials, models, or unreviewed logs/screenshots.
Never share OAuth callback URLs.
This repository ships source and patches only and does not modify system Wine or `~/.wine`.
Project code is MIT-licensed; Wine-derived changes and upstream contributions retain the notices collected in [LICENSE](LICENSE).
Vendor software is downloaded separately and remains subject to its own licenses.

The captionless-popup contribution comes from the [community Fusion-on-Linux project](https://codeberg.org/cryinkfly/Autodesk-Fusion-360-on-Linux).
Other upstream projects are [Wine](https://www.winehq.org/), [Hyprland](https://github.com/hyprwm/Hyprland), [DXVK](https://github.com/doitsujin/dxvk), and [Winetricks](https://github.com/Winetricks/winetricks).
