#!/usr/bin/env python3
"""
device_backup.py — full iTunes-style device backup over USB.

Captures *everything* an iTunes backup would: WhatsApp chats + media, voice
memos, Messages attachments, app data, settings. Pair it with
backup_extract.py to pull browsable files back out.

The phone shows a passcode prompt when the backup starts — it must be entered
on the device. The first run copies everything; later runs into the same
folder are incremental unless full=True.

Usage (CLI):
    python device_backup.py --dest "D:\\iPhoneFullBackup"
    python device_backup.py --dest ... --incremental

Importable API (used by the GUI):
    asyncio.run(run_backup(dest, full=True,
                           progress=lambda pct: None, log=print))
"""

import argparse
import asyncio
import os
from pathlib import Path


async def run_backup(dest, full=True, progress=None, log=print):
    """Run the backup; returns the device name. Raises on failure."""
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

    os.makedirs(dest, exist_ok=True)
    lockdown = await create_using_usbmux()
    name = lockdown.all_values.get("DeviceName", "iPhone")
    log(f"Connected to {name}.")
    log("If the iPhone asks for its passcode, enter it on the phone to "
        "authorize the backup.")

    service = Mobilebackup2Service(lockdown)
    last = [-1]

    def _cb(pct):
        if progress is not None:
            progress(pct)
        step = int(pct)
        if step >= last[0] + 5:          # log every ~5%
            last[0] = step
            log(f"Backup {step}% …")

    await service.backup(full=full, backup_directory=Path(dest),
                         progress_callback=_cb)
    log(f"Backup finished: {dest}")
    return name


def main():
    ap = argparse.ArgumentParser(description="Full iPhone backup over USB.")
    ap.add_argument("--dest", "-d", required=True, help="Backup folder")
    ap.add_argument("--incremental", action="store_true",
                    help="Update an existing backup instead of a full one")
    args = ap.parse_args()
    asyncio.run(run_backup(args.dest, full=not args.incremental))


if __name__ == "__main__":
    main()
