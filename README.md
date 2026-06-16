# iPhone Photo & Video Bulk Exporter (Windows, USB)

Copy **all photos and videos** off an iPhone over USB into a folder on your PC.
No iCloud, no WSL. There's a desktop **GUI** (`iphone_export_gui.py`) and a
**command-line** version (`iphone_export.py`) — both share the same transfer
engine.

It uses [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) and the
**AFC** protocol for true byte-streaming with exact file sizes. It recursively
finds every photo/video, preserves filenames + folder structure
(`100APPLE`, `101APPLE`, …) and modified dates, shows a progress bar, **skips
already-copied files (resumable)**, and logs failures to `failures.log`.

---

## Setup

1. **Install Python 3.9+** from https://www.python.org/downloads/
   (tick *"Add Python to PATH"*).
2. **Install the Apple Mobile Device USB driver.** You don't have to *use* the
   app — it just installs the driver/service pymobiledevice3 needs. Any of:
   - **iTunes from the Microsoft Store** (recommended by the pymobiledevice3 docs), **or**
   - the **Apple Devices** app from the Microsoft Store, **or**
   - iTunes from https://www.apple.com/itunes/download/.

   Reboot once after installing.
3. Install the Python packages:
   ```powershell
   pip install -r requirements.txt
   ```
4. Plug in the iPhone with a **data-capable** cable, **unlock it**, and tap
   **Trust** / **Allow** on the phone (enter passcode). Once per PC.
5. Verify the device is visible:
   ```powershell
   pymobiledevice3 usbmux list
   ```
   If this prints `[]`, the phone isn't trusted yet (unlock + re-plug + Trust) or
   the Apple driver isn't installed.

---

## Run it — GUI

```powershell
python iphone_export_gui.py
```

Pick a destination folder and click export. Same engine as the CLI, with a
progress bar and live log.

## Run it — CLI

```powershell
python iphone_export.py --dest "D:\iPhoneBackup"
# scan the whole media partition instead of just DCIM:
python iphone_export.py -d "D:\iPhoneBackup" -s /
```

Example output:
```
Connected to: Jalal's iPhone (iPhone14,2)
Found 8421 files (62.4 GB).
Exporting: 100%|██████████| 8421/8421 [21:38<00:00] copied=8421 skipped=0 failed=0
Done. Copied 8421, skipped 0 (already present), failed 0.
Files are in: D:\iPhoneBackup
```

**Resuming:** just run the same command again — files already copied (same size)
are skipped, so an interrupted run picks up where it left off. `Ctrl+C` to stop.

Options: `--dest/-d` (required), `--source/-s` (default `/DCIM`, use `/` for the
whole partition), `--all` (copy non-media too), `--log`.

### Converting HEIC photos to JPG (`--jpg`)
By default photos are exported in their **original** format (HEIC stays HEIC).
Add `--jpg` to also convert HEIC/HEIF photos to widely-compatible JPG:

```powershell
python -m pip install pillow pillow-heif      # one-time, for conversion
python iphone_export.py -d "C:\Users\user1\iPhoneBackup" --library --jpg
```

- `--jpg` replaces each `.HEIC` with a `.jpg` (EXIF, incl. capture date, is kept).
- `--jpg --keep-heic` keeps **both** the original `.HEIC` and the new `.jpg`.
- Videos and already-JPG photos are untouched. Conversion runs off-thread so it
  doesn't stall the USB connection, and it's resume-safe.

> `.HEIC` photos are normal iPhone photos; install the free **HEIF Image
> Extensions** from the Microsoft Store to view them in Windows Photos without
> converting.

## `--library` mode — the "full library, like iMazing" option

```powershell
python iphone_export.py --dest "D:\iPhoneBackup" --library
```

Instead of just walking the DCIM folder, this pulls the iPhone's **Photos
database** (`/PhotoData/Photos.sqlite`) off the device and reads it to build the
*complete* asset manifest — the same trick iMazing uses. You get:

- **Every asset**, including ones stored outside DCIM, with **accurate capture
  dates** straight from the library (not just file timestamps).
- A union with a raw DCIM sweep, so Live Photo `.MOV` halves and `.AAE` edit
  sidecars are caught too.
- **`not_on_device.txt`** — a precise list of every photo/video that exists in
  your library but is **only in iCloud** (offloaded by "Optimize iPhone
  Storage"), with filename + date. These genuinely aren't on the phone, so *no*
  USB tool — iMazing included — can pull them until you download them:
  enable **Settings → Photos → Download and Keep Originals**, let it sync over
  Wi-Fi, then re-run and they'll be exported.

This mode needs the phone **unlocked and trusted** (the Photos database is only
readable then).

---

## Troubleshooting

**iPhone not detected**
- Phone unlocked? Cable **data-capable** (not charge-only)? Tapped **Trust**?
- Run `pymobiledevice3 usbmux list`. If it's empty, either the Apple driver isn't
  installed (install iTunes/Apple Devices and reboot), or the phone hasn't been
  trusted yet — unlock it, unplug/replug, tap **Trust**, and try again.

**Pairing keeps failing**
- Unlock, unplug/replug, watch for the Trust prompt, re-run. To fully reset:
  Settings → General → Transfer or Reset iPhone → Reset → **Reset Location &
  Privacy**, reconnect, and Trust again.

**Some files in `failures.log`**
- iOS sometimes briefly locks a file (e.g. a Live Photo still processing). Just
  re-run — transient failures usually succeed next pass.

**Low-res photos / placeholders**
- With iCloud "Optimize iPhone Storage" on, some on-device photos are
  placeholders. Set Settings → Photos → **Download and Keep Originals**, let it
  finish over Wi-Fi, then export to get full-res files.
