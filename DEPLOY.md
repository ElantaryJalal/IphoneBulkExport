# Deploying iPhone Exporter (app + website)

You ship **two things**:

1. **`iPhoneExporter.exe`** — the app, hosted on **GitHub Releases** (binaries are
   too big and not a good fit for Vercel).
2. **`website/`** — the static landing page, hosted on **Vercel**, which links to
   the .exe.

```
        ┌─────────────┐   links to .exe   ┌──────────────────────┐
        │  Vercel     │ ───────────────►  │  GitHub Releases     │
        │  (website)  │                   │  iPhoneExporter.exe  │
        └─────────────┘                   └──────────────────────┘
```

---

## Step 1 — Build the installer (on Windows)

You ship a single **installer** (`iPhoneExporterSetup.exe`) that installs the app
**and** silently sets up the Apple Mobile Device USB driver (via winget) so the
fast AFC path works on first launch. If that driver step can't run, the app falls
back to MTP (Windows' built-in iPhone connection) — so it still works with no
Apple software at all.

**1a. Build the one-file exe** — must be built from a **python.org** Python, not
Anaconda (conda's DLL layout makes the packaged exe crash with `_ctypes` errors):

```powershell
.\build_venv.bat
```

Result: **`dist\iPhoneExporter.exe`**. ffmpeg **is** bundled inside it (MOV→MP4
works out of the box); HEIC→JPG works too (pillow-heif bundled).

**1b. Compile the installer** with Inno Setup (`winget install JRSoftware.InnoSetup`
if you don't have it), from the project root:

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

Result: **`dist\iPhoneExporterSetup.exe`** — this is what you upload and what the
website links to.

Test the installer on a clean-ish machine if you can. SmartScreen flags the
unsigned installer (users click **More info → Run anyway** — covered in the
website FAQ). The driver step needs internet + admin (UAC) on the user's machine.

## Step 2 — Put the installer on GitHub Releases

1. Create a GitHub repo and push the project (the `.py` files + `website/` +
   `installer.iss` + `installer/`).
2. On GitHub: **Releases → Draft a new release**.
3. Tag it `v1.0`, give it a title, and **attach `iPhoneExporterSetup.exe`** as a
   release asset. Publish.
4. Your permanent “always latest” download URL is:
   ```
   https://github.com/YOUR_USER/YOUR_REPO/releases/latest/download/iPhoneExporterSetup.exe
   ```

## Step 3 — Point the website at it

Edit the CONFIG block at the bottom of **`website/index.html`**:

```js
const DOWNLOAD_URL = "https://github.com/YOUR_USER/YOUR_REPO/releases/latest/download/iPhoneExporter.exe";
const GITHUB_URL   = "https://github.com/YOUR_USER/YOUR_REPO";
const VERSION      = "v1.0";
const SIZE_TEXT    = "~90 MB";   // check the real size of your .exe
```

## Step 4 — Deploy the website to Vercel

**Option A — Vercel dashboard (easiest)**
1. Push the repo to GitHub (if you haven't).
2. vercel.com → **Add New → Project → Import** your repo.
3. **Root Directory: `website`**. Framework Preset: **Other** (it's plain static).
   No build command, no install. Click **Deploy**.

**Option B — Vercel CLI**
```powershell
npm i -g vercel
cd website
vercel            # preview deploy
vercel --prod     # production
```

You'll get a `*.vercel.app` URL immediately. Add a custom domain later under the
project's **Domains** tab if you want.

---

## Updating later

- New app version: rebuild the exe, upload it to a **new GitHub release**
  (`/releases/latest/download/…` automatically points to the newest). Bump
  `VERSION` / `SIZE_TEXT` in `index.html` and redeploy the site.
- The website is static, so each push to the connected GitHub repo auto-deploys
  on Vercel.

## Notes
- **Don't commit `dist/`, `build/`, or `__pycache__/`** to the repo — they're
  build artifacts. A `.gitignore` is included.
- Add an `og.png` (1200×630) in `website/` for a nice social-share preview; the
  page already references `/og.png`.
