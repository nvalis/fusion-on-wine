#!/usr/bin/env python3
"""Restore original browser/readout transparency, or disable it as a fallback."""
import argparse
from pathlib import Path
import re


BROWSER_PARAMETERS = re.compile(
    r'<Parameters\b(?=[^>]*\bTitle="LOCID=Browser")[^>]*>'
)
MESSAGE_TRAY_TOOLBAR = re.compile(
    r'<Toolbar\b(?=[^>]*\bId="MessageTray")[^>]*>'
)


def decode_xml(original):
    encoding = "utf-8"
    if original.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    elif original.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
    return encoding, original.decode(encoding)


def make_opaque(path, pattern, attribute, backup_suffix):
    original = path.read_bytes()
    encoding, text = decode_xml(original)

    def replace(match):
        tag = match.group()
        if f'{attribute}="' in tag:
            return re.sub(rf'\b{attribute}="[^"]*"', f'{attribute}="False"', tag)
        return re.sub(r'(/?>)$', rf' {attribute}="False"\1', tag)

    updated = pattern.sub(replace, text)
    if updated == text:
        return False
    backup = path.with_name(path.name + backup_suffix)
    if not backup.exists():
        backup.write_bytes(original)
    path.write_bytes(updated.encode(encoding))
    return True


def make_browser_opaque(path):
    return make_opaque(path, BROWSER_PARAMETERS, "Transparent", ".before-opaque-browser")


def make_message_tray_opaque(path):
    return make_opaque(path, MESSAGE_TRAY_TOOLBAR, "TransparentBackground", ".before-opaque-message-tray")


def restore_transparency(path, pattern, attribute, backup_suffix):
    backup = path.with_name(path.name + backup_suffix)
    if not backup.is_file():
        return False
    original = path.read_bytes()
    encoding, text = decode_xml(original)
    _, saved = decode_xml(backup.read_bytes())
    tags = pattern.findall(saved)
    if len(tags) != len(pattern.findall(text)):
        print(f"Skipped {path.name}: backup transparency targets differ")
        return False
    saved_tags = iter(tags)

    def replace(match):
        tag = match.group()
        previous = re.search(rf'\b{attribute}="[^"]*"', next(saved_tags))
        if previous is None:
            return re.sub(rf'\s+{attribute}="[^"]*"', "", tag)
        if re.search(rf'\b{attribute}="[^"]*"', tag):
            return re.sub(rf'\b{attribute}="[^"]*"', lambda _: previous.group(), tag)
        return re.sub(r'(/?>)$', lambda m: " " + previous.group() + m.group(), tag)

    updated = pattern.sub(replace, text)
    if updated == text:
        return False
    rollback = path.with_name(path.name + ".before-native-transparency")
    if not rollback.exists():
        rollback.write_bytes(original)
    path.write_bytes(updated.encode(encoding))
    return True


def configure(executable, prefix, restore=False):
    # New documents reload the shipped layout, not just NULastDisplayedLayout.
    default = executable.parent / "Neutron/UI/Commands/Resources/Layouts/AssetShellLayout.xml"
    options = prefix / "drive_c/users"
    paths = [default, *options.glob("*/AppData/Roaming/Autodesk/Neutron Platform/Options/*/NULastDisplayedLayout.xml")]
    for path in paths:
        if not path.is_file():
            continue
        if restore:
            changed = restore_transparency(path, BROWSER_PARAMETERS, "Transparent", ".before-opaque-browser")
        else:
            changed = make_browser_opaque(path)
        if changed:
            print(f"{'Restored' if restore else 'Disabled'} browser transparency in {path.name}")
    toolbar = executable.parent / "Neutron/UI/Commands/Resources/Toolbar/Toolbars.xml"
    if toolbar.is_file():
        if restore:
            changed = restore_transparency(toolbar, MESSAGE_TRAY_TOOLBAR, "TransparentBackground", ".before-opaque-message-tray")
        else:
            changed = make_message_tray_opaque(toolbar)
        if changed:
            print(f"{'Restored' if restore else 'Disabled'} selection-readout transparency in {toolbar.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--restore", action="store_true", help="Restore only the backed-up transparency attributes")
    args = parser.parse_args()
    configure(args.executable, args.prefix, restore=args.restore)


if __name__ == "__main__":
    main()
