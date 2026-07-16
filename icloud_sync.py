#!/usr/bin/env python3
"""
icloud_sync.py — download the full iCloud Photos library to a local folder.

Wraps the icloudpd CLI (pip install icloudpd) in a subprocess and handles its
interactive moments (Apple ID password, 2FA code) through callbacks, so a GUI
can show dialogs instead of a console. Fully resumable: files already in the
destination are skipped, so re-running continues where it left off.

Why iCloud and not USB?  When the phone runs "Optimize iPhone Storage" the
originals only exist in iCloud — USB export can't reach them. This can.

Usage (CLI):
    python icloud_sync.py --dest "D:\\iCloudPhotos" --username you@example.com

Importable API (used by the GUI):
    run_download(username, dest,
                 ask_password=lambda: "...",   # called once, return password
                 ask_2fa=lambda: "123456",     # called if Apple asks for a code
                 log=print,
                 progress=lambda done, total: None,
                 cancel=threading.Event())
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import threading

_RE_TOTAL = re.compile(r"Downloading\s+(\d+)\s+original", re.I)
_RE_DONE_LINE = re.compile(r"\bDownloaded\s+\S", re.I)
_RE_2FA_PROMPT = re.compile(r"enter\s+two-factor|enter\s+the\s+code|"
                            r"security\s+code", re.I)
_RE_PWD_PROMPT = re.compile(r"iCloud Password for", re.I)


def _icloudpd_cmd():
    exe = shutil.which("icloudpd")
    if exe:
        return [exe]
    return [sys.executable, "-m", "icloudpd"]


def run_download(username, dest, ask_password, ask_2fa=None, log=print,
                 progress=None, cancel=None, size="original"):
    """Run icloudpd until the library is fully downloaded. Returns exit code."""
    os.makedirs(dest, exist_ok=True)
    password = ask_password()
    if not password:
        log("No password given — cancelled.")
        return 1

    cmd = _icloudpd_cmd() + [
        "--directory", dest,
        "--username", username,
        "--password", password,
        "--size", size,
        "--no-progress-bar",
    ]
    log(f"Starting iCloud download for {username} -> {dest}")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    total = [0]
    done = [0]

    def _watch_cancel():
        if cancel is None:
            return
        cancel.wait()
        if proc.poll() is None:
            proc.kill()

    threading.Thread(target=_watch_cancel, daemon=True).start()

    # icloudpd prints its prompts without a trailing newline, so read
    # character-wise and treat both '\n' and ':' as possible line ends.
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if ch == "" and proc.poll() is not None:
            break
        if ch == "":
            continue
        buf += ch
        flushed = None
        if ch == "\n":
            flushed = buf.rstrip("\r\n")
            buf = ""
        elif ch == ":" and _RE_2FA_PROMPT.search(buf):
            flushed = buf
            buf = ""
        if flushed is None:
            continue
        line = flushed.strip()
        if not line:
            continue
        log(line)

        m = _RE_TOTAL.search(line)
        if m:
            total[0] = int(m.group(1))
        elif _RE_DONE_LINE.search(line):
            done[0] += 1
            if progress is not None:
                progress(done[0], max(total[0], done[0]))

        if _RE_2FA_PROMPT.search(line):
            code = (ask_2fa or (lambda: ""))()
            if not code:
                log("No 2FA code given — stopping.")
                proc.kill()
                return 1
            proc.stdin.write(code.strip() + "\n")
            proc.stdin.flush()
        elif _RE_PWD_PROMPT.search(line):
            # only reached if --password was ignored (older icloudpd)
            proc.stdin.write(password + "\n")
            proc.stdin.flush()

    rc = proc.wait()
    if cancel is not None and cancel.is_set():
        log("Cancelled.")
    elif rc == 0:
        log("iCloud download complete — everything is local.")
    else:
        log(f"icloudpd exited with code {rc} — re-run to resume.")
    return rc


def main():
    ap = argparse.ArgumentParser(
        description="Download the full iCloud Photos library (resumable).")
    ap.add_argument("--dest", "-d", required=True, help="Destination folder")
    ap.add_argument("--username", "-u", required=True, help="Apple ID email")
    ap.add_argument("--size", default="original",
                    choices=["original", "medium", "thumb"])
    args = ap.parse_args()
    rc = run_download(
        args.username, args.dest,
        ask_password=lambda: getpass.getpass(f"iCloud password for {args.username}: "),
        ask_2fa=lambda: input("2FA code (check your iPhone): "),
        size=args.size)
    sys.exit(rc)


if __name__ == "__main__":
    main()
