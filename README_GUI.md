# iPhone Exporter — Desktop GUI

A tkinter desktop app over the same proven engine as the CLI (`iphone_export.py`).
It connects to an iPhone over USB and bulk-exports all photos & videos to a
folder, with a live progress bar, a real-time log, and a connection/trust status
light.

It works **with or without Apple software**: it uses the fast **AFC** path when
the Apple Mobile Device driver is present, and automatically falls back to
**MTP** (Windows' built-in iPhone connection) when it isn't — see
[README.md](README.md) for the engine details.

**Main window**
- A **status light** (red / orange / green) showing: not connected · connected
  but locked or not trusted · connected & trusted (with the device name). When
  the Apple driver is absent it shows **MTP mode** with the device name.
- A **destination folder** picker.
- A toggle: **Keep Originals (HEIC/MOV)** vs **Convert to JPG/MP4**.
- **Skip already-transferred files (resume)** checkbox.
- **Read full photo library** checkbox (the iMazing-style mode: reads
  `Photos.sqlite` for every asset + accurate dates, and reports iCloud-only
  photos to `not_on_device.txt`). This is **AFC-only**; in MTP mode it's ignored
  and a DCIM scan is used instead.
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

The device I/O runs on a **background thread** (its own asyncio loop for AFC, or
a per-thread COM apartment for MTP); the UI stays fully responsive and is updated
through a thread-safe queue.

---

## Run from source (Windows)

1. **Python 3.9+** (python.org).
2. Install the packages into the *same* interpreter you'll run with:
   ```powershell
   python -m pip install -r requirements.txt
   ```
   `pillow` + `pillow-heif` are only needed for HEIC→JPG; `pywin32` powers the
   MTP fallback (Windows). tkinter ships with Python.
3. (Optional) For the fast AFC path + full-library mode, install the Apple
   Mobile Device driver — `winget install Apple.AppleMobileDeviceSupport` (just
   the driver, not iTunes). Without it, the GUI uses MTP automatically.
4. Run it:
   ```powershell
   python iphone_export_gui.py
   ```

Then: plug in the iPhone, **unlock it**, tap **Trust** if prompted, click
**Refresh** until the light is green, pick a folder, choose your options, and
**Scan & Choose…**. Keep the phone unlocked during scan and transfer (set
Auto-Lock to Never for big libraries); if the connection drops, just run again —
it resumes.

> Keep `iphone_export_gui.py` next to `iphone_export.py` and
> `iphone_export_mtp.py` — the GUI imports both engines from them.

## Packaging & distribution

Don't package by hand — use the maintained build:

- **`build_venv.bat`** builds `dist\iPhoneExporter.exe` from a clean python.org
  venv (ffmpeg and pywin32 are bundled inside it).
- **`installer.iss`** (Inno Setup) wraps that into `dist\iPhoneExporterSetup.exe`,
  which also installs the Apple USB driver for the user automatically.

Full release steps are in [DEPLOY.md](DEPLOY.md).

> Build only from a **python.org** Python, never Anaconda — conda's DLL layout
> makes the packaged exe crash with `_ctypes` errors.

---

## Notes
- **iCloud-only photos** (with "Optimize iPhone Storage" on) aren't on the
  device, so no USB tool can fetch them. In AFC full-library mode they're listed
  in `not_on_device.txt`; enable **Settings → Photos → Download and Keep
  Originals**, let it sync, then run again.
- **HEIC previews**: install the free *HEIF Image Extensions* from the Store to
  view `.heic` in Windows Photos (or use the Convert option to get JPGs).
