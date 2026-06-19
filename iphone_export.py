#!/usr/bin/env python3
"""
iphone_export.py — Bulk-export photos & videos from an iPhone over USB.

Uses pymobiledevice3 (pure-Python libimobiledevice, native Windows support) to
talk to the device's media partition over the Apple File Conduit (AFC) service.
No WSL required. Written for pymobiledevice3 >= 9 (async API).

Two modes:
  * default        — walk the /DCIM folder (fast, camera-roll files).
  * --library      — read the on-device Photos database (Photos.sqlite) for the
                     COMPLETE asset manifest, like iMazing: accurate capture
                     dates, assets outside DCIM, and a precise list of photos
                     that live ONLY in iCloud (written to not_on_device.txt).

    python iphone_export.py --dest "C:\\Users\\me\\iPhoneBackup"
    python iphone_export.py --dest "C:\\Users\\me\\iPhoneBackup" --library
"""

import argparse
import asyncio
import os
import posixpath
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone

from tqdm import tqdm

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".gif", ".bmp", ".webp", ".dng", ".raw", ".cr2", ".nef", ".aae",
    ".mov", ".mp4", ".m4v", ".avi", ".3gp", ".hevc",
}

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB streaming chunks

# Core Data epoch (2001-01-01 UTC) offset from the Unix epoch.
COCOA_EPOCH = 978307200.0

PHOTOS_DB_DIR = "/PhotoData"
PHOTOS_DB_FILES = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def is_media(name):
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


async def afc_stat(afc, path):
    """afc.stat() that returns None instead of raising when the path is absent."""
    try:
        return (await afc.stat(path)) or None
    except Exception:
        return None


async def stream_remote(afc, remote, out, size):
    """Stream a device file into an open binary file object, in chunks."""
    handle = await afc.fopen(remote, "r")
    try:
        remaining = size
        while remaining > 0:
            chunk = await afc.fread(handle, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    finally:
        await afc.fclose(handle)


async def read_remote_bytes(afc, remote, size):
    """Read an entire device file into memory and return its bytes."""
    handle = await afc.fopen(remote, "r")
    buf = bytearray()
    try:
        remaining = size
        while remaining > 0:
            chunk = await afc.fread(handle, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            buf += chunk
            remaining -= len(chunk)
    finally:
        await afc.fclose(handle)
    return bytes(buf)


# iOS keeps the small JPEGs the Photos app shows in its grid (photo previews and
# video poster frames) under a per-asset folder that mirrors the original's path.
THUMB_CACHE_BASES = ("/PhotoData/Thumbnails/V2", "/PhotoData/Thumbnails")


async def detect_thumb_cache(afc):
    """Return the on-device thumbnail-cache base that exists, or None.
    Probed once so we don't pay a failing lookup per asset when it's absent."""
    for base in THUMB_CACHE_BASES:
        if await afc_stat(afc, base):
            return base
    return None


async def find_cached_thumb(afc, base, remote):
    """Find iOS's pre-rendered preview for `remote` (an original's device path).
    The cache stores one folder per asset (named after the original's full path)
    holding rendered JPEGs; we pick the largest. Returns (path, size) or None.
    This is the fast path that also gives video poster frames for free."""
    folder = base + remote          # remote begins with '/'
    try:
        names = await afc.listdir(folder)
    except Exception:
        return None
    best, best_size = None, -1
    for name in names:
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        info = await afc_stat(afc, folder + "/" + name)
        if info:
            size = int(info.get("st_size", 0) or 0)
            if size > best_size:
                best, best_size = folder + "/" + name, size
    return (best, best_size) if best else None


# --------------------------------------------------------------------------- #
# Mode 1: plain folder walk (default)
# --------------------------------------------------------------------------- #

async def scan(afc, source, media_only):
    """Walk the device from `source`, returning a list of file records."""
    files = []
    print(f"Scanning {source} on device (this can take a moment)...")
    async for root, _dirs, names in afc.walk(source):
        for name in names:
            if media_only and not is_media(name):
                continue
            remote = posixpath.join(root, name)
            info = await afc_stat(afc, remote)
            if not info:
                continue
            files.append({
                "remote": remote,
                "rel": remote.lstrip("/"),
                "size": int(info.get("st_size", 0) or 0),
                "mtime": info.get("st_mtime"),
            })
            if len(files) % 200 == 0:
                print(f"\r  ...found {len(files)} files", end="", flush=True)
    print()
    return files


# --------------------------------------------------------------------------- #
# Mode 2: full Photos library (--library)
# --------------------------------------------------------------------------- #

async def pull_photos_db(afc, tmpdir):
    """Stream Photos.sqlite (+ -wal/-shm) off the device into tmpdir."""
    main_local = None
    for fname in PHOTOS_DB_FILES:
        remote = f"{PHOTOS_DB_DIR}/{fname}"
        info = await afc_stat(afc, remote)
        if info is None:
            if fname == "Photos.sqlite":
                raise RuntimeError(
                    f"Couldn't read {remote}. The phone must be unlocked and "
                    "trusted for the Photos database to be accessible."
                )
            continue  # -wal / -shm are optional
        local = os.path.join(tmpdir, fname)
        with open(local, "wb") as out:
            await stream_remote(afc, remote, out, int(info.get("st_size", 0) or 0))
        if fname == "Photos.sqlite":
            main_local = local
    return main_local


def query_assets(db_path):
    """Read every asset from Photos.sqlite. Schema-defensive across iOS versions.
    Returns a list of dicts: {remote, mtime, kind, filename}."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        tables = {r[0] for r in
                  cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = next((t for t in ("ZASSET", "ZGENERICASSET") if t in tables), None)
        if table is None:
            raise RuntimeError("Unrecognized Photos database schema "
                               "(no ZASSET/ZGENERICASSET table).")

        cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}

        def pick(*cands):
            return next((c for c in cands if c in cols), None)

        c_dir = pick("ZDIRECTORY")
        c_file = pick("ZFILENAME")
        c_date = pick("ZDATECREATED")
        c_kind = pick("ZKIND")
        c_trash = pick("ZTRASHEDSTATE")
        if not (c_dir and c_file):
            raise RuntimeError("Photos database has no directory/filename columns.")

        select = f"{c_dir}, {c_file}, {c_date or 'NULL'}, {c_kind or 'NULL'}"
        query = f"SELECT {select} FROM {table}"
        if c_trash:
            query += f" WHERE {c_trash} = 0"  # skip Recently Deleted

        assets = []
        for directory, filename, zdate, kind in cur.execute(query):
            if not directory or not filename:
                continue
            remote = "/" + directory.strip("/") + "/" + filename
            mtime = None
            if zdate is not None:
                try:
                    mtime = datetime.fromtimestamp(zdate + COCOA_EPOCH,
                                                   tz=timezone.utc)
                except (ValueError, OverflowError, OSError):
                    mtime = None
            assets.append({"remote": remote, "mtime": mtime,
                           "kind": kind, "filename": filename})
        return assets
    finally:
        con.close()


async def scan_library(afc):
    """Build the export set from the Photos database, plus a raw DCIM sweep.
    Returns (records, missing) where `missing` = assets only in iCloud."""
    print("Reading the on-device Photos library (Photos.sqlite)...")
    with tempfile.TemporaryDirectory(prefix="iphone_photos_db_") as tmp:
        db_path = await pull_photos_db(afc, tmp)
        assets = query_assets(db_path)
    print(f"Photos library lists {len(assets)} assets. "
          "Checking which are on the device...")

    records = []
    missing = []
    seen = set()
    for a in tqdm(assets, desc="Resolving", unit="asset"):
        info = await afc_stat(afc, a["remote"])
        if info is None:
            missing.append(a)
            continue
        records.append({
            "remote": a["remote"],
            "rel": a["remote"].lstrip("/"),
            "mtime": a["mtime"] or info.get("st_mtime"),
            "size": int(info.get("st_size", 0) or 0),
        })
        seen.add(a["remote"])

    # Union with a raw DCIM walk to catch files not listed as assets (the .MOV
    # half of a Live Photo, .AAE sidecars, etc.).
    for r in await scan(afc, "/DCIM", media_only=True):
        if r["remote"] not in seen:
            records.append(r)
            seen.add(r["remote"])

    return records, missing


def write_missing_report(missing, dest_root):
    path = os.path.join(dest_root, "not_on_device.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {len(missing)} asset(s) are in your Photos library but NOT on "
                "the device (stored only in iCloud).\n")
        f.write("# To export these too: on the iPhone enable\n"
                "#   Settings > Photos > Download and Keep Originals\n"
                "# let it finish over Wi-Fi, then re-run this script.\n\n")
        for a in sorted(missing, key=lambda x: x["filename"]):
            when = a["mtime"].astimezone().strftime("%Y-%m-%d %H:%M:%S") \
                if a["mtime"] else "unknown date"
            f.write(f"{a['filename']}\t{when}\t{a['remote']}\n")
    return path


# --------------------------------------------------------------------------- #
# Copy
# --------------------------------------------------------------------------- #

HEIC_EXTS = (".heic", ".heif")
_heif_ready = False


def ensure_heif():
    """Lazily register HEIF support. Exits with install help if missing."""
    global _heif_ready
    if _heif_ready:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _heif_ready = True
    except ImportError:
        sys.exit("--jpg needs Pillow + pillow-heif for HEIC conversion.\n"
                 "Install them with:  python -m pip install pillow pillow-heif")


def try_register_heif():
    """Register HEIF support if available. Returns True on success, False if
    pillow-heif isn't installed (caller decides how to degrade — no exit)."""
    global _heif_ready
    if _heif_ready:
        return True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _heif_ready = True
        return True
    except Exception:
        return False


def _is_heif(data):
    """Cheap brand sniff for HEIC/HEIF containers (ISO-BMFF 'ftyp' box)."""
    return len(data) >= 24 and data[4:8] == b"ftyp" and any(
        b in data[8:32] for b in (b"heic", b"heix", b"heif", b"mif1", b"msf1"))


def _open_preview(data, box):
    """Open image bytes as a PIL image, decoding as little as possible:
      * HEIC/HEIF — decode the container's embedded thumbnail when present
        (skips decoding the full-resolution image entirely).
      * JPEG — use draft() to decode at a reduced resolution.
    Falls back to a normal full decode for anything else."""
    import io
    from PIL import Image

    if _is_heif(data):
        try:
            import pillow_heif
            heif = pillow_heif.open_heif(data, convert_hdr_to_8bit=True)
            img = heif[0]
            thumbs = sorted((getattr(img, "thumbnails", None) or []),
                            key=lambda t: min(t.size))
            pick = next((t for t in thumbs if min(t.size) >= min(box)),
                        thumbs[-1] if thumbs else None)
            return (pick or img).to_pillow()
        except Exception:
            pass  # fall through to the generic decoder below

    im = Image.open(io.BytesIO(data))
    if im.format == "JPEG":
        try:
            im.draft("RGB", box)   # decoder downscales by 1/2..1/8 while reading
        except Exception:
            pass
    im.load()
    return im


def make_thumbnail(data, dest_path, box=(160, 160)):
    """Decode image bytes (cheaply — see _open_preview), downscale to fit `box`,
    and save a PNG at dest_path. Honors EXIF orientation. Writes atomically.
    Raises on undecodable input."""
    from PIL import ImageOps
    try_register_heif()
    im = _open_preview(data, box)
    im = ImageOps.exif_transpose(im)
    im.thumbnail(box)
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    tmp = dest_path + ".part"
    im.save(tmp, "PNG")
    os.replace(tmp, dest_path)


def convert_heic_to_jpg(src_path, jpg_path, quality=90):
    """Convert a HEIC/HEIF file to JPG, preserving EXIF (incl. capture date)."""
    from PIL import Image
    with Image.open(src_path) as im:
        exif = im.info.get("exif")
        im = im.convert("RGB")
        kwargs = {"quality": quality}
        if exif:
            kwargs["exif"] = exif
        im.save(jpg_path, "JPEG", **kwargs)


MOV_EXTS = (".mov",)


def ffmpeg_path():
    """Return the ffmpeg executable path if available, else None.

    Search order:
      1. A bundled ffmpeg(.exe) inside a PyInstaller one-file build (sys._MEIPASS).
      2. An ffmpeg(.exe) sitting next to this script / the frozen .exe.
      3. ffmpeg on the system PATH.
    """
    import shutil
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"

    candidate_dirs = []
    # 1. PyInstaller unpacks bundled data here at runtime.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate_dirs.append(meipass)
    # 2. Directory of the running .exe (frozen) or this source file.
    if getattr(sys, "frozen", False):
        candidate_dirs.append(os.path.dirname(sys.executable))
    else:
        candidate_dirs.append(os.path.dirname(os.path.abspath(__file__)))

    for d in candidate_dirs:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p

    # 3. Fall back to PATH.
    return shutil.which("ffmpeg")


def convert_mov_to_mp4(src_path, mp4_path):
    """Remux a .MOV into a .MP4 container without re-encoding (fast, lossless).
    iPhone video (H.264/HEVC) plays in .mp4 as-is; we just change the wrapper."""
    import subprocess
    ffmpeg = ffmpeg_path() or "ffmpeg"
    subprocess.run(
        [ffmpeg, "-y", "-i", src_path, "-c", "copy", "-movflags", "+faststart",
         mp4_path],
        check=True, capture_output=True,
    )


def set_mtime(path, mtime):
    if isinstance(mtime, datetime):
        try:
            ts = mtime.timestamp()
            os.utime(path, (ts, ts))
        except (OSError, ValueError, OverflowError):
            pass


async def copy_file(afc, record, dest_root, convert_jpg=False, keep_heic=False,
                    convert_mov=False, keep_mov=False, skip_existing=True):
    """Stream one file to disk and restore its timestamp. Optionally convert
    HEIC photos to JPG and/or MOV videos to MP4. Returns 'copied' or 'skipped'.

    :param skip_existing: when True, files already on disk are skipped (resume).
    """
    rel = record["rel"].replace("/", os.sep)
    local_path = os.path.join(dest_root, rel)
    os.makedirs(os.path.dirname(local_path) or dest_root, exist_ok=True)
    size = record["size"]

    ext = os.path.splitext(local_path)[1].lower()
    want_jpg = convert_jpg and ext in HEIC_EXTS
    want_mp4 = convert_mov and ext in MOV_EXTS
    keep_src = keep_heic if want_jpg else keep_mov

    out_path = None
    if want_jpg:
        out_path = os.path.splitext(local_path)[0] + ".jpg"
    elif want_mp4:
        out_path = os.path.splitext(local_path)[0] + ".mp4"

    have_original = (os.path.exists(local_path)
                     and os.path.getsize(local_path) == size)

    # Resume: skip when everything we'd produce is already present.
    if skip_existing:
        if out_path is not None:
            if os.path.exists(out_path) and (have_original or not keep_src):
                return "skipped"
        elif have_original:
            return "skipped"

    # Fetch the original bytes (unless a good copy is already on disk).
    if not have_original:
        tmp_path = local_path + ".part"
        with open(tmp_path, "wb") as out:
            await stream_remote(afc, record["remote"], out, size)
        os.replace(tmp_path, local_path)
        set_mtime(local_path, record["mtime"])

    # Conversions run off the event loop so the AFC connection keeps breathing.
    if want_jpg:
        await asyncio.to_thread(convert_heic_to_jpg, local_path, out_path)
        set_mtime(out_path, record["mtime"])
        if not keep_heic:
            os.remove(local_path)
    elif want_mp4:
        await asyncio.to_thread(convert_mov_to_mp4, local_path, out_path)
        set_mtime(out_path, record["mtime"])
        if not keep_mov:
            os.remove(local_path)

    return "copied"


# --------------------------------------------------------------------------- #
# Async driver
# --------------------------------------------------------------------------- #

async def run(args):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    try:
        lockdown = await create_using_usbmux()
    except Exception as e:
        sys.exit(
            "Could not connect to an iPhone.\n"
            f"  ({type(e).__name__}: {e})\n\n"
            "Checklist:\n"
            "  1. iPhone plugged in with a data-capable USB cable, unlocked.\n"
            "  2. Tapped 'Trust' on the phone, then entered your passcode.\n"
            "  3. Apple driver installed (iTunes / Apple Devices). Verify with:\n"
            "       python -m pymobiledevice3 usbmux list"
        )

    name = lockdown.all_values.get("DeviceName", "iPhone")
    product = lockdown.all_values.get("ProductType", "")
    print(f"Connected to: {name} ({product})")

    dest_root = os.path.abspath(args.dest)
    drive = os.path.splitdrive(dest_root)[0]
    if drive and not os.path.exists(drive + os.sep):
        sys.exit(f"The destination drive '{drive}' doesn't exist. "
                 f"You passed: {args.dest}")
    os.makedirs(dest_root, exist_ok=True)
    log_path = args.log or os.path.join(dest_root, "failures.log")

    missing = []
    async with AfcService(lockdown=lockdown) as afc:
        try:
            if args.library:
                records, missing = await scan_library(afc)
            else:
                media_only = not args.all
                if args.source.rstrip("/").upper().endswith("DCIM"):
                    media_only = True
                records = await scan(afc, args.source, media_only)
        except Exception as e:
            sys.exit(f"Failed to read from device: {type(e).__name__}: {e}")

        if not records:
            print("No files found to export.")
            if missing:
                report = write_missing_report(missing, dest_root)
                print(f"{len(missing)} asset(s) are iCloud-only — see {report}")
            return

        total_bytes = sum(r["size"] for r in records)
        print(f"Exporting {len(records)} files ({human(total_bytes)})"
              + (f"; {len(missing)} more are only in iCloud." if missing else "."))

        copied = skipped = failed = 0
        consecutive_fail = 0
        aborted = False
        failures = []
        bar = tqdm(total=total_bytes, unit="B", unit_scale=True,
                   unit_divisor=1024, desc="Exporting")
        try:
            for record in records:
                try:
                    result = await copy_file(afc, record, dest_root,
                                             convert_jpg=args.jpg,
                                             keep_heic=args.keep_heic,
                                             convert_mov=args.mp4,
                                             keep_mov=args.keep_mov)
                    copied += result == "copied"
                    skipped += result == "skipped"
                    consecutive_fail = 0
                except Exception as e:
                    failed += 1
                    consecutive_fail += 1
                    failures.append((record["remote"], f"{type(e).__name__}: {e}",
                                     traceback.format_exc()))
                    # A burst of back-to-back failures means the AFC connection
                    # died (phone locked / cable blip), not bad files. Stop now
                    # instead of spamming thousands of instant failures.
                    if consecutive_fail >= 12:
                        aborted = True
                        break
                bar.update(record["size"])
                bar.set_postfix(copied=copied, skipped=skipped, failed=failed,
                                refresh=False)
        finally:
            bar.close()

    if aborted:
        print("\n\nLost the connection to the iPhone (many failures in a row).")
        print("This almost always means the phone LOCKED or the cable/port "
              "hiccuped mid-transfer.")
        print("  1. iPhone: Settings > Display & Brightness > Auto-Lock > Never")
        print("  2. Unlock the phone, reseat the USB cable (no hub).")
        print(f"  3. Re-run the SAME command — it resumes from the {copied} "
              "already copied.")
        return

    print(f"\nDone. Copied {copied}, skipped {skipped} (already present), "
          f"failed {failed}.")
    print(f"Files are in: {dest_root}")

    if missing:
        report = write_missing_report(missing, dest_root)
        print(f"\n{len(missing)} photo(s)/video(s) are stored ONLY in iCloud and "
              "couldn't be exported over USB.")
        print(f"  -> Full list: {report}")
        print("  -> To get them: enable Settings > Photos > Download and Keep "
              "Originals, let it sync, then re-run.")

    if failures:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# iphone_export failures — {datetime.now().isoformat()}\n")
            f.write(f"# {len(failures)} failure(s)\n\n")
            for remote, short, tb in failures:
                f.write(f"{remote}\n    {short}\n{tb}\n")
        print(f"{failed} failure(s) logged to: {log_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-export iPhone photos & videos over USB (no iCloud, no WSL).",
    )
    parser.add_argument("--dest", "-d", required=True,
                        help="Destination folder on the PC.")
    parser.add_argument("--library", action="store_true",
                        help="Read the full Photos database (like iMazing): all "
                             "assets, accurate dates, + an iCloud-only report.")
    parser.add_argument("--source", "-s", default="/DCIM",
                        help="Folder-walk mode: remote start folder. Default /DCIM. "
                             "Use '/' for the whole media partition.")
    parser.add_argument("--all", action="store_true",
                        help="Folder-walk mode: copy every file, not just media.")
    parser.add_argument("--jpg", action="store_true",
                        help="Convert HEIC/HEIF photos to JPG (originals are kept "
                             "as-is by default; use --jpg to also get JPGs).")
    parser.add_argument("--keep-heic", action="store_true",
                        help="With --jpg, also keep the original .HEIC alongside "
                             "the .jpg (default: replace it with the .jpg).")
    parser.add_argument("--mp4", action="store_true",
                        help="Convert .MOV videos to .MP4 (lossless remux, needs "
                             "ffmpeg on PATH).")
    parser.add_argument("--keep-mov", action="store_true",
                        help="With --mp4, also keep the original .MOV.")
    parser.add_argument("--log", default=None,
                        help="Failure log. Default: <dest>/failures.log")
    args = parser.parse_args()

    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        sys.exit("pymobiledevice3 is not installed for THIS python.\n"
                 "Install it with:  python -m pip install pymobiledevice3 tqdm")

    if args.jpg:
        ensure_heif()  # fail fast if Pillow/pillow-heif are missing
    if args.mp4 and ffmpeg_path() is None:
        sys.exit("--mp4 needs ffmpeg on PATH. Install it (e.g. "
                 "`winget install Gyan.FFmpeg`) and reopen your terminal.")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()
