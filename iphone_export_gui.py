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
import hashlib
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
# Thumbnail service (AFC). A background thread holds one live AFC connection and
# turns photo records into small PNG thumbnails on demand. Requests are served
# newest-first (LIFO) so fast scrolling prioritizes what's currently on screen;
# thumbnails are cached to a temp dir keyed by path+size+mtime, so re-scrolling
# (and re-runs) never re-fetch. Only the main thread touches widgets — results
# are handed back through a thread-safe queue.
# --------------------------------------------------------------------------- #

class ThumbnailService:
    THUMB_BOX = (256, 256)     # master size; tiles scale down from this
    CONCURRENCY = 4            # parallel AFC connections (one socket is serial)
    MAX_QUEUE = 400            # drop the oldest pending when scrolling far/fast

    def __init__(self):
        self.cache_dir = os.path.join(tempfile.gettempdir(),
                                      "iphone_export_thumbs")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.results = queue.Queue()           # (remote, png_path | None)
        self._lock = threading.Lock()
        self._stack = []                       # LIFO of (remote, size, mtime)
        self._pending = set()                  # remotes queued or in flight
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    def shutdown(self):
        self._stop.set()

    def cached_path(self, remote, size, mtime):
        key = hashlib.md5(f"{remote}|{size}|{_ts(mtime)}".encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".png")

    def request(self, remote, size, mtime, is_video=False):
        """Queue a thumbnail fetch. No-op if it's already cached or in flight."""
        with self._lock:
            if remote in self._pending:
                return
            self._pending.add(remote)
            self._stack.append((remote, size, mtime, is_video))
            # Bound the backlog so a long fast scroll doesn't pile up stale work.
            while len(self._stack) > self.MAX_QUEUE:
                old = self._stack.pop(0)
                self._pending.discard(old[0])

    def _next(self):
        with self._lock:
            return self._stack.pop() if self._stack else None

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            pass

    async def _serve(self):
        # Each worker owns its own AFC connection so transfers run in parallel
        # (a single AFC socket is strictly request/response).
        workers = [asyncio.create_task(self._worker())
                   for _ in range(self.CONCURRENCY)]
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
        try:
            lockdown = await create_using_usbmux()
        except Exception:
            return
        try:
            async with AfcService(lockdown=lockdown) as afc:
                # iOS's pre-rendered preview cache (fast path; gives video posters).
                cache_base = await core.detect_thumb_cache(afc)
                while not self._stop.is_set():
                    req = self._next()
                    if req is None:
                        await asyncio.sleep(0.05)
                        continue
                    remote, size, mtime, is_video = req
                    path = self.cached_path(remote, size, mtime)
                    try:
                        if not os.path.exists(path):
                            await self._build(afc, cache_base, remote, size,
                                              is_video, path)
                        self.results.put((remote, path))
                    except Exception:
                        self.results.put((remote, None))
                    finally:
                        with self._lock:
                            self._pending.discard(remote)
        except Exception:
            return

    async def _build(self, afc, cache_base, remote, size, is_video, path):
        """Produce one thumbnail PNG at `path`. Prefers iOS's pre-rendered
        preview (tiny, and the only cheap source for video); for photos, falls
        back to decoding the original. Videos with no cached poster are skipped."""
        if cache_base:
            found = await core.find_cached_thumb(afc, cache_base, remote)
            if found:
                tremote, tsize = found
                data = await core.read_remote_bytes(afc, tremote, tsize)
                await asyncio.to_thread(core.make_thumbnail, data, path,
                                        self.THUMB_BOX)
                return
        if is_video:
            raise RuntimeError("no cached poster for video")
        data = await core.read_remote_bytes(afc, remote, size)
        await asyncio.to_thread(core.make_thumbnail, data, path, self.THUMB_BOX)


# --------------------------------------------------------------------------- #
# Selection window (main thread). A lazy-loading thumbnail grid of every item.
# --------------------------------------------------------------------------- #

class SelectionWindow(tk.Toplevel):
    SIZES = {"Small": 112, "Medium": 152, "Large": 200}
    PHOTO_CAP = 500             # max decoded PhotoImages kept in memory

    # palette
    BG = "#f4f5f7"
    CARD = "#ffffff"
    CARD_SEL = "#eaf2ff"
    BORDER = "#e2e5ea"
    BORDER_HOVER = "#b7c4d6"
    ACCENT = "#2f6fdb"
    VIDEO_BG = "#2b2f36"
    NAME_FG = "#555555"

    def __init__(self, master, records, on_export, thumbs=None):
        super().__init__(master)
        self.title("Choose items to export")
        self.geometry("1000x720")
        self.minsize(560, 480)
        self.configure(bg=self.BG)
        self.records = records
        self.on_export = on_export
        self.thumbs = thumbs                       # ThumbnailService or None
        self.checked = set(range(len(records)))    # record indices ticked
        self.view = list(range(len(records)))      # record indices after filter
        self.photo = {}                            # idx -> PhotoImage (LRU-ish)
        self.failed = set()                        # idx with no usable preview
        self.cols = 1
        self.hover = None                          # idx under the cursor
        self.anchor = None                         # view-pos for shift-range
        self._last_w = 0
        self._content_h = 1
        self._alive = True
        self.remote_to_idx = {r["remote"]: i for i, r in enumerate(records)
                              if r.get("remote")}

        # tile geometry (recomputed when the size selector changes)
        self.ip = 8                                # inner pad around the image
        self.label_h = 24                          # filename strip
        self.gap = 14                              # space between cards
        self.thumb = self.SIZES["Medium"]
        self._recompute_dims()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Control-a>", lambda _e: self._bulk(True))
        self.bind("<Control-A>", lambda _e: self._bulk(True))
        if self.thumbs is not None:
            self.thumbs.start()
            self.after(120, self._drain_thumbs)
        self._apply_filter()

    def _recompute_dims(self):
        self.card_w = self.thumb + 2 * self.ip
        self.card_h = self.thumb + 2 * self.ip + self.label_h
        self.cell_w = self.card_w + self.gap
        self.cell_h = self.card_h + self.gap
        self.margin = self.gap

    def _build(self):
        # Filter / toolbar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(bar, text="From").pack(side="left")
        self.from_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.from_var, width=11).pack(side="left", padx=(2, 6))
        ttk.Label(bar, text="To").pack(side="left")
        self.to_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.to_var, width=11).pack(side="left", padx=(2, 6))
        ttk.Label(bar, text="(YYYY-MM-DD)").pack(side="left", padx=(0, 10))
        ttk.Label(bar, text="Type").pack(side="left")
        self.type_var = tk.StringVar(value="All")
        cb = ttk.Combobox(bar, textvariable=self.type_var, width=8, state="readonly",
                          values=["All", "Photos", "Videos"])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())
        ttk.Label(bar, text="Name").pack(side="left", padx=(8, 0))
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=14)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda _e: self._apply_filter())
        ttk.Button(bar, text="Apply", command=self._apply_filter).pack(side="left", padx=6)
        ttk.Button(bar, text="Reset", command=self._reset_filter).pack(side="left")

        # Size selector (right side)
        self.size_var = tk.StringVar(value="Medium")
        size_cb = ttk.Combobox(bar, textvariable=self.size_var, width=8,
                               state="readonly", values=list(self.SIZES))
        size_cb.pack(side="right")
        size_cb.bind("<<ComboboxSelected>>", self._on_size_change)
        ttk.Label(bar, text="Size").pack(side="right", padx=(0, 4))

        ttk.Label(self, text="Click a tile to select · Shift-click for a range · "
                             "Ctrl+A selects all shown",
                  foreground="#8a8f98").pack(anchor="w", padx=14, pady=(0, 4))

        # Thumbnail grid (virtualized canvas — only visible tiles are drawn)
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=12)
        self.canvas = tk.Canvas(wrap, bg=self.BG, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<MouseWheel>", self._on_wheel)        # Windows / macOS
        self.canvas.bind("<Button-4>", self._on_wheel)          # Linux up
        self.canvas.bind("<Button-5>", self._on_wheel)          # Linux down

        # Bottom controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=12, pady=8)
        ttk.Button(ctrl, text="Select all (shown)", command=lambda: self._bulk(True)).pack(side="left")
        ttk.Button(ctrl, text="Deselect all (shown)", command=lambda: self._bulk(False)).pack(side="left", padx=6)
        self.count_lbl = ttk.Label(ctrl, text="")
        self.count_lbl.pack(side="left", padx=16)
        self.export_btn = ttk.Button(ctrl, text="Export selected", command=self._export)
        self.export_btn.pack(side="right")

    # --- filtering ---
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
        self.view = [i for i, rec in enumerate(self.records) if self._matches(rec)]
        self.anchor = None
        self.canvas.yview_moveto(0)
        self._relayout()
        self._update_count()

    def _on_size_change(self, _event=None):
        self.thumb = self.SIZES.get(self.size_var.get(), self.SIZES["Medium"])
        self.photo.clear()          # re-render from disk masters at the new size
        self._recompute_dims()
        self._relayout()

    # --- virtualized grid ---
    def _on_resize(self, event):
        if event.width != self._last_w:
            self._last_w = event.width
            self._relayout()

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._redraw()

    def _scroll_by(self, dy):
        view_h = self.canvas.winfo_height()
        total = max(self._content_h, view_h)
        max_top = max(total - view_h, 0)
        new = min(max(self.canvas.canvasy(0) + dy, 0), max_top)
        self.canvas.yview_moveto(new / total if total else 0)
        self._redraw()

    def _on_wheel(self, event):
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self._scroll_by(step * self.cell_h)
        return "break"

    def _relayout(self):
        w = max(self.canvas.winfo_width(), self.cell_w)
        self.cols = max(1, (w - self.margin) // self.cell_w)
        rows = (len(self.view) + self.cols - 1) // self.cols if self.view else 0
        self._content_h = self.margin + rows * self.cell_h
        self.canvas.configure(
            scrollregion=(0, 0, self.margin + self.cols * self.cell_w,
                          max(self._content_h, 1)))
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("cell")
        if not self.view:
            return
        top = c.canvasy(0)
        height = c.winfo_height()
        first_row = max(0, int((top - self.margin) // self.cell_h))
        last_row = int((top + height - self.margin) // self.cell_h) + 1
        visible = set()
        for row in range(first_row, last_row + 1):
            for col in range(self.cols):
                pos = row * self.cols + col
                if pos >= len(self.view):
                    break
                idx = self.view[pos]
                visible.add(idx)
                self._draw_cell(row, col, idx)
        self._evict(visible)

    def _load_photo(self, idx, master_path):
        """Render a tk image from a cached master PNG, scaled to the tile size."""
        try:
            from PIL import Image, ImageTk
            with Image.open(master_path) as im:
                im = im.convert("RGB")
                im.thumbnail((self.thumb, self.thumb))
                ph = ImageTk.PhotoImage(im)
        except Exception:
            self.failed.add(idx)
            return None
        self.photo[idx] = ph
        return ph

    def _draw_cell(self, row, col, idx):
        c = self.canvas
        rec = self.records[idx]
        name = os.path.basename(rec["rel"])
        is_video = media_kind(name) == "Video"
        selected = idx in self.checked
        hovered = idx == self.hover

        x0 = self.margin + col * self.cell_w
        y0 = self.margin + row * self.cell_h
        x1, y1 = x0 + self.card_w, y0 + self.card_h
        ix0, iy0 = x0 + self.ip, y0 + self.ip
        icx, icy = ix0 + self.thumb / 2, iy0 + self.thumb / 2

        # card background
        c.create_rectangle(x0, y0, x1, y1, fill=self.CARD_SEL if selected else self.CARD,
                           outline="", tags="cell")

        # image / placeholder
        photo = self.photo.get(idx)
        if (photo is None and idx not in self.failed
                and self.thumbs is not None and rec.get("remote")):
            cp = self.thumbs.cached_path(rec["remote"], rec["size"], rec["mtime"])
            if os.path.exists(cp):
                photo = self._load_photo(idx, cp)
            else:
                self.thumbs.request(rec["remote"], rec["size"], rec["mtime"],
                                    is_video)

        if photo is not None:
            c.create_image(icx, icy, image=photo, tags="cell")
        else:
            box = (ix0, iy0, ix0 + self.thumb, iy0 + self.thumb)
            if is_video:
                c.create_rectangle(*box, fill=self.VIDEO_BG, outline="", tags="cell")
                r = max(12, self.thumb * 0.11)
                c.create_polygon(icx - r * 0.5, icy - r, icx - r * 0.5, icy + r,
                                 icx + r, icy, fill="#e9eaec", outline="", tags="cell")
            else:
                c.create_rectangle(*box, fill="#eef0f3", outline="", tags="cell")
                if idx in self.failed or self.thumbs is None or not rec.get("remote"):
                    label = "no preview"
                else:
                    label = "loading…"
                c.create_text(icx, icy, text=label, fill="#9aa0a8",
                              font=("Segoe UI", 9), tags="cell")

        # border (selected > hover > default)
        if selected:
            bcol, bw = self.ACCENT, 2
        elif hovered:
            bcol, bw = self.BORDER_HOVER, 1
        else:
            bcol, bw = self.BORDER, 1
        c.create_rectangle(x0, y0, x1, y1, outline=bcol, width=bw, tags="cell")

        # selection badge (top-right); hollow ring on hover when unselected
        bx, by, br = x1 - 16, y0 + 16, 11
        if selected:
            c.create_oval(bx - br, by - br, bx + br, by + br, fill=self.ACCENT,
                          outline="white", width=2, tags="cell")
            c.create_text(bx, by, text="✓", fill="white",
                          font=("Segoe UI", 11, "bold"), tags="cell")
        elif hovered:
            c.create_oval(bx - br, by - br, bx + br, by + br, fill="#ffffff",
                          outline=self.ACCENT, width=2, tags="cell")

        # filename
        c.create_text((x0 + x1) / 2, iy0 + self.thumb + self.label_h * 0.45,
                      text=self._short(name), width=self.card_w - 8, anchor="center",
                      fill=self.NAME_FG, font=("Segoe UI", 8), tags="cell")

    def _short(self, name, n=24):
        return name if len(name) <= n else name[:n - 9] + "…" + name[-8:]

    def _evict(self, visible):
        if len(self.photo) <= self.PHOTO_CAP:
            return
        for idx in list(self.photo.keys()):
            if len(self.photo) <= self.PHOTO_CAP:
                break
            if idx not in visible:
                del self.photo[idx]

    def _pos_at(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col = int((x - self.margin) // self.cell_w)
        row = int((y - self.margin) // self.cell_h)
        if col < 0 or col >= self.cols or row < 0:
            return None
        pos = row * self.cols + col
        return pos if 0 <= pos < len(self.view) else None

    # --- interaction ---
    def _on_motion(self, event):
        pos = self._pos_at(event)
        idx = self.view[pos] if pos is not None else None
        if idx != self.hover:
            self.hover = idx
            self._redraw()

    def _on_leave(self, _event):
        if self.hover is not None:
            self.hover = None
            self._redraw()

    def _on_click(self, event):
        pos = self._pos_at(event)
        if pos is None:
            return
        shift = bool(event.state & 0x0001)
        if shift and self.anchor is not None:
            lo, hi = sorted((self.anchor, pos))
            self.checked.update(self.view[lo:hi + 1])
        else:
            idx = self.view[pos]
            if idx in self.checked:
                self.checked.discard(idx)
            else:
                self.checked.add(idx)
            self.anchor = pos
        self._update_count()
        self._redraw()

    def _bulk(self, select):
        if select:
            self.checked.update(self.view)
        else:
            self.checked.difference_update(self.view)
        self._update_count()
        self._redraw()

    def _update_count(self):
        nbytes = sum(self.records[i]["size"] for i in self.checked)
        self.count_lbl.config(
            text=f"{len(self.checked)} selected ({core.human(nbytes)})  ·  "
                 f"{len(self.view)} shown  ·  {len(self.records)} total")

    # --- thumbnail results pump ---
    def _drain_thumbs(self):
        if not self._alive:
            return
        changed = False
        try:
            while True:
                remote, path = self.thumbs.results.get_nowait()
                idx = self.remote_to_idx.get(remote)
                if idx is None:
                    continue
                if not path:
                    self.failed.add(idx)
                changed = True       # _draw_cell loads the master lazily
        except queue.Empty:
            pass
        if changed:
            self._redraw()
        self.after(120, self._drain_thumbs)

    def destroy(self):
        self._alive = False
        if self.thumbs is not None:
            self.thumbs.shutdown()
            self.thumbs = None
        super().destroy()

    def _export(self):
        selected = [self.records[i] for i in sorted(self.checked)]
        if not selected:
            self.count_lbl.config(text="Nothing selected — tick at least one item.")
            return
        self.destroy()          # closes the thumbnail AFC session first
        self.on_export(selected)


# --------------------------------------------------------------------------- #
# Main window.
# --------------------------------------------------------------------------- #

class ExporterApp:
    DOT = {"disconnected": "#e5484d", "locked": "#f5a623",
           "untrusted": "#f5a623", "connecting": "#3b82f6", "trusted": "#22a06b"}
    PILL = {"disconnected": "No device", "locked": "Locked",
            "untrusted": "Trust needed", "connecting": "Connecting…",
            "trusted": "Connected"}

    # palette (matches the chooser window)
    BG = "#f4f5f7"
    CARD = "#ffffff"
    BORDER = "#e2e5ea"
    FG = "#23272f"
    MUTED = "#7a818c"
    ACCENT = "#2f6fdb"
    ACCENT_HOVER = "#2861c4"

    def __init__(self, root):
        self.root = root
        root.title("iPhone Exporter")
        root.geometry("760x760")
        root.minsize(680, 660)
        root.configure(bg=self.BG)

        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.scan_opts = {}
        self.pending_missing = []
        self.backend = "afc"   # flips to "mtp" when the Apple driver is absent

        self._build_ui()
        self._set_status("connecting", "Checking for iPhone…")
        self.detect()
        self.root.after(80, self._poll)

    def _init_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")          # clam lets us recolor flat widgets
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=self.CARD,
                        foreground=self.FG)
        style.configure("TEntry", fieldbackground="white", bordercolor=self.BORDER,
                        lightcolor=self.BORDER, darkcolor=self.BORDER, padding=6)
        style.configure("TCheckbutton", background=self.CARD, foreground=self.FG)
        style.configure("TRadiobutton", background=self.CARD, foreground=self.FG)
        style.map("TCheckbutton", background=[("active", self.CARD)])
        style.map("TRadiobutton", background=[("active", self.CARD)])
        # Buttons
        style.configure("Accent.TButton", background=self.ACCENT, foreground="white",
                        borderwidth=0, focusthickness=0, padding=(18, 9),
                        font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton",
                  background=[("disabled", "#aebfd9"), ("active", self.ACCENT_HOVER)])
        style.configure("Ghost.TButton", background=self.CARD, foreground=self.FG,
                        bordercolor=self.BORDER, borderwidth=1, padding=(12, 7))
        style.map("Ghost.TButton",
                  background=[("active", "#eef1f5"), ("disabled", self.CARD)],
                  foreground=[("disabled", "#b3b9c2")])
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#e7eaef",
                        background=self.ACCENT, borderwidth=0, thickness=8)

    def _card(self, parent):
        return tk.Frame(parent, bg=self.CARD, highlightbackground=self.BORDER,
                        highlightcolor=self.BORDER, highlightthickness=1, bd=0)

    def _section(self, card, title):
        tk.Label(card, text=title.upper(), bg=self.CARD, fg=self.MUTED,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=16, pady=(13, 6))

    def _build_ui(self):
        self._init_styles()

        # Header: app/device identity + status pill + refresh
        header = tk.Frame(self.root, bg=self.BG)
        header.pack(fill="x", padx=24, pady=(20, 6))
        tk.Label(header, text="📲", bg=self.BG,
                 font=("Segoe UI Emoji", 24)).pack(side="left")
        titles = tk.Frame(header, bg=self.BG)
        titles.pack(side="left", padx=12)
        self.device_lbl = tk.Label(titles, text="iPhone Exporter", bg=self.BG,
                                   fg=self.FG, font=("Segoe UI Semibold", 16))
        self.device_lbl.pack(anchor="w")
        self.status_lbl = tk.Label(titles, text="Checking…", bg=self.BG,
                                   fg=self.MUTED, font=("Segoe UI", 9))
        self.status_lbl.pack(anchor="w")
        ttk.Button(header, text="Refresh", style="Ghost.TButton",
                   command=self.detect).pack(side="right")
        self.status_pill = tk.Label(header, text="", bg="#999", fg="white",
                                    font=("Segoe UI Semibold", 9), padx=11, pady=3)
        self.status_pill.pack(side="right", padx=12)

        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(6, 18))

        # Destination card
        dest_card = self._card(body)
        dest_card.pack(fill="x", pady=(6, 12))
        self._section(dest_card, "Save to")
        drow = tk.Frame(dest_card, bg=self.CARD)
        drow.pack(fill="x", padx=16, pady=(0, 14))
        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "iPhoneBackup"))
        ttk.Entry(drow, textvariable=self.dest_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(drow, text="Browse…", style="Ghost.TButton",
                   command=self.browse).pack(side="left", padx=(8, 0))

        # Options card
        opt_card = self._card(body)
        opt_card.pack(fill="x", pady=(0, 12))
        self._section(opt_card, "Options")
        og = tk.Frame(opt_card, bg=self.CARD)
        og.pack(fill="x", padx=16, pady=(0, 12))
        og.columnconfigure(0, weight=1)
        og.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(value="keep")
        ttk.Radiobutton(og, text="Keep originals (HEIC / MOV)", variable=self.mode,
                        value="keep").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Radiobutton(og, text="Convert to JPG / MP4", variable=self.mode,
                        value="convert").grid(row=0, column=1, sticky="w", pady=4)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(og, text="Skip already-transferred files (resume)",
                        variable=self.skip_var).grid(row=1, column=0, sticky="w", pady=4)
        self.lib_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(og, text="Read full photo library (recommended)",
                        variable=self.lib_var).grid(row=1, column=1, sticky="w", pady=4)

        # Primary actions
        action = tk.Frame(body, bg=self.BG)
        action.pack(fill="x", pady=(2, 6))
        self.start_btn = ttk.Button(action, text="Scan & Choose…",
                                    style="Accent.TButton", command=self.scan_and_choose)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(action, text="Cancel", style="Ghost.TButton",
                                     command=self.do_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        # Backup & iCloud tools
        tools_card = self._card(body)
        tools_card.pack(fill="x", pady=(6, 6))
        self._section(tools_card, "Backup & iCloud")
        trow = tk.Frame(tools_card, bg=self.CARD)
        trow.pack(fill="x", padx=16, pady=(0, 14))
        self.icloud_btn = ttk.Button(trow, text="Download iCloud Photos…",
                                     style="Ghost.TButton",
                                     command=self.icloud_download)
        self.icloud_btn.pack(side="left")
        self.backup_btn = ttk.Button(trow, text="Full Backup…",
                                     style="Ghost.TButton",
                                     command=self.full_backup)
        self.backup_btn.pack(side="left", padx=8)
        self.extract_btn = ttk.Button(trow, text="Extract from Backup…",
                                      style="Ghost.TButton",
                                      command=self.extract_backup)
        self.extract_btn.pack(side="left")

        # Progress
        self.prog = ttk.Progressbar(body, mode="determinate",
                                    style="Accent.Horizontal.TProgressbar")
        self.prog.pack(fill="x", pady=(8, 2))
        self.prog_lbl = tk.Label(body, text="Idle.", bg=self.BG, fg=self.MUTED,
                                 font=("Segoe UI", 9), anchor="w")
        self.prog_lbl.pack(fill="x")

        # Activity log
        log_card = self._card(body)
        log_card.pack(fill="both", expand=True, pady=(12, 0))
        self._section(log_card, "Activity")
        self.log = scrolledtext.ScrolledText(log_card, height=8, state="disabled",
                                             font=("Consolas", 9), wrap="none",
                                             relief="flat", bd=0, bg="#fbfbfc",
                                             fg=self.FG, highlightthickness=0)
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # --- widget helpers (main thread) ---
    def _set_status(self, kind, text, device=None):
        self.status_pill.config(bg=self.DOT.get(kind, "#999"),
                                text=self.PILL.get(kind, "Status"))
        self.status_lbl.config(text=text)
        if device is not None:
            self.device_lbl.config(text=device)

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

    # --- Backup & iCloud tools ---------------------------------------------
    def _tools_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.start_btn, self.icloud_btn, self.backup_btn,
                  self.extract_btn):
            b.config(state=state)
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def _ask_on_main(self, title, prompt, secret=False):
        """Block a worker thread on a dialog that runs in the main thread."""
        from tkinter import simpledialog
        holder, ready = {}, threading.Event()

        def show():
            kw = {"parent": self.root}
            if secret:
                kw["show"] = "*"
            holder["v"] = simpledialog.askstring(title, prompt, **kw)
            ready.set()

        self.root.after(0, show)
        ready.wait()
        return holder.get("v") or ""

    def icloud_download(self):
        """Pull the entire iCloud Photos library (originals) to a folder."""
        import icloud_sync
        if not icloud_sync.icloudpd_available():
            self._append_log("icloudpd isn't installed — run:  "
                             "python -m pip install icloudpd")
            return
        from tkinter import simpledialog
        user = simpledialog.askstring(
            "iCloud", "Apple ID (email):", parent=self.root)
        if not user:
            return
        dest = filedialog.askdirectory(title="Folder for the iCloud library")
        if not dest:
            return
        import icloud_sync
        self.cancel.clear()
        self._tools_busy(True)
        self.prog.config(value=0)
        self.prog_lbl.config(text="Signing in to iCloud…")
        self._append_log("──────── iCloud download ────────")

        def work():
            try:
                icloud_sync.run_download(
                    user, dest,
                    ask_password=lambda: self._ask_on_main(
                        "iCloud", f"Password for {user}:", secret=True),
                    ask_2fa=lambda: self._ask_on_main(
                        "iCloud", "2FA code (tap Allow on your iPhone):"),
                    log=lambda m: self._emit("log", m),
                    progress=lambda d, t: self._emit("progress", d, t),
                    cancel=self.cancel)
            except Exception as e:
                self._emit("error", f"{type(e).__name__}: {e}")
            self._emit("finished", None)

        threading.Thread(target=work, daemon=True).start()

    def full_backup(self):
        """iTunes-style full backup — WhatsApp, app data, everything."""
        dest = filedialog.askdirectory(
            title="Folder for the full device backup")
        if not dest:
            return
        from tkinter import messagebox
        if not messagebox.askokcancel(
                "Full backup",
                "This copies everything an iTunes backup holds (WhatsApp "
                "chats & media, app data, settings).\n\nThe iPhone will ask "
                "for its passcode — enter it on the phone.\n\nStart now?",
                parent=self.root):
            return
        import device_backup
        self.cancel.clear()
        self._tools_busy(True)
        self.prog.config(value=0, maximum=100)
        self.prog_lbl.config(text="Backing up…")
        self._append_log("──────── full device backup ────────")

        def work():
            try:
                run_async(lambda: device_backup.run_backup(
                    dest,
                    progress=lambda pct: self._emit("pct", pct),
                    log=lambda m: self._emit("log", m)))
            except Exception as e:
                self._emit("error", f"{type(e).__name__}: {e}")
            self._emit("finished", None)

        threading.Thread(target=work, daemon=True).start()

    def extract_backup(self):
        """Turn a full backup into browsable folders (WhatsApp, Files, …)."""
        import backup_extract
        backup = filedialog.askdirectory(
            title="Pick the backup folder (the one holding Manifest.db)")
        if not backup:
            return
        try:
            info = backup_extract.backup_info(backup)
        except Exception as e:
            self._append_log(f"Not a backup folder: {e}")
            return
        if info["encrypted"]:
            self._append_log("That backup is encrypted — extraction needs an "
                             "unencrypted backup.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Extract from backup")
        dlg.configure(bg=self.CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text=f"Backup of {info['device']} (iOS {info['ios']})",
                 bg=self.CARD, fg=self.FG,
                 font=("Segoe UI Semibold", 11)).pack(
            anchor="w", padx=16, pady=(14, 8))
        vars_ = {}
        for key, preset in backup_extract.PRESETS.items():
            v = tk.BooleanVar(value=(key == "whatsapp"))
            vars_[key] = v
            ttk.Checkbutton(dlg, text=preset.label, variable=v).pack(
                anchor="w", padx=18, pady=2)

        def start():
            chosen = [k for k, v in vars_.items() if v.get()]
            if not chosen:
                return
            dest = filedialog.askdirectory(title="Extract into…", parent=dlg)
            if not dest:
                return
            dlg.destroy()
            self.cancel.clear()
            self._tools_busy(True)
            self.prog.config(value=0)
            self.prog_lbl.config(text="Extracting…")
            self._append_log("──────── extracting from backup ────────")

            def work():
                try:
                    backup_extract.extract(
                        backup, dest, presets=chosen,
                        log=lambda m: self._emit("log", m),
                        progress=lambda d, t: self._emit("progress", d, t),
                        cancel=self.cancel)
                except Exception as e:
                    self._emit("error", f"{type(e).__name__}: {e}")
                self._emit("finished", None)

            threading.Thread(target=work, daemon=True).start()

        ttk.Button(dlg, text="Choose destination & start",
                   style="Accent.TButton", command=start).pack(
            anchor="e", padx=16, pady=14)

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
            msg = {"disconnected": ("disconnected", "Plug in your iPhone via USB.", "No device"),
                   "locked": ("locked", "Unlock your iPhone to continue.", "iPhone"),
                   "untrusted": ("untrusted", "Tap “Trust” on the iPhone, then Refresh.", "iPhone")}
            if state == "trusted":
                self.backend = "afc"
                self._set_status("trusted", "Trusted over USB · full library available",
                                 device=name)
            elif state == "mtp_ready":
                self.backend = "mtp"
                self._set_status("trusted", "Connected over MTP · no Apple driver",
                                 device=name)
            else:
                if state in ("locked", "untrusted"):
                    self.backend = "afc"
                kind_, text_, dev_ = msg.get(
                    state, ("disconnected", "No iPhone.", "No device"))
                self._set_status(kind_, text_, device=dev_)
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
        elif kind == "pct":
            self.prog.config(maximum=100, value=payload)
            self.prog_lbl.config(text=f"Backing up… {payload:.0f}%")
        elif kind == "error":
            self._append_log("ERROR: " + payload)
        elif kind == "records":
            records, missing = payload
            self.pending_missing = missing
            if not records:
                self._append_log("No items found to choose from.")
                return
            # Thumbnails only over AFC (live partial reads). MTP shows a
            # name-only grid — pulling each file just to preview is too slow.
            thumbs = (ThumbnailService()
                      if self.scan_opts.get("backend") == "afc" else None)
            SelectionWindow(self.root, records, on_export=self.begin_export,
                            thumbs=thumbs)
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
            self._tools_busy(False)


def main():
    import sys
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        root = tk.Tk(); root.withdraw()
        from tkinter import messagebox
        messagebox.showerror(
            "Missing dependency",
            "pymobiledevice3 is not installed for this Python.\n\n"
            "Install with:\n  python -m pip install pymobiledevice3 pillow pillow-heif")
        sys.exit(1)

    root = tk.Tk()
    ExporterApp(root)            # sets up the (clam-based) modern theme itself
    root.mainloop()


if __name__ == "__main__":
    main()
