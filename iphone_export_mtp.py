#!/usr/bin/env python3
r"""
iphone_export_mtp.py — Bulk-export iPhone photos & videos over USB on Windows
with NO Apple software required (no iTunes, no Store app, no WSL).

It uses the exact same channel Windows File Explorer uses to show your iPhone:
MTP (Media Transfer Protocol), driven through the built-in Windows Shell COM
API. Windows installs that driver automatically, so the only thing you install
is this script's two pip packages.

    pip install pywin32 tqdm
    python iphone_export_mtp.py --dest "D:\iPhoneBackup"

Just plug in the iPhone, unlock it, and tap "Trust" / "Allow" once.

See README.md for full details.
"""

import argparse
import os
import sys
import time
import traceback
from datetime import datetime

from tqdm import tqdm

# FOF_* flags passed to Shell CopyHere to make the copy non-interactive:
#   4   FOF_SILENT          - no progress dialog
#   16  FOF_NOCONFIRMATION  - answer "Yes to All"
#   512 FOF_NOCONFIRMMKDIR  - create folders without asking
#   1024 FOF_NOERRORUI      - suppress error pop-ups (we handle errors)
COPY_FLAGS = 4 | 16 | 512 | 1024

# This PC / "Computer" virtual folder, where connected devices appear.
SSF_DRIVES = 17

# Extensions we treat as photos/videos. Used so the whole-device fallback only
# grabs media, not random app data.
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".gif", ".bmp", ".webp", ".dng", ".raw", ".cr2", ".nef", ".aae",
    ".mov", ".mp4", ".m4v", ".avi", ".3gp", ".hevc",
}


def is_media(name):
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def _note(*args, **kwargs):
    """print() that's a no-op when there's no console. The GUI imports this
    module as a library inside a PyInstaller --windowed build, where sys.stdout
    is None; on the CLI, stdout is real and this prints normally."""
    if sys.stdout is None:
        return
    try:
        print(*args, **kwargs)
    except (OSError, ValueError):
        pass


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_prop(item, key):
    """Read an extended shell property, returning None if unavailable."""
    try:
        return item.ExtendedProperty(key)
    except Exception:
        return None


def get_name(item):
    """The real filename, with extension. iPhones expose photos over MTP with
    no extension in item.Name (e.g. 'IMG_0707'), and a single shot can appear as
    several renditions (HEIC/JPG/MOV/AAE) that share that Name. The unique,
    extensioned name lives in System.FileName — use it for filtering, output
    names, and staging so renditions don't collide."""
    return get_prop(item, "System.FileName") or item.Name


_DATE_PROPS = ("System.DateModified", "System.ItemDate",
               "System.Photo.DateTaken", "System.DateCreated")


def get_mtime(item):
    """Best-effort original timestamp; None when MTP exposes no usable date."""
    for p in _DATE_PROPS:
        v = get_prop(item, p)
        if v is None:
            continue
        if isinstance(v, datetime):
            return v
        if hasattr(v, "year"):  # pywintypes time -> plain datetime
            try:
                return datetime(v.year, v.month, v.day, getattr(v, "hour", 0),
                                getattr(v, "minute", 0), getattr(v, "second", 0))
            except Exception:
                pass
    return None


def items_with_retry(folder, tries=5, delay=1.0):
    """Return a folder's Items(), retrying when MTP hands back an empty listing
    on the first call (a common quirk that makes folders look empty)."""
    items = folder.Items()
    for _ in range(tries - 1):
        if items.Count > 0:
            break
        time.sleep(delay)
        items = folder.Items()
    return items


def find_child(folder, name):
    """Find a direct child of a Shell folder by (case-insensitive) name."""
    if folder is None:
        return None
    for item in items_with_retry(folder):
        if item.Name.lower() == name.lower():
            return item
    return None


def find_iphone(shell, wanted):
    """Locate the iPhone device under 'This PC'. Returns a FolderItem or None."""
    this_pc = shell.NameSpace(SSF_DRIVES)
    candidates = []
    for item in this_pc.Items():
        name = item.Name
        low = name.lower()
        if wanted:
            if wanted.lower() in low:
                return item
        elif "iphone" in low or "apple" in low:
            candidates.append(item)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _note("Multiple Apple devices found:")
        for c in candidates:
            _note(f"  - {c.Name}")
        _note("Re-run with --device \"<part of the name>\" to pick one.")
        return candidates[0]
    return None


def get_start_folder(phone_item, full):
    """Drill into the device to the folder we start scanning from.

    iPhones expose 'Internal Storage'; inside it is 'DCIM'. Returns a Shell
    Folder object, or None. Uses only fast direct-child lookups — deep searching
    over MTP is extremely slow.
    """
    root = phone_item.GetFolder
    storage = find_child(root, "Internal Storage")
    storage_folder = storage.GetFolder if storage else root
    if full:
        return storage_folder
    dcim = find_child(storage_folder, "DCIM")
    if dcim:
        return dcim.GetFolder
    _note("No DCIM folder directly visible; scanning the whole device instead "
          "(only photos/videos are copied). Keep the phone unlocked.")
    return storage_folder


def collect(folder, rel, out, media_only, depth=0):
    """Recursively gather (item, relative_dir, size, mtime) for every file."""
    for item in items_with_retry(folder):
        if item.IsFolder:
            sub = os.path.join(rel, item.Name)
            if depth <= 1:
                msg = f"  scanning {sub} ... ({len(out)} media files so far)"
                print(f"\r{msg[:75]:<78}", end="", flush=True)
            collect(item.GetFolder, sub, out, media_only, depth + 1)
        else:
            fname = get_name(item)
            if media_only and not is_media(fname):
                continue
            size = get_prop(item, "System.Size")
            try:
                size = int(size) if size is not None else None
            except (TypeError, ValueError):
                size = None
            mtime = get_mtime(item)
            out.append((item, rel, size, mtime, fname))


def copy_one(shell, item, size, mtime, staging_dir, final_path, timeout):
    """Copy a single MTP item to final_path. Returns 'copied' or 'skipped'."""
    # Resumable: present already and size matches (or size unknown) -> skip.
    if os.path.exists(final_path):
        if size is None or os.path.getsize(final_path) == size:
            return "skipped"

    name = get_name(item)
    staged = os.path.join(staging_dir, name)
    if os.path.exists(staged):
        os.remove(staged)

    shell.NameSpace(staging_dir).CopyHere(item, COPY_FLAGS)

    # CopyHere is asynchronous. Wait until the staged file appears and its size
    # stops growing (and matches the expected size when we know it).
    deadline = time.time() + timeout
    last = -1
    stable = 0
    while time.time() < deadline:
        if os.path.exists(staged):
            cur = os.path.getsize(staged)
            if cur > 0 and cur == last and (size is None or cur == size):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                last = cur
        time.sleep(0.2)
    else:
        raise TimeoutError("copy timed out / never completed")

    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    os.replace(staged, final_path)

    # Preserve the original modification date.
    if isinstance(mtime, datetime):
        try:
            ts = mtime.timestamp()
            os.utime(final_path, (ts, ts))
        except (OSError, ValueError, OverflowError):
            pass

    return "copied"


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-export iPhone photos & videos over USB via MTP "
                    "(no iTunes / no Apple software needed).",
    )
    parser.add_argument("--dest", "-d", required=True,
                        help="Destination folder on the PC, e.g. D:\\iPhoneBackup")
    parser.add_argument("--device", default=None,
                        help="Part of the device name, if you have more than one "
                             "Apple device connected.")
    parser.add_argument("--full", action="store_true",
                        help="Start from the whole Internal Storage instead of "
                             "just DCIM.")
    parser.add_argument("--all", action="store_true",
                        help="Copy every file, not just photos/videos.")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Max seconds to wait for a single (large) file. "
                             "Default 3600.")
    parser.add_argument("--log", default=None,
                        help="Failure log path. Default <dest>\\failures.log")
    args = parser.parse_args()

    if os.name != "nt":
        sys.exit("This MTP version only runs on Windows. "
                 "(On macOS/Linux use iphone_export.py instead.)")

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        sys.exit("pywin32 is not installed.\n"
                 "Install it with:  pip install pywin32 tqdm")

    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("Shell.Application")

    phone = find_iphone(shell, args.device)
    if phone is None:
        sys.exit(
            "No iPhone found under 'This PC'.\n\n"
            "Checklist:\n"
            "  1. Plug the iPhone in with a DATA-capable USB cable.\n"
            "  2. Unlock the phone.\n"
            "  3. Tap 'Trust' / 'Allow' on the prompt that appears on the phone,\n"
            "     then enter your passcode.\n"
            "  4. Open File Explorer -> This PC. The iPhone should appear as a\n"
            "     device. If it does there but not here, re-run this script.\n"
        )
    print(f"Found device: {phone.Name}")

    start = get_start_folder(phone, args.full)
    if start is None:
        sys.exit("Could not open the iPhone's storage. Unlock it and retry.")

    # Validate the destination up front so we fail clearly, not mid-scan.
    dest_root = os.path.abspath(args.dest)
    drive = os.path.splitdrive(dest_root)[0]
    if drive and not os.path.exists(drive + os.sep):
        sys.exit(
            f"The destination drive '{drive}' doesn't exist on this PC.\n"
            f"  You passed: {args.dest}\n\n"
            "Pick a folder on a drive you actually have, e.g.:\n"
            "  --dest \"C:\\Users\\user1\\iPhoneBackup\"\n"
            "(Run 'wmic logicaldisk get name' to list your drive letters.)"
        )

    print("Scanning the iPhone (this can take a minute on large libraries)...")
    records = []
    collect(start, "", records, media_only=not args.all)
    print()  # finish the live "...found N so far" line
    if not records:
        print("No files found. If the phone is locked, unlock it and retry.")
        return

    known = [r[2] for r in records if r[2] is not None]
    total_bytes = sum(known) if known else None
    print(f"Found {len(records)} files"
          + (f" ({human(total_bytes)})." if total_bytes else "."))

    os.makedirs(dest_root, exist_ok=True)
    staging_dir = os.path.join(dest_root, ".staging")
    os.makedirs(staging_dir, exist_ok=True)
    log_path = args.log or os.path.join(dest_root, "failures.log")

    copied = skipped = failed = 0
    failures = []

    bar = tqdm(records, unit="file", desc="Exporting")
    for item, rel, size, mtime, fname in bar:
        final_path = os.path.join(dest_root, rel, fname)
        try:
            result = copy_one(shell, item, size, mtime, staging_dir,
                              final_path, args.timeout)
            if result == "copied":
                copied += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            failures.append((os.path.join(rel, item.Name),
                             f"{type(e).__name__}: {e}", traceback.format_exc()))
        bar.set_postfix(copied=copied, skipped=skipped, failed=failed,
                        refresh=False)
    bar.close()

    # Clean up staging dir if empty.
    try:
        os.rmdir(staging_dir)
    except OSError:
        pass

    print(f"\nDone. Copied {copied}, skipped {skipped} (already present), "
          f"failed {failed}.")
    print(f"Files are in: {dest_root}")

    if failures:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# iphone_export_mtp failures — {datetime.now().isoformat()}\n")
            f.write(f"# {len(failures)} failure(s)\n\n")
            for path, short, tb in failures:
                f.write(f"{path}\n    {short}\n{tb}\n")
        print(f"{failed} failure(s) logged to: {log_path}  (re-run to retry them)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.")
        sys.exit(130)
