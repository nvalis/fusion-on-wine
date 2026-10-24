"""Download, build, and install. Does not alter an existing unmanaged prefix."""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from .runtime import (ROOT, active_prefix_processes, bundled_shader_compiler, find_executable,
                      fusion_version, install_entries, manifest, prepare_preferences)

# Host tools and verified downloads

ARCH_PACKAGES = [
    "base-devel", "mingw-w64-gcc", "python", "python-pip", "python-virtualenv", "cabextract",
    "unzip", "7zip", "curl", "wget", "xdg-utils", "desktop-file-utils", "xorg-xwayland", "mesa", "mesa-utils",
    "vulkan-headers", "vulkan-icd-loader", "vulkan-tools", "libx11", "libxext", "libxrandr",
    "libxcursor", "libxi", "libxfixes", "libxrender", "libxcomposite", "libxinerama",
    "libxxf86vm", "gnutls", "freetype2", "fontconfig", "libpng", "libjpeg-turbo", "libtiff",
    "libunwind", "alsa-lib", "libpulse", "dbus", "cups", "sdl2", "gstreamer",
    "gst-plugins-base-libs", "ffmpeg", "zlib", "bzip2", "xz",
]
REQUIRED_COMMANDS = ["gcc", "g++", "make", "patch", "pkg-config", "flex", "bison",
                     "cabextract", "unzip", "curl", "7z", "xdg-mime", "update-desktop-database"]
REQUIRED_LIBRARIES = ["x11", "xext", "xrandr", "xcursor", "xi", "xfixes", "xrender", "xcomposite",
                      "freetype2", "fontconfig", "gnutls", "gl", "vulkan"]


def host_check():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("This installer supports x86-64 Linux only.")
    if os.geteuid() == 0:
        raise RuntimeError("Run as your normal user, not root. --deps uses sudo only for host packages.")


def run(command, *, env=None, cwd=None, log=None, new_session=False):
    """Never use this for OAuth callbacks: errors include the command arguments."""
    print("Running:", " ".join(str(x) for x in command), flush=True)
    with log.open("a") if log is not None else open(os.devnull, "w") as output:
        result = subprocess.run([str(x) for x in command], env=env, cwd=cwd,
                                stdout=output if log is not None else None,
                                stderr=subprocess.STDOUT if log is not None else None, start_new_session=new_session)
    if result.returncode:
        raise RuntimeError(f"Command failed with status {result.returncode}: {command[0]}." +
                           (f" See {log}." if log is not None else ""))


def dependencies():
    missing = [name for name in REQUIRED_COMMANDS if shutil.which(name) is None]
    if missing:
        raise RuntimeError("Missing host tools: " + ", ".join(missing) + ". See README.md.")
    mingw = ("x86_64-w64-mingw32-gcc", "i686-w64-mingw32-gcc")
    llvm = ("clang", "lld", "llvm-dlltool")
    if not (all(shutil.which(name) for name in mingw) or all(shutil.which(name) for name in llvm)):
        raise RuntimeError("Missing Windows cross-compiler toolchain. Install both "
                           "x86_64-w64-mingw32-gcc and i686-w64-mingw32-gcc, "
                           "or clang, lld, and llvm-dlltool. See README.md.")
    missing = [name for name in REQUIRED_LIBRARIES
               if subprocess.run(["pkg-config", "--exists", name], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode]
    if missing:
        raise RuntimeError("Missing development libraries: " + ", ".join(missing) + ". See README.md.")


def install_dependencies():
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    family = {values.get("ID", ""), *values.get("ID_LIKE", "").split()}
    if not family.intersection({"arch", "cachyos", "manjaro", "endeavouros"}):
        raise RuntimeError("Automatic dependency installation supports Arch-based systems only. See README.md.")
    print("Installing host packages with pacman. Review its transaction before confirming.")
    # A full upgrade avoids unsupported partial upgrades on Arch.
    run(["sudo", "pacman", "-Syu", "--needed", *ARCH_PACKAGES])


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(spec, path, *, refresh=False):
    if not spec["url"].startswith("https://"):
        raise RuntimeError("Downloads require HTTPS.")
    expected = spec.get("sha256")
    if path.exists() and not refresh and expected and sha256(path) == expected:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "fusion-on-wine-installer"})
    try:
        with urllib.request.urlopen(request, timeout=60) as source, temporary.open("wb") as output:
            if not source.geturl().startswith("https://"):
                raise RuntimeError("Refusing a non-HTTPS redirect.")
            shutil.copyfileobj(source, output)
        if expected and sha256(temporary) != expected:
            raise RuntimeError(f"Checksum mismatch for {path.name}; download rejected.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def extract(archive, destination, expected_root):
    """Reject traversal, links, devices, and archives with unexpected top-level paths."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for entry in source:
            parts = PurePosixPath(entry.name)
            if parts.is_absolute() or ".." in parts.parts or not parts.parts or parts.parts[0] != expected_root:
                raise RuntimeError("Unsafe archive path.")
            if not (entry.isfile() or entry.isdir()):
                raise RuntimeError("Archive links and special files are not supported.")
            target = destination.joinpath(*parts.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(entry) as contents, target.open("wb") as output:
                    shutil.copyfileobj(contents, output)
                target.chmod(entry.mode & 0o777)
    return destination / expected_root


def write_json(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(contents, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)

# Private Wine and application installation

PREFIX_MARKER = ".fusion-on-wine-prefix.json"
RUNTIME_MARKER = ".fusion-on-wine-build.json"


def fingerprint(spec):
    return {"wine": spec["wine"]["version"], "source_sha256": spec["wine"]["sha256"],
            "archs": "i386,x86_64", "patches": {p: sha256(ROOT / p) for p in spec["wine"]["patches"]}}


def build_runtime(settings, spec, jobs):
    wanted = fingerprint(spec)
    marker = settings.runtime / RUNTIME_MARKER
    if (marker.is_file() and json.loads(marker.read_text()) == wanted
            and settings.wine.is_file() and settings.server.is_file()):
        print("Patched Wine runtime is already built.")
        return
    if settings.runtime.exists() and any(settings.runtime.iterdir()) and not marker.is_file():
        raise RuntimeError("Refusing to replace an unmanaged Wine runtime. Choose a fresh --data-dir.")
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if str(settings.runtime) + "/" in (process / "maps").read_text():
                raise RuntimeError("This runtime is loaded by a running application, possibly in another prefix. Close it before rebuilding.")
        except OSError:
            pass
    archive = download(spec["wine"], settings.cache / ("wine-" + spec["wine"]["version"] + ".tar.xz"))
    settings.runtime.parent.mkdir(parents=True, exist_ok=True)
    builds = settings.cache / "builds"
    builds.mkdir(parents=True, exist_ok=True)
    log = settings.logs / "wine-build.log"
    with tempfile.TemporaryDirectory(prefix="wine-", dir=builds) as temporary:
        work = Path(temporary)
        source = extract(archive, work / "source", "wine-" + spec["wine"]["version"])
        for patch in spec["wine"]["patches"]:
            run(["patch", "--batch", "--forward", "-p1", "-i", ROOT / patch], cwd=source, log=log)
        build = work / "build"
        build.mkdir()
        run([source / "configure", "--enable-archs=i386,x86_64", "--without-wayland", "--disable-tests",
             "--prefix=" + str(settings.runtime), "--libdir=" + str(settings.runtime / "lib")], cwd=build, log=log)
        run(["make", f"-j{jobs}"], cwd=build, log=log)
        if not (build / "dlls/winex11.drv/winex11.so").is_file():
            raise RuntimeError("Wine was built without its X11 driver; inspect wine-build.log and host dependencies.")
        stage = work / "stage"
        run(["make", "install", "DESTDIR=" + str(stage)], cwd=build, log=log)
        installed = stage.joinpath(*settings.runtime.parts[1:])
        version = subprocess.check_output([installed / "bin/wine", "--version"], text=True).strip()
        if version != "wine-" + spec["wine"]["version"]:
            raise RuntimeError("Built Wine version does not match the manifest.")
        write_json(installed / RUNTIME_MARKER, wanted)
        replacement = settings.runtime.with_name(settings.runtime.name + ".new")
        if replacement.exists():
            raise RuntimeError(f"Interrupted runtime replacement exists: {replacement}. Inspect it before retrying.")
        shutil.move(str(installed), replacement)
    previous = settings.runtime.with_name(settings.runtime.name + ".previous")
    if settings.runtime.exists():
        if previous.exists():
            shutil.rmtree(previous)
        settings.runtime.rename(previous)
    try:
        replacement.rename(settings.runtime)
    except OSError:
        if previous.exists() and not settings.runtime.exists():
            previous.rename(settings.runtime)
        raise


def python_environment(settings):
    location = settings.home / "python"
    run([sys.executable, "-m", "venv", str(location)])
    run([location / "bin/python", "-m", "pip", "install", "--disable-pip-version-check",
         *manifest()["python_requirements"]], log=settings.logs / "python-install.log")


def stop_bootstrap_services(settings):
    # Only used during idle-prefix preparation, never after the Autodesk downloader runs.
    subprocess.run([settings.server, "-k"], env=settings.env(), check=False)
    subprocess.run([settings.server, "-w"], env=settings.env(), check=True, timeout=60)


def setup_prefix(settings, spec):
    marker = settings.prefix / PREFIX_MARKER
    if marker.exists():
        state = json.loads(marker.read_text())
        if state.get("bootstrap_complete") and state.get("wine") == spec["wine"]["version"]:
            print("Windows prerequisites are already installed.")
            return
        try:
            find_executable(settings.prefix)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Existing Fusion has an incomplete bootstrap record; refusing to reinitialize its prefix.")
    if settings.prefix.exists() and any(settings.prefix.iterdir()) and not marker.is_file():
        raise RuntimeError("Refusing to reuse an unmanaged prefix. Your existing models and credentials are untouched.")
    settings.prefix.mkdir(parents=True, exist_ok=True)
    # Write ownership before wineboot so a failed bootstrap can be retried safely.
    if not marker.exists():
        write_json(marker, {"schema": 1, "wine": spec["wine"]["version"]})
    # Wine 11 only loads missing builtin files in bootstrap mode. The ownership
    # marker makes this directory nonempty before Wine sees it, and an interrupted
    # first boot can also leave an incomplete prefix. Plain wineboot -u may then
    # return success without populating syswow64 (or loading kernel32.dll).
    env = settings.env()
    env["WINEBOOTSTRAPMODE"] = "1"
    run([settings.wine, "wineboot.exe", "-u"], env=env, log=settings.logs / "prefix.log")
    stop_bootstrap_services(settings)
    # Test outside bootstrap mode: wineboot's exit status does not reflect all
    # child installation failures. Do not mark a broken prefix complete.
    for directory in ("system32", "syswow64"):
        names = ("kernel32.dll", "reg.exe", "regedit.exe") if directory == "syswow64" else ("kernel32.dll", "reg.exe")
        for name in names:
            if not (settings.prefix / "drive_c/windows" / directory / name).is_file():
                raise RuntimeError(f"Wine bootstrap did not install {directory}/{name}. "
                                   f"See {settings.logs / 'prefix.log'}.")
        run([settings.wine, rf"C:\windows\{directory}\reg.exe", "query", r"HKLM\Software\Microsoft\Windows NT\CurrentVersion"],
            env=settings.env(), log=settings.logs / "prefix.log")
    stop_bootstrap_services(settings)
    tricks = download(spec["winetricks"], settings.cache / ("winetricks-" + spec["winetricks"]["version"]))
    env = settings.env()
    env.update(WINE=str(settings.wine), WINETRICKS_CACHE=str(settings.cache / "winetricks"))
    run(["bash", tricks, "-q", "win11", "vcrun2022", "corefonts"], env=env, log=settings.logs / "winetricks.log")
    stop_bootstrap_services(settings)
    prepare_preferences(settings.prefix, force_graphics=True)
    write_json(marker, {"schema": 1, "wine": spec["wine"]["version"], "bootstrap_complete": True})


def setup_dxvk(settings, spec, *, allow_upgrade=False):
    marker = settings.prefix / PREFIX_MARKER
    state = json.loads(marker.read_text())
    if state.get("dxvk") == spec["dxvk"]["version"]:
        print("Pinned DXVK is already installed.")
        return
    try:
        find_executable(settings.prefix)
    except RuntimeError:
        pass
    else:
        if not allow_upgrade:
            raise RuntimeError("Save and close Fusion, then rerun installation with --upgrade-graphics to update DXVK. "
                               "Existing Fusion was not altered.")
    package = download(spec["dxvk"], settings.cache / ("dxvk-" + spec["dxvk"]["version"] + ".tar.gz"))
    with tempfile.TemporaryDirectory(dir=settings.cache) as temporary:
        source = extract(package, Path(temporary), "dxvk-" + spec["dxvk"]["version"])
        architectures = (("x64", "system32"), ("x32", "syswow64"))
        packages = {arch: sorted((source / arch).glob("*.dll")) for arch, _ in architectures}
        for arch, dlls in packages.items():
            if not {"d3d11.dll", "dxgi.dll"}.issubset(dll.name for dll in dlls):
                raise RuntimeError("DXVK archive is missing its " + arch + " DLLs.")
        if active_prefix_processes(settings.prefix):
            raise RuntimeError("This prefix started Wine applications or services during installation. "
                               "Save and close them before updating DXVK.")
        overrides = []
        for arch, destination in architectures:
            dlls = packages[arch]
            directory = settings.prefix / "drive_c/windows" / destination
            directory.mkdir(parents=True, exist_ok=True)
            for dll in dlls:
                target = directory / dll.name
                backup = target.with_name(target.name + ".before-dxvk-" + state.get("dxvk", "wine"))
                if target.exists() and not backup.exists():
                    shutil.copy2(target, backup)
                # wineboot's DLLs can be symlinks into Wine's installation. Never overwrite their targets.
                staged = target.with_name(target.name + ".new")
                shutil.copy2(dll, staged)
                staged.replace(target)
                if arch == "x64":
                    overrides.append(dll.stem)
        for name in overrides:
            run([settings.wine, "reg.exe", "add", r"HKCU\Software\Wine\DllOverrides", "/v", name,
                 "/t", "REG_SZ", "/d", "native,builtin", "/f"], env=settings.env(), log=settings.logs / "dxvk.log")
    stop_bootstrap_services(settings)
    state["dxvk"] = spec["dxvk"]["version"]
    write_json(marker, state)


def setup_shader_compiler(settings, executable):
    """Stage Fusion's own x64 compiler for callers that explicitly load system32."""
    if not (settings.prefix / PREFIX_MARKER).is_file():
        raise RuntimeError("Refusing to stage a shader compiler in an unmanaged prefix.")
    source = bundled_shader_compiler(executable)
    target = settings.prefix / "drive_c/windows/system32/d3dcompiler_47.dll"
    if target.is_file() and sha256(target) == sha256(source):
        print("Bundled native shader compiler is already staged.")
        return
    if active_prefix_processes(settings.prefix):
        raise RuntimeError("Save and close this prefix's Wine applications and services, then rerun installation "
                           "to stage Fusion's native shader compiler. Existing data was not deleted.")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(target.name + ".before-fusion-native-compiler")
    if target.exists() and not backup.exists():
        shutil.copy2(target, backup)
    temporary = target.with_name(target.name + ".new")
    try:
        shutil.copy2(source, temporary)
        # wineboot may install symlinks. Replace the link, never its runtime target.
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Staged Fusion's bundled native shader compiler in its private prefix.")


def install_fusion(settings, spec, refresh=False):
    try:
        executable = find_executable(settings.prefix)
    except RuntimeError:
        executable = None
    if executable is None or refresh:
        # Autodesk's official downloader is mutable. Do not pretend its hash/version is pinned.
        installer = download(spec["fusion"], settings.cache / "FusionClientDownloader.exe", refresh=True)
        with installer.open("rb") as downloaded:
            if downloaded.read(2) != b"MZ":
                raise RuntimeError("Autodesk's downloader response is not a Windows executable.")
        print("Official Autodesk downloader SHA-256:", sha256(installer))
        run([settings.wine, installer, "--quiet"], env=settings.env(),
            log=settings.logs / "autodesk-installer.log", new_session=True)
        deadline = time.monotonic() + 1800
        while True:
            try:
                executable = find_executable(settings.prefix)
                if (executable.parent / "Neutron/UI/Commands/Resources/Toolbar/Toolbars.xml").is_file():
                    break
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Fusion installation did not finish within 30 minutes. Inspect autodesk-installer.log.")
            time.sleep(5)
    version = fusion_version(executable)
    print("Installed Fusion:", version)
    if version != spec["fusion"]["tested_version"]:
        print("WARNING: This Fusion release differs from the tested version " + spec["fusion"]["tested_version"] + ".")
    write_json(settings.home / "installation.json", {"fusion_version": version, "wine_version": spec["wine"]["version"]})
    return executable


def install(settings, args):
    host_check()
    spec = manifest()
    print("Data:", settings.home)
    print("Prefix:", settings.prefix)
    print("Wine:", settings.runtime)
    print("Wine source and window fixes are pinned; Autodesk supplies the current Fusion release.")
    print("Graphics: DirectX 11 viewport and UI, DXVK " + spec["dxvk"]["version"] + ", bundled shader compilers.")
    if args.dry_run:
        if args.upgrade_graphics:
            print("Existing managed prefix: explicitly upgrade DXVK and select all-DirectX graphics.")
        print("Plan: dependencies if requested; full patched WoW64 Wine build; Python environment; fresh prefix;")
        print("      Windows 11, VC++ 2022, fonts, pinned DXVK, official Fusion download, graphics defaults, desktop entries.")
        print("No downloads, system changes, or application-data changes were made.")
        return
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("Run installation from a graphical session with X11/XWayland, not a headless shell.")
    if not args.accept_third_party_licenses:
        raise RuntimeError("Read README.md and vendor licenses, then pass --accept-third-party-licenses to install.")
    for path, marker in ((settings.prefix, PREFIX_MARKER), (settings.runtime, RUNTIME_MARKER)):
        if path.exists() and any(path.iterdir()) and not (path / marker).is_file():
            raise RuntimeError(f"Refusing to alter unmanaged data at {path}. Choose a fresh --data-dir.")
    settings.home.mkdir(parents=True, exist_ok=True)
    settings.home.chmod(0o700)
    settings.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.logs.chmod(0o700)
    settings.cache.mkdir(parents=True, exist_ok=True)
    with (settings.home / "install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another installation is already running.") from None
        if active_prefix_processes(settings.prefix):
            raise RuntimeError("This prefix has running Wine applications or services. Save and close them before installation.")
        if args.deps:
            install_dependencies()
        dependencies()
        build_runtime(settings, spec, args.jobs)
        python_environment(settings)
        setup_prefix(settings, spec)
        setup_dxvk(settings, spec, allow_upgrade=args.upgrade_graphics)
        if args.upgrade_graphics:
            prepare_preferences(settings.prefix, force_graphics=True)
        executable = install_fusion(settings, spec, args.refresh_fusion)
        setup_shader_compiler(settings, executable)
        install_entries(settings)
    print("Installation complete. Sign in yourself after running ./fusion.sh.")
    print("If Autodesk opened Fusion during installation, close it first so the graphics defaults load on restart.")
