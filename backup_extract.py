#!/usr/bin/env python3
"""
backup_extract.py — turn an iTunes-style device backup into browsable folders.

A full backup (device_backup.py / `pymobiledevice3 backup2 backup --full` /
iTunes) stores every file under a SHA1 hash name. This module maps those hashes
back to real paths using Manifest.db and copies selected app data out into a
normal folder tree, with original modified dates.

Presets:
    whatsapp    WhatsApp photos, videos, voice notes, GIFs, documents (by chat)
    files       the Files app "On My iPhone" storage, incl. its Downloads folder
    voicememos  Voice Memos recordings
    messages    iMessage / SMS attachments
    photos      Camera roll (DCIM) as stored inside the backup

Usage (CLI):
    python backup_extract.py --backup "D:\\iPhoneFullBackup\\<udid>" --dest "D:\\Extracted"
    python backup_extract.py --backup ... --dest ... --presets whatsapp,files
    python backup_extract.py --backup ... --list        (show what the backup holds)

Importable API (used by the GUI):
    info = backup_info(backup_dir)
    summary = extract(backup_dir, dest, presets=("whatsapp",),
                      log=print, progress=lambda done, total: None,
                      cancel=threading.Event())
"""

import argparse
import os
import plistlib
import re
import sqlite3
import sys
from collections import namedtuple

# Media/document extensions worth extracting when a preset filters (WhatsApp
# keeps thousands of .thumb previews we don't want).
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff",
    ".mp4", ".mov", ".3gp", ".m4v", ".avi",
    ".opus", ".m4a", ".aac", ".mp3", ".wav", ".amr", ".caf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".vcf", ".zip",
}

Preset = namedtuple("Preset", "label where media_only")

PRESETS = {
    "whatsapp": Preset(
        "WhatsApp media & documents",
        "domain LIKE '%net.whatsapp%'",
        True),
    "files": Preset(
        "Files app — On My iPhone (incl. Downloads)",
        "domain = 'AppDomainGroup-group.com.apple.FileProvider.LocalStorage'",
        False),
    "voicememos": Preset(
        "Voice Memos recordings",
        "domain = 'MediaDomain' AND relativePath LIKE 'Media/Recordings/%'",
        False),
    "messages": Preset(
        "Messages (iMessage/SMS) attachments",
        "domain = 'MediaDomain' AND relativePath LIKE 'Library/SMS/Attachments/%'",
        False),
    "photos": Preset(
        "Camera roll (DCIM) inside the backup",
        "domain = 'CameraRollDomain' AND relativePath LIKE 'Media/DCIM/%'",
        False),
}

DEST_SUBDIR = {"whatsapp": "WhatsApp", "files": "Files", "voicememos": "VoiceMemos",
               "messages": "MessageAttachments", "photos": "CameraRoll"}

_WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def _find_manifest(backup_dir):
    """Accept either the backup folder itself or its parent (with one child)."""
    direct = os.path.join(backup_dir, "Manifest.db")
    if os.path.isfile(direct):
        return direct
    subs = [os.path.join(backup_dir, d) for d in os.listdir(backup_dir)
            if os.path.isfile(os.path.join(backup_dir, d, "Manifest.db"))]
    if len(subs) == 1:
        return os.path.join(subs[0], "Manifest.db")
    raise FileNotFoundError(
        f"No Manifest.db under {backup_dir!r} — point me at the backup folder "
        "(the one holding Manifest.db, Info.plist, Status.plist).")


def backup_info(backup_dir):
    """Return {'root', 'device', 'date', 'encrypted', 'ios'} for a backup dir."""
    manifest_db = _find_manifest(backup_dir)
    root = os.path.dirname(manifest_db)
    info = {"root": root, "device": "?", "date": None, "encrypted": False,
            "ios": "?"}
    try:
        with open(os.path.join(root, "Manifest.plist"), "rb") as f:
            mp = plistlib.load(f)
        info["encrypted"] = bool(mp.get("IsEncrypted"))
        lockdown = mp.get("Lockdown", {})
        info["device"] = lockdown.get("DeviceName", "?")
        info["ios"] = lockdown.get("ProductVersion", "?")
    except FileNotFoundError:
        pass
    try:
        with open(os.path.join(root, "Status.plist"), "rb") as f:
            info["date"] = plistlib.load(f).get("Date")
    except FileNotFoundError:
        pass
    return info


def _safe_relpath(rel):
    """iOS path -> a path Windows accepts (per-component sanitizing)."""
    parts = [_WIN_BAD.sub("_", p).rstrip(" .") or "_" for p in rel.split("/")]
    return os.sep.join(parts)


def _mtime_from_blob(blob):
    """Original LastModified (unix seconds) from the NSKeyedArchiver metadata."""
    try:
        objs = plistlib.loads(blob)["$objects"]
        lm = objs[1].get("LastModified")
        return lm if isinstance(lm, int) and lm > 0 else None
    except Exception:
        return None


def count_matches(backup_dir, presets):
    """Per-preset (files, bytes) counts, without copying anything."""
    manifest_db = _find_manifest(backup_dir)
    out = {}
    conn = sqlite3.connect(manifest_db)
    try:
        for key in presets:
            p = PRESETS[key]
            rows = conn.execute(
                f"SELECT relativePath FROM Files WHERE flags = 1 AND {p.where}"
            ).fetchall()
            if p.media_only:
                rows = [r for r in rows
                        if os.path.splitext(r[0])[1].lower() in MEDIA_EXTS]
            out[key] = len(rows)
    finally:
        conn.close()
    return out


def extract(backup_dir, dest, presets=("whatsapp",), log=print,
            progress=None, cancel=None, skip_existing=True):
    """Copy the selected presets out of the backup. Returns a summary dict."""
    info = backup_info(backup_dir)
    if info["encrypted"]:
        raise RuntimeError(
            "This backup is encrypted — its file names and contents are "
            "protected with the backup password. Make an unencrypted backup "
            "(or turn off 'Encrypt local backup' in iTunes/Apple Devices) and "
            "try again.")
    root = info["root"]
    manifest_db = os.path.join(root, "Manifest.db")

    conn = sqlite3.connect(manifest_db)
    jobs = []
    try:
        for key in presets:
            p = PRESETS[key]
            for fid, rel, blob in conn.execute(
                    "SELECT fileID, relativePath, file FROM Files "
                    f"WHERE flags = 1 AND {p.where}"):
                if p.media_only and \
                        os.path.splitext(rel)[1].lower() not in MEDIA_EXTS:
                    continue
                jobs.append((key, fid, rel, blob))
    finally:
        conn.close()

    total = len(jobs)
    log(f"{total} files to extract from {info['device']} backup "
        f"({', '.join(PRESETS[k].label for k in presets)}).")
    copied = skipped = missing = failed = 0
    for i, (key, fid, rel, blob) in enumerate(jobs, 1):
        if cancel is not None and cancel.is_set():
            log("Cancelled.")
            break
        src = os.path.join(root, fid[:2], fid)
        out = os.path.join(dest, DEST_SUBDIR[key], _safe_relpath(rel))
        try:
            if not os.path.isfile(src):
                missing += 1
                continue
            size = os.path.getsize(src)
            if skip_existing and os.path.isfile(out) \
                    and os.path.getsize(out) == size:
                skipped += 1
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(src, "rb") as fin, open(out, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
            lm = _mtime_from_blob(blob)
            if lm:
                os.utime(out, (lm, lm))
            copied += 1
        except OSError as e:
            failed += 1
            log(f"FAILED {rel}: {e}")
        if progress is not None and (i % 50 == 0 or i == total):
            progress(i, total)

    summary = {"copied": copied, "skipped": skipped, "missing": missing,
               "failed": failed, "total": total, "dest": dest}
    log(f"Done. Copied {copied}, skipped {skipped} (already present), "
        f"{missing} not in backup, {failed} failed.")
    return summary


def main():
    ap = argparse.ArgumentParser(
        description="Extract browsable media/files from an iTunes-style backup.")
    ap.add_argument("--backup", "-b", required=True,
                    help="Backup folder (holds Manifest.db) or its parent")
    ap.add_argument("--dest", "-d", help="Where to put the extracted folders")
    ap.add_argument("--presets", "-p", default="whatsapp",
                    help="Comma-separated: " + ",".join(PRESETS) + " or 'all'")
    ap.add_argument("--list", action="store_true",
                    help="Only show backup info + per-preset file counts")
    args = ap.parse_args()

    keys = list(PRESETS) if args.presets.strip().lower() == "all" else \
        [k.strip().lower() for k in args.presets.split(",") if k.strip()]
    for k in keys:
        if k not in PRESETS:
            ap.error(f"unknown preset {k!r} — pick from {', '.join(PRESETS)}")

    info = backup_info(args.backup)
    print(f"Backup of {info['device']} (iOS {info['ios']}) from {info['date']}"
          f"{' — ENCRYPTED' if info['encrypted'] else ''}")
    if args.list:
        for k, n in count_matches(args.backup, keys).items():
            print(f"  {k:<12} {n:>7} files   ({PRESETS[k].label})")
        return
    if not args.dest:
        ap.error("--dest is required unless --list is used")
    extract(args.backup, args.dest, presets=keys)


if __name__ == "__main__":
    main()
