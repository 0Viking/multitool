# Multitool

A couple of handy tools that run on your own PC and open in your browser.

- **Video downloader**: paste a link from YouTube, X, Instagram, TikTok or [hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), or grab videos playing in your other tabs. Always the best quality, with a progress bar, pause/resume, retry and multi-connection speed.
- **Universal converter**: drop video, audio or image files and turn them into other formats, one at a time or in batches. It also converts tables between CSV, TSV and JSON, and pulls the text out of PDFs.
- **Image editor**: a Photoshop-style workspace (tools left, resizable panels and layers right, zoom and pan). New projects by size and resolution (px, in, cm, mm at any ppi) or open / paste images; selections (rectangle, ellipse, lasso, magic wand) with copy, cut, delete and paste in place; brush, eraser, paint bucket, gradient, eyedropper; editable text and shape layers; layers you can move, resize and rotate by dragging, reorder, rename, hide, lock, blend, merge and flatten; crop, canvas size, resize, adjustments (exposure, brightness, contrast, saturation, hue, curves, levels, black & white, invert) and filters (blur, sharpen, pixelate); a History panel; projects saved as `.mtimg` to keep editing later; exports PNG, JPG or WebP.
- **Video editor**: crop, resize, trim on a filmstrip, change the canvas shape, move / resize / rotate the video on the canvas by dragging it, mirror and mute, with undo (Ctrl+Z).
- **Compressor & resizer**: shrink videos, images and GIFs to a size goal (e.g. under 10 MB) or a % of the original, resize and crop them, with undo.

Everything runs locally. Nothing is uploaded anywhere, and your logins never leave your computer.

## Setup (Windows, once)

**1. Install the tools it uses.** Open **PowerShell** and run these one by one (`winget` comes with Windows 10/11):

```powershell
winget install Python.Python.3.14
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS
winget install aria2.aria2
winget install Git.Git
```

ffmpeg does the converting, Node.js is needed for YouTube, aria2 makes downloads faster (optional), and Git lets you grab updates easily.

**2. Close PowerShell and open a new one** so it finds the new programs, then install the downloader engine and the PDF reader:

```powershell
py -m pip install -U "yt-dlp[default]" pypdf
```

**3. Get Multitool:**

```powershell
git clone https://github.com/0Viking/multitool.git
cd multitool
```

(No Git? Use **Code → Download ZIP** on this page and unzip it instead.)

## Run it

In the `multitool` folder:

```powershell
py server.py
```

Then open **http://127.0.0.1:8765** in your browser. Keep that PowerShell window open while you use it; close it to stop.

## The browser extension (for "Caught in your tabs")

The downloader can list the videos playing in your other tabs, ready to grab. That part needs a small extension for Chrome, Edge, Opera (GX), Brave or another Chromium browser; Firefox can't load it. Until it's installed in the browser you're using, the downloader page shows a setup box with these steps and Copy buttons for the address and folder:

1. Open a new tab and go to your browser's extensions page: `chrome://extensions` (Edge: `edge://extensions`, Opera: `opera://extensions`, Brave: `brave://extensions`).
2. Turn on **Developer mode** (top right; on the left in Edge).
3. Click **Load unpacked** and choose the `extension` folder inside `multitool` (the box's **Copy** button gives you the path to paste).

The extension also passes your logins for video sites (YouTube, Instagram, X…) to Multitool, so videos that need you signed in work too. Those stay in a `cookies.txt` file on your PC and are never uploaded.

## Updating

```powershell
git pull
py -m pip install -U "yt-dlp[default]"
```

Then restart `py server.py`. If the extension changed, click the reload icon on its card in `chrome://extensions`.

YouTube changes things every few weeks. If downloads suddenly fail, updating yt-dlp (the second line) usually fixes it.

## Good to know

- **Where files go:** each tool has a **Save to … Change** button. The folder window can open behind the browser, so check the taskbar.
- **"aria2c not found" when starting:** aria2 isn't installed, or PowerShell was opened before installing it. Downloads still work, just over one connection.
- **"Sign in to confirm you're not a bot" on YouTube:** be signed in to YouTube in Chrome with the extension loaded, and update yt-dlp.
- **Never share** `cookies.txt` (your logins) or `settings.json` from your copy. Git already leaves them out.
- Made for Windows. Linux mostly works; on macOS the **Open folder** link doesn't.
- Only download things you have the right to.

## What's inside

| File | What it does |
|---|---|
| `server.py` | The local server: pages, downloads (yt-dlp), video previews |
| `convert.py` | The converter, editor and compressor exports (ffmpeg for media; Python for tables and PDFs) |
| `index.html`, `downloader.html`, `converter.html`, `studio.html`, `image.html`, `style.css` | The pages (`studio.html` is both the video editor and the compressor) |
| `extension/` | The browser extension |
