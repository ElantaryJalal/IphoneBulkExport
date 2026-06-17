#!/usr/bin/env python3
"""
iphone_export_gui.py — Desktop GUI to bulk-export iPhone photos & videos over USB.

A tkinter front-end over the proven async engine in iphone_export.py
(pymobiledevice3 / AFC). Device I/O runs on a background thread with its own
asyncio loop; all UI updates are marshaled to the main thread through a
thread-safe queue, so the window never freezes.

If the Apple Mobile Device driver (iTunes / Apple Devices app) isn't installed,
usbmuxd is unreachable and AFC can't be used. In that case the app falls back
automatically to MTP (the same channel File Explorer uses) via the built-in
Windows driver — no Apple software required. MTP mode covers DCIM export with
resume + optional conversion, but not the full-library / iCloud-only report.

Flow:
  1. "Scan & Choose…" connects and lists every photo/video.
  2. A selection window lets you tick exactly which items to export
     (with date / type filters and Select-All / None).
  3. Only the ticked items are exported (with resume + optional conversion).

Run:    python iphone_export_gui.py
Deps:   python -m pip install pymobiledevice3 pillow pillow-heif
        (ffmpeg on PATH optional — only for MOV->MP4)
"""

import asyncio
import os
import posixpath
import queue
import tempfile
import threading
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext

import iphone_export as core  # the shared, tested transfer engine

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".hevc"}


def media_kind(name):
    return "Video" if os.path.splitext(name)[1].lower() in VIDEO_EXTS else "Photo"


def _ts(m):
    """A sortable float for a (possibly None / tz-aware / naive) datetime."""
    try:
        return m.timestamp() if m else 0.0
    except Exception:
        return 0.0


def _datestr(m):
    return m.strftime("%Y-%m-%d %H:%M") if m else ""


def _parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- #
# Background device work (async). Runs on the worker thread; only pushes events
# onto a queue — never touches widgets.
# --------------------------------------------------------------------------- #

async def detect_device():
    """Return ('disconnected'|'locked'|'untrusted'|'afc_unavailable', None)
    or ('trusted', name).

    'afc_unavailable' means usbmuxd (the Apple driver) couldn't be reached at
    all — the caller should fall back to MTP.
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.exceptions import (
        NotTrustedError, NotPairedError, PairingDialogResponsePendingError,
        UserDeniedPairingError, PasswordRequiredError, PasscodeRequiredError,
    )
    try:
        from pymobiledevice3.usbmux import list_devices
        devices = await list_devices()
    except Exception:
        # Couldn't even open the usbmuxd socket -> no Apple driver/service.
        return ("afc_unavailable", None)
    if not devices:
        return ("disconnected", None)
    try:
        lockdown = await create_using_usbmux(autopair=False)
        return ("trusted", lockdown.all_values.get("DeviceName", "iPhone"))
    except (PasswordRequiredError, PasscodeRequiredError):
        return ("locked", None)
    except (NotTrustedError, NotPairedError, PairingDialogResponsePendingError,
            UserDeniedPairingError):
        return ("untrusted", None)
    except Exception:
        return ("untrusted", None)


async def _connect(emit):
    from pymobiledevice3.lockdown import create_using_usbmux
    emit("status", "connecting", "Connecting…")
    lockdown = await create_using_usbmux()  # autopair -> Trust dialog if needed
    name = lockdown.all_values.get("DeviceName", "iPhone")
    emit("status", "trusted", f"Connected — {name}")
    emit("log", f"Connected to {name}.")
    return lockdown


async def gui_scan(afc, source, media_only, emit, cancel):
    files = []
    emit("log", f"Scanning {source} …")
    async for root, _dirs, names in afc.walk(source):
        if cancel.is_set():
            break
        for name in names:
            if media_only and not core.is_media(name):
                continue
            remote = posixpath.join(root, name)
            info = await core.afc_stat(afc, remote)
            if not info:
                continue
            files.append({
                "remote": remote, "rel": remote.lstrip("/"),
                "size": int(info.get("st_size", 0) or 0),
                "mtime": info.get("st_mtime"),
            })
            if len(files) % 300 == 0:
                emit("log", f"   …{len(files)} files found")
    return files


async def gui_scan_library(afc, emit, cancel):
    emit("log", "Reading the on-device Photos library (Photos.sqlite)…")
    with tempfile.TemporaryDirectory(prefix="iphone_photos_db_") as tmp:
        db_path = await core.pull_photos_db(afc, tmp)
        assets = core.query_assets(db_path)
    emit("log", f"Library lists {len(assets)} assets. Checking the device…")

    records, missing, seen = [], [], set()
    for i, a in enumerate(assets):
        if cancel.is_set():
            break
        info = await core.afc_stat(afc, a["remote"])
        if info is None:
            missing.append(a)
            continue
        records.append({
            "remote": a["remote"], "rel": a["remote"].lstrip("/"),
            "mtime": a["mtime"] or info.get("st_mtime"),
            "size": int(info.get("st_size", 0) or 0),
        })
        seen.add(a["remote"])
        if i % 1000 == 0:
            emit("log", f"   …resolved {i}/{len(assets)}")

    for r in await gui_scan(afc, "/DCIM", True, emit, cancel):
        if r["remote"] not in seen:
            records.append(r)
            seen.add(r["remote"])
    return records, missing


async def scan_worker(opts, emit, cancel):
    """Connect + scan, then hand the records back to the UI to choose from."""
    from pymobiledevice3.services.afc import AfcService
    try:
        lockdown = await _connect(emit)
    except Exception as e:
        emit("error", f"Could not connect / trust the iPhone.\n"
                      f"{type(e).__name__}: {e}")
        emit("finished", None)
        return
    try:
        async with AfcService(lockdown=lockdown) as afc:
            if opts["library"]:
                records, missing = await gui_scan_library(afc, emit, cancel)
            else:
                records = await gui_scan(afc, "/DCIM", True, emit, cancel)
                missing = []
    except Exception as e:
        emit("error", f"Failed reading the device: {type(e).__name__}: {e}")
        emit("finished", None)
        return

    # Newest first — nicer to scroll/select.
    records.sort(key=lambda r: _ts(r["mtime"]), reverse=True)
    emit("log", f"Found {len(records)} items"
                + (f"; {len(missing)} are iCloud-only." if missing else "."))
    emit("records", records, missing)
    emit("finished", None)


async def copy_records_worker(opts, records, missing, emit, cancel):
    """Reconnect and export the chosen records."""
    from pymobiledevice3.services.afc import AfcService
    try:
        lockdown = await _connect(emit)
    except Exception as e:
        emit("error", f"Could not connect: {type(e).__name__}: {e}")
        emit("finished", None)
        return

    total = len(records)
    emit("phase", f"Exporting {total} files…")
    emit("progress", 0, total)

    copied = skipped = failed = 0
    consecutive_fail = 0
    failures = []
    async with AfcService(lockdown=lockdown) as afc:
        for i, rec in enumerate(records, 1):
            if cancel.is_set():
                emit("log", "Cancelled by user.")
                break
            try:
                result = await core.copy_file(
                    afc, rec, opts["dest"],
                    convert_jpg=opts["convert"], keep_heic=False,
                    convert_mov=opts["convert"] and opts["ffmpeg"], keep_mov=False,
                    skip_existing=opts["skip_existing"],
                )
                if result == "copied":
                    copied += 1
                    emit("log", f"✓ {rec['rel']}")
                else:
                    skipped += 1
                consecutive_fail = 0
            except Exception as e:
                failed += 1
                consecutive_fail += 1
                failures.append((rec["remote"], f"{type(e).__name__}: {e}",
                                 traceback.format_exc()))
                emit("log", f"✗ FAILED {rec['rel']} — {type(e).__name__}: {e}")
                if consecutive_fail >= 12:
                    emit("log", "Connection lost (many failures in a row) — keep "
                                "the phone unlocked and run again to resume.")
                    break
            emit("progress", i, total)

    if failures:
        with open(os.path.join(opts["dest"], "failures.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"# {len(failures)} failure(s) — {datetime.now().isoformat()}\n\n")
            for remote, short, tb in failures:
                f.write(f"{remote}\n    {short}\n{tb}\n")
    if missing:
        core.write_missing_report(missing, opts["dest"])

    emit("summary", {"copied": copied, "skipped": skipped, "failed": failed,
                     "missing": len(missing), "dest": opts["dest"]})
    emit("finished", None)


def run_async(coro_factory):
    asyncio.run(coro_factory())


# --------------------------------------------------------------------------- #
# MTP backend (no Apple driver). Used automatically when usbmuxd is unreachable.
# Windows-only; drives the same channel File Explorer uses, through Shell COM.
# COM apartments are thread-bound, so each worker initializes COM itself and
# re-resolves device folders by path instead of sharing COM objects across
# threads. Records mirror the AFC shape (rel/size/mtime) plus reldir/name so the
# export pass can navigate straight back to each file.
# --------------------------------------------------------------------------- #

def _mtp_detect():
    """('mtp_ready', name) if an iPhone is visible under 'This PC',
    else ('disconnected', None)."""
    if os.name != "nt":
        return ("disconnected", None)
    try:
        import pythoncom
        import win32com.client
        import iphone_export_mtp as mtp
    except Exception:
        return ("disconnected", None)
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        phone = mtp.find_iphone(shell, None)
        return ("mtp_ready", phone.Name) if phone else ("disconnected", None)
    except Exception:
        return ("disconnected", None)
    finally:
        pythoncom.CoUninitialize()


def _mtp_navigate(start_folder, reldir, mtp):
    """Descend from start_folder through a relative dir like '105APPLE'."""
    folder = start_folder
    for part in (reldir or "").replace("\\", "/").split("/"):
        if not part:
            continue
        child = mtp.find_child(folder, part)
        if child is None:
            return None
        folder = child.GetFolder
    return folder


def _mtp_post_convert(final_path, mtime, convert, have_ffmpeg):
    """Mirror core.copy_file's optional HEIC->JPG / MOV->MP4 conversion on a
    freshly-copied original. No-op when convert is off."""
    if not convert:
        return
    ext = os.path.splitext(final_path)[1].lower()
    if ext in (".heic", ".heif"):
        jpg = os.path.splitext(final_path)[0] + ".jpg"
        core.convert_heic_to_jpg(final_path, jpg)
        core.set_mtime(jpg, mtime)
        os.remove(final_path)
    elif ext == ".mov" and have_ffmpeg:
        mp4 = os.path.splitext(final_path)[0] + ".mp4"
        core.convert_mov_to_mp4(final_path, mp4)
        core.set_mtime(mp4, mtime)
        os.remove(final_path)


def mtp_scan_worker(opts, emit, cancel):
    import pythoncom
    import win32com.client
    import iphone_export_mtp as mtp
    pythoncom.CoInitialize()
    try:
        emit("status", "connecting", "Scanning iPhone (MTP — no Apple driver)…")
        shell = win32com.client.Dispatch("Shell.Application")
        phone = mtp.find_iphone(shell, None)
        if phone is None:
            emit("error", "iPhone not found under 'This PC'. Unlock it, tap "
                          "Trust, and confirm it shows in File Explorer.")
            emit("finished", None)
            return
        emit("log", f"Connected to {phone.Name} (MTP).")
        start = mtp.get_start_folder(phone, full=False)
        if start is None:
            emit("error", "Could not open the iPhone's storage. Unlock and retry.")
            emit("finished", None)
            return

        records = []

        def walk(folder, reldir):
            for item in mtp.items_with_retry(folder):
                if cancel.is_set():
                    return
                if item.IsFolder:
                    sub = posixpath.join(reldir, item.Name) if reldir else item.Name
                    walk(item.GetFolder, sub)
                    continue
                fname = mtp.get_name(item)
                if not mtp.is_media(fname):
                    continue
                size = mtp.get_prop(item, "System.Size")
                try:
                    size = int(size) if size is not None else 0
                except (TypeError, ValueError):
                    size = 0
                rel = posixpath.join(reldir, fname) if reldir else fname
                records.append({
                    "rel": rel, "reldir": reldir, "name": fname,
                    "size": size, "mtime": mtp.get_mtime(item),
                })
                if len(records) % 200 == 0:
                    emit("log", f"   …{len(records)} files found")

        emit("log", "Scanning the iPhone (this can take a minute)…")
        walk(start, "")
        records.sort(key=lambda r: _ts(r["mtime"]), reverse=True)
        if not records:
            emit("log", "0 files found — keep the iPhone unlocked and tap "
                        "Allow/Trust on it, then Refresh and try again.")
        else:
            emit("log", f"Found {len(records)} items.")
        emit("records", records, [])
        emit("finished", None)
    except Exception as e:
        emit("error", f"MTP scan failed: {type(e).__name__}: {e}")
        emit("finished", None)
    finally:
        pythoncom.CoUninitialize()


def mtp_copy_worker(opts, records, emit, cancel):
    import pythoncom
    import win32com.client
    import iphone_export_mtp as mtp
    from collections import defaultdict
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        phone = mtp.find_iphone(shell, None)
        if phone is None:
            emit("error", "iPhone not found. Keep it unlocked and connected.")
            emit("finished", None)
            return
        start = mtp.get_start_folder(phone, full=False)
        if start is None:
            emit("error", "Could not open the iPhone's storage.")
            emit("finished", None)
            return

        dest = opts["dest"]
        staging = os.path.join(dest, ".staging")
        os.makedirs(staging, exist_ok=True)
        convert = opts["convert"]
        have_ffmpeg = opts["ffmpeg"]
        if convert:
            try:
                core.ensure_heif()
            except SystemExit:
                convert = False

        total = len(records)
        emit("phase", f"Exporting {total} files…")
        emit("progress", 0, total)

        by_dir = defaultdict(list)
        for r in records:
            by_dir[r["reldir"]].append(r)

        copied = skipped = failed = done = 0
        failures = []
        for reldir, recs in by_dir.items():
            if cancel.is_set():
                emit("log", "Cancelled by user.")
                break
            folder = _mtp_navigate(start, reldir, mtp)
            items_by_name = {}
            if folder is not None:
                for it in mtp.items_with_retry(folder):
                    items_by_name[mtp.get_name(it)] = it
            for r in recs:
                if cancel.is_set():
                    break
                done += 1
                final_path = os.path.join(dest, r["rel"].replace("/", os.sep))
                try:
                    item = items_by_name.get(r["name"])
                    if item is None:
                        raise FileNotFoundError("file no longer on device")
                    result = mtp.copy_one(shell, item, r["size"] or None,
                                          r["mtime"], staging, final_path,
                                          opts.get("timeout", 3600))
                    if result == "copied":
                        _mtp_post_convert(final_path, r["mtime"], convert, have_ffmpeg)
                        copied += 1
                        emit("log", f"✓ {r['rel']}")
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    failures.append((r["rel"], f"{type(e).__name__}: {e}",
                                     traceback.format_exc()))
                    emit("log", f"✗ FAILED {r['rel']} — {type(e).__name__}: {e}")
                emit("progress", done, total)

        try:
            os.rmdir(staging)
        except OSError:
            pass
        if failures:
            with open(os.path.join(dest, "failures.txt"), "w",
                      encoding="utf-8") as f:
                f.write(f"# {len(failures)} failure(s) — {datetime.now().isoformat()}\n\n")
                for path, short, tb in failures:
                    f.write(f"{path}\n    {short}\n{tb}\n")
        emit("summary", {"copied": copied, "skipped": skipped, "failed": failed,
                         "missing": 0, "dest": dest})
        emit("finished", None)
    except Exception as e:
        emit("error", f"MTP export failed: {type(e).__name__}: {e}")
        emit("finished", None)
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------- #
# Selection window (main thread). A checkable table of every item.
# --------------------------------------------------------------------------- #

class SelectionWindow(tk.Toplevel):
    CHECK, UNCHECK = "☑", "☐"

    def __init__(self, master, records, on_export):
        super().__init__(master)
        self.title("Choose items to export")
        self.geometry("780x600")
        self.records = records
        self.on_export = on_export
        self.checked = set(range(len(records)))   # indices ticked; default all

        self._build()
        self._apply_filter()

    def _build(self):
        # Filter bar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Label(bar, text="From").pack(side="left")
        self.from_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.from_var, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="To").pack(side="left")
        self.to_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.to_var, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="(YYYY-MM-DD)").pack(side="left", padx=(0, 10))
        ttk.Label(bar, text="Type").pack(side="left")
        self.type_var = tk.StringVar(value="All")
        ttk.Combobox(bar, textvariable=self.type_var, width=8, state="readonly",
                     values=["All", "Photos", "Videos"]).pack(side="left", padx=4)
        ttk.Label(bar, text="Name").pack(side="left", padx=(8, 0))
        self.search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search_var, width=14).pack(side="left", padx=4)
        ttk.Button(bar, text="Apply filter", command=self._apply_filter).pack(side="left", padx=6)
        ttk.Button(bar, text="Reset", command=self._reset_filter).pack(side="left")

        # Table
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=10)
        cols = ("chk", "name", "date", "type", "size")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for c, txt, w, anchor in (("chk", "", 36, "center"), ("name", "Name", 320, "w"),
                                  ("date", "Date", 150, "w"), ("type", "Type", 70, "center"),
                                  ("size", "Size", 90, "e")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)

        # Bottom controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=8)
        ttk.Button(ctrl, text="Select all (shown)", command=lambda: self._bulk(True)).pack(side="left")
        ttk.Button(ctrl, text="Deselect all (shown)", command=lambda: self._bulk(False)).pack(side="left", padx=6)
        self.count_lbl = ttk.Label(ctrl, text="")
        self.count_lbl.pack(side="left", padx=16)
        self.export_btn = ttk.Button(ctrl, text="Export selected", command=self._export)
        self.export_btn.pack(side="right")

    # --- filtering / populating ---
    def _reset_filter(self):
        self.from_var.set(""); self.to_var.set("")
        self.type_var.set("All"); self.search_var.set("")
        self._apply_filter()

    def _matches(self, rec):
        d_from, d_to = _parse_date(self.from_var.get()), _parse_date(self.to_var.get())
        t = self.type_var.get()
        needle = self.search_var.get().strip().lower()
        name = os.path.basename(rec["rel"])
        if t == "Photos" and media_kind(name) != "Photo":
            return False
        if t == "Videos" and media_kind(name) != "Video":
            return False
        if needle and needle not in name.lower():
            return False
        if d_from or d_to:
            m = rec["mtime"]
            rd = m.date() if m else None
            if rd is None:
                return False
            if d_from and rd < d_from:
                return False
            if d_to and rd > d_to:
                return False
        return True

    def _apply_filter(self):
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for idx, rec in enumerate(self.records):
            if not self._matches(rec):
                continue
            name = os.path.basename(rec["rel"])
            glyph = self.CHECK if idx in self.checked else self.UNCHECK
            self.tree.insert("", "end", iid=str(idx),
                             values=(glyph, name, _datestr(rec["mtime"]),
                                     media_kind(name), core.human(rec["size"])))
            shown += 1
            if shown % 3000 == 0:
                self.update_idletasks()
        self._shown_count = shown
        self._update_count()

    # --- checkbox interaction ---
    def _toggle(self, iid):
        idx = int(iid)
        if idx in self.checked:
            self.checked.discard(idx)
            self.tree.set(iid, "chk", self.UNCHECK)
        else:
            self.checked.add(idx)
            self.tree.set(iid, "chk", self.CHECK)
        self._update_count()

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) == "#1":  # the checkbox column
            row = self.tree.identify_row(event.y)
            if row:
                self._toggle(row)
                return "break"  # don't also change the row highlight

    def _on_space(self, _event):
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _bulk(self, select):
        for iid in self.tree.get_children():
            idx = int(iid)
            if select:
                self.checked.add(idx)
                self.tree.set(iid, "chk", self.CHECK)
            else:
                self.checked.discard(idx)
                self.tree.set(iid, "chk", self.UNCHECK)
        self._update_count()

    def _update_count(self):
        self.count_lbl.config(
            text=f"{len(self.checked)} selected  ·  {self._shown_count} shown  "
                 f"·  {len(self.records)} total")

    def _export(self):
        selected = [self.records[i] for i in sorted(self.checked)]
        if not selected:
            self.count_lbl.config(text="Nothing selected — tick at least one item.")
            return
        self.destroy()
        self.on_export(selected)


# --------------------------------------------------------------------------- #
# Main window.
# --------------------------------------------------------------------------- #

class ExporterApp:
    DOT = {"disconnected": "#c0392b", "locked": "#e67e22",
           "untrusted": "#e67e22", "connecting": "#2980b9", "trusted": "#27ae60"}

    def __init__(self, root):
        self.root = root
        root.title("iPhone Photo & Video Exporter")
        root.geometry("720x620")
        root.minsize(640, 560)

        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.scan_opts = {}
        self.pending_missing = []
        self.backend = "afc"   # flips to "mtp" when the Apple driver is absent

        self._build_ui()
        self._set_status("connecting", "Checking for iPhone…")
        self.detect()
        self.root.after(80, self._poll)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        status = ttk.Frame(self.root)
        status.pack(fill="x", **pad)
        self.dot = tk.Canvas(status, width=16, height=16, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 14, 14, fill="#999", outline="")
        self.dot.pack(side="left")
        self.status_lbl = ttk.Label(status, text="…", font=("Segoe UI", 10, "bold"))
        self.status_lbl.pack(side="left", padx=8)
        ttk.Button(status, text="Refresh", command=self.detect).pack(side="right")

        dest = ttk.Frame(self.root)
        dest.pack(fill="x", **pad)
        ttk.Label(dest, text="Save to:").pack(side="left")
        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "iPhoneBackup"))
        ttk.Entry(dest, textvariable=self.dest_var).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(dest, text="Browse…", command=self.browse).pack(side="left")

        opts = ttk.LabelFrame(self.root, text="Options")
        opts.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="keep")
        ttk.Radiobutton(opts, text="Keep Originals (HEIC / MOV)",
                        variable=self.mode, value="keep").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        ttk.Radiobutton(opts, text="Convert to JPG / MP4",
                        variable=self.mode, value="convert").grid(row=0, column=1, sticky="w", padx=10, pady=4)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Skip already-transferred files (resume)",
                        variable=self.skip_var).grid(row=1, column=0, sticky="w", padx=10, pady=4)
        self.lib_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Read full photo library (recommended)",
                        variable=self.lib_var).grid(row=1, column=1, sticky="w", padx=10, pady=4)

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctrl, text="Scan & Choose…", command=self.scan_and_choose)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(ctrl, text="Cancel", command=self.do_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        self.prog = ttk.Progressbar(self.root, mode="determinate")
        self.prog.pack(fill="x", **pad)
        self.prog_lbl = ttk.Label(self.root, text="Idle.")
        self.prog_lbl.pack(anchor="w", padx=10)

        self.log = scrolledtext.ScrolledText(self.root, height=12, state="disabled",
                                             font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True, padx=10, pady=8)

    # --- widget helpers (main thread) ---
    def _set_status(self, kind, text):
        self.dot.itemconfig(self.dot_id, fill=self.DOT.get(kind, "#999"))
        self.status_lbl.config(text=text)

    def _append_log(self, line):
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def browse(self):
        d = filedialog.askdirectory(title="Choose destination folder")
        if d:
            self.dest_var.set(d)

    def detect(self):
        self._set_status("connecting", "Checking for iPhone…")

        def work():
            try:
                state = asyncio.run(detect_device())
            except Exception:
                state = ("afc_unavailable", None)
            if state[0] in ("afc_unavailable", "disconnected"):
                # AFC gave us nothing usable (no Apple driver, or the phone is
                # only visible over MTP). Try the no-Apple-software MTP path;
                # if that finds the phone we use it, otherwise it's truly absent.
                mtp_state = _mtp_detect()
                state = mtp_state if mtp_state[0] == "mtp_ready" else ("disconnected", None)
            self.events.put(("detect", state))

        threading.Thread(target=work, daemon=True).start()

    # --- phase 1: scan & choose ---
    def scan_and_choose(self):
        dest = self.dest_var.get().strip()
        if not dest:
            self._append_log("Pick a destination folder first.")
            return
        drive = os.path.splitdrive(os.path.abspath(dest))[0]
        if drive and not os.path.exists(drive + os.sep):
            self._append_log(f"Drive {drive} doesn't exist — pick another folder.")
            return
        os.makedirs(dest, exist_ok=True)

        convert = self.mode.get() == "convert"
        if convert:
            try:
                core.ensure_heif()
            except SystemExit as e:
                self._append_log(str(e))
                return
        have_ffmpeg = core.ffmpeg_path() is not None
        if convert and not have_ffmpeg:
            self._append_log("Note: ffmpeg not found — videos kept as .MOV.")

        library = self.lib_var.get()
        if self.backend == "mtp" and library:
            self._append_log("Note: full-library mode needs the Apple driver; "
                             "scanning DCIM over MTP instead.")
            library = False

        self.scan_opts = {"dest": os.path.abspath(dest), "convert": convert,
                          "ffmpeg": have_ffmpeg, "skip_existing": self.skip_var.get(),
                          "library": library, "backend": self.backend}

        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog.config(value=0)
        self.prog_lbl.config(text="Scanning…")
        self._append_log("──────── scanning device ────────")

        if self.backend == "mtp":
            target = (lambda: mtp_scan_worker(self.scan_opts, self._emit, self.cancel))
        else:
            target = (lambda: run_async(
                lambda: scan_worker(self.scan_opts, self._emit, self.cancel)))
        threading.Thread(target=target, daemon=True).start()

    # --- phase 2: export the chosen items ---
    def begin_export(self, selected):
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog.config(value=0)
        self.prog_lbl.config(text="Starting…")
        self._append_log(f"──────── exporting {len(selected)} selected items ────────")
        if self.scan_opts.get("backend") == "mtp":
            target = (lambda: mtp_copy_worker(self.scan_opts, selected,
                                              self._emit, self.cancel))
        else:
            target = (lambda: run_async(
                lambda: copy_records_worker(self.scan_opts, selected,
                                            self.pending_missing, self._emit, self.cancel)))
        threading.Thread(target=target, daemon=True).start()

    def do_cancel(self):
        self.cancel.set()
        self.cancel_btn.config(state="disabled")
        self.prog_lbl.config(text="Cancelling…")

    def _emit(self, kind, *payload):
        self.events.put((kind, payload if len(payload) != 1 else payload[0]))

    # --- UI event pump ---
    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle(self, kind, payload):
        if kind == "detect":
            state, name = payload
            msg = {"disconnected": ("disconnected", "No iPhone detected — plug it in via USB."),
                   "locked": ("locked", "iPhone connected but locked — unlock it."),
                   "untrusted": ("untrusted", "iPhone connected — tap “Trust”, then Refresh.")}
            if state == "trusted":
                self.backend = "afc"
                self._set_status("trusted", f"Connected & trusted — {name}")
            elif state == "mtp_ready":
                self.backend = "mtp"
                self._set_status("trusted",
                                 f"Connected — {name}  ·  MTP mode (no Apple driver)")
            else:
                if state in ("locked", "untrusted"):
                    self.backend = "afc"
                self._set_status(*msg.get(state, ("disconnected", "No iPhone.")))
        elif kind == "status":
            self._set_status(payload[0], payload[1])
        elif kind == "log":
            self._append_log(payload)
        elif kind == "phase":
            self.prog_lbl.config(text=payload)
        elif kind == "progress":
            done, total = payload
            self.prog.config(maximum=max(total, 1), value=done)
            self.prog_lbl.config(text=f"{done} / {total} files")
        elif kind == "error":
            self._append_log("ERROR: " + payload)
        elif kind == "records":
            records, missing = payload
            self.pending_missing = missing
            if not records:
                self._append_log("No items found to choose from.")
                return
            SelectionWindow(self.root, records, on_export=self.begin_export)
        elif kind == "summary":
            s = payload
            msg = (f"Done. Copied {s['copied']}, skipped {s['skipped']}, "
                   f"failed {s['failed']}.")
            if s["missing"]:
                msg += f" {s['missing']} iCloud-only (not_on_device.txt)."
            self._append_log("──────── " + msg + " ────────")
            self.prog_lbl.config(text=msg)
            if s["failed"]:
                self._append_log("Failures saved to "
                                 + os.path.join(s["dest"], "failures.txt"))
        elif kind == "finished":
            self.start_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")


def main():
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        import sys
        root = tk.Tk(); root.withdraw()
        from tkinter import messagebox
        messagebox.showerror(
            "Missing dependency",
            "pymobiledevice3 is not installed for this Python.\n\n"
            "Install with:\n  python -m pip install pymobiledevice3 pillow pillow-heif")
        sys.exit(1)

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ExporterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
