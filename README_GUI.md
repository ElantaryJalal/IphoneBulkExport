# iPhone Exporter — Desktop GUI

A tkinter desktop app over the same proven engine as the CLI (`iphone_export.py`).
It connects to an iPhone over USB and bulk-exports all photos & videos to a
folder, with a live progress bar, a real-time log, and a connection/trust status
light.

![overview](docs-not-included) <!-- no image; described below -->

**Main window**
- A **status light** (red / orange / green) showing: not connected · connected
  but locked or not trusted · connected & trusted (with the device name).
- A **destination folder** picker.
- A toggle: **Keep Originals (HEIC/MOV)** vs **Convert to JPG/MP4**.
- **Skip already-transferred files (resume)** checkbox.
- **Read full photo library** checkbox (the iMazing-style mode: reads
  `Photos.sqlite` for every asset + accurate dates, and reports iCloud-only
  photos to `not_on_device.txt`).
- **Scan & Choose…** / **Cancel** buttons, a **progress bar** (files / total),
  and a scrolling **log**.
- On finish: a **summary** (copied / skipped / failed), and failures saved to
  `failures.txt` in the destination.

**Selection window** (opens after a scan)
- A checkable table of **every item** — tick/untick each row's box (Name, Date,
  Type, Size).
- **Filters**: From / To date (`YYYY-MM-DD`), Type (All / Photos / Videos), and
  a Name search. Click **Apply filter** to narrow the list.
- **Select all (shown)** / **Deselect all (shown)** act on the *currently
  filtered* rows — so you can e.g. filter to Videos, Select All, then untick a
  few. Selections persist as you change filters.
- **Space** toggles the highlighted row(s); a single click on the checkbox
  column toggles that row.
- **Export selected** exports just the ticked items (with resume + conversion).

The device I/O runs on a **background thread** with its own asyncio loop; the UI
stays fully responsive and is updated through a thread-safe queue.

---

## 1. Setup (Windows)

1. **Apple USB driver** — install **iTunes** or **Apple Devices** from the
   Microsoft Store (provides the driver pymobiledevice3 needs), then reboot.
   Verify: `python -m pymobiledevice3 usbmux list` should list your iPhone.
2. **Python 3.9+** (python.org, or your existing install).
3. **Python packages** — install into the *same* interpreter you'll run with
   (use `python -m pip` to be sure):
   ```powershell
   python -m pip install pymobiledevice3 pillow pillow-heif
   ```
   - `pillow` + `pillow-heif` are only needed for **HEIC→JPG** conversion.
   - tkinter ships with Python — nothing to install.
4. **ffmpeg** (optional) — only for **MOV→MP4** conversion. You likely already
   have it (`ffmpeg -version`). If not: `winget install Gyan.FFmpeg`, then reopen
   the terminal. Without ffmpeg, videos are simply kept as `.MOV`.

> Keep `iphone_export_gui.py` **next to `iphone_export.py`** — the GUI imports the
> engine from it.

## 2. Run it

```powershell
cd C:\Users\user1\iphone-export
python iphone_export_gui.py
```

Then:
1. Plug in the iPhone, **unlock it**, tap **Trust** if prompted, click **Refresh**
   until the light is green.
2. Pick a destination folder.
3. Choose Keep Originals or Convert, tick the options you want, click
   **Scan & Choose…**.
4. In the selection window, filter/tick the items you want and click
   **Export selected**.
5. Keep the phone **unlocked** during scan and transfer (set Auto-Lock to Never
   for big libraries). If the connection drops, just run it again — it resumes.

## 3. Package as a standalone .exe (PyInstaller)

```powershell
python -m pip install pyinstaller

pyinstaller --noconfirm --onefile --windowed --name iPhoneExporter ^
  --collect-all pymobiledevice3 ^
  --collect-submodules pymobiledevice3 ^
  --collect-all pillow_heif ^
  --collect-all construct ^
  iphone_export_gui.py
```

(`^` is the PowerShell/CMD line-continuation; or put it all on one line.)

- The result is `dist\iPhoneExporter.exe` — double-clickable, no Python needed.
- `--collect-all pymobiledevice3` bundles its data files (plist/protocol
  resources); `--collect-all pillow_heif` bundles the native HEIF decoder.
  PyInstaller automatically pulls in `iphone_export.py` because the GUI imports
  it.
- **ffmpeg is NOT bundled.** For MOV→MP4 in the packaged app, either have ffmpeg
  on the user's PATH, or drop `ffmpeg.exe` in the same folder as the .exe.
- First launch of a one-file build is a little slow (it unpacks to a temp dir).
  For faster startup use `--onedir` instead of `--onefile` (ships a folder).

**If the .exe errors with a missing module** (some pymobiledevice3 deps import
lazily), add it explicitly, e.g.:
```
--collect-all bpylist2 --collect-all asn1 --hidden-import coloredlogs
```

---

## Notes
- **iCloud-only photos** (with "Optimize iPhone Storage" on) aren't on the
  device, so no USB tool can fetch them. They're listed in `not_on_device.txt`;
  enable **Settings → Photos → Download and Keep Originals**, let it sync, then
  run again.
- **HEIC previews**: install the free *HEIF Image Extensions* from the Store to
  view `.heic` in Windows Photos (or use the Convert option to get JPGs).
- The CLI (`iphone_export.py`) has the same engine and now matching flags:
  `--jpg`, `--keep-heic`, `--mp4`, `--keep-mov`, `--library`.
