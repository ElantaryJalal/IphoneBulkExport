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

## Step 1 — Build the .exe (on Windows)

In the project folder (where `iphone_export_gui.py` lives):

```powershell
.\build_exe.bat
```

Result: **`dist\iPhoneExporter.exe`** (one double-clickable file). First launch is
a bit slow (it unpacks to a temp dir); that's normal for one-file builds.

> ffmpeg is **not** bundled. For MOV→MP4 in the packaged app, drop `ffmpeg.exe`
> next to `iPhoneExporter.exe`, or tell users to install ffmpeg. HEIC→JPG works
> out of the box (pillow-heif is bundled).

Test the .exe on a clean-ish machine if you can, since SmartScreen will flag an
unsigned binary (users click **More info → Run anyway** — this is covered in the
website FAQ).

## Step 2 — Put the app on GitHub Releases

1. Create a GitHub repo and push the project (the `.py` files + `website/`).
2. On GitHub: **Releases → Draft a new release**.
3. Tag it `v1.0`, give it a title, and **attach `iPhoneExporter.exe`** as a
   release asset. Publish.
4. Your permanent “always latest” download URL is:
   ```
   https://github.com/YOUR_USER/YOUR_REPO/releases/latest/download/iPhoneExporter.exe
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
