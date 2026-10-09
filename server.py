"""Multitool — local tools in the browser: a video downloader and a universal converter (convert.py).

Run:   python server.py   → open http://127.0.0.1:8765
Sniffer: chrome://extensions → Developer mode → Load unpacked → pick the `extension` folder.
Files go to the folders picked on each page (default ~/Downloads). Needs `pip install -U "yt-dlp[default]"`,
plus ffmpeg and node on PATH.
Optional: aria2c on PATH (`winget install aria2.aria2`) downloads over 16 connections, beating per-connection caps.
"""
import glob, hashlib, json, os, re, shutil, subprocess, sys, threading, time, tkinter
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from tkinter import filedialog
from urllib.parse import parse_qs, unquote, urlsplit

from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.utils import MEDIA_EXTENSIONS, determine_ext, sanitize_filename
from yt_dlp.utils.networking import std_headers

import convert

PORT = 8765
PAGES = {"/": "index.html", "/downloader": "downloader.html", "/converter": "converter.html", "/style.css": "style.css",
         "/editor": "studio.html", "/compressor": "studio.html"}  # one page for both: it adapts to its address
COOKIES = Path(__file__).parent / "cookies.txt"  # synced from Chrome by the extension; holds logins, keep private
SETTINGS = Path(__file__).parent / "settings.json"
EXTENSION_DIR = Path(__file__).parent / "extension"
HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
ORIGINS = tuple(f"http://{h}" for h in HOSTS) + ("chrome-extension://",)
EXTRACTORS = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
MEDIA_EXTS = {*MEDIA_EXTENSIONS.video, *MEDIA_EXTENSIONS.audio, *MEDIA_EXTENSIONS.manifests}
PROGRESS = ("download:PROG %(info.vcodec)s %(info.acodec)s %(progress.status)s %(progress.downloaded_bytes)s "
            "%(progress.total_bytes)s %(progress.total_bytes_estimate)s %(progress.speed)s")
YOUTUBE_VIDEO = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)([\w-]{11})")
ARIA2C = shutil.which("aria2c")
ARIA2_READOUT = re.compile(r"\[#\w+ ([\d.]+)(\w+)/([\d.]+)(\w+)(?:\(\d+%\))? CN:\d+ DL:([\d.]+)(\w+)")  # yt-dlp doesn't relay it
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}

settings = {"out": str(Path.home() / "Downloads"), "convert_out": str(Path.home() / "Downloads")}
try:
    settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
except (OSError, ValueError):
    pass

sniffed, jobs, lock = deque(maxlen=10), {}, threading.Lock()  # jobs: download key -> latest progress
downloads = {}  # download key -> its row in the Downloads list, so new catches can't push it out
procs, stops, stems = {}, {}, {}  # download key -> running yt-dlp / "pause" or "cancel" asked / output path minus ext
thumbs, previewing = {}, set()  # thumb id -> jpeg; list keys whose preview is being made
picking = threading.Lock()  # one folder dialog at a time


def extractor(url):
    """yt-dlp's dedicated extractor for this page, if any: it knows the real title, formats and audio."""
    return next((ie for ie in EXTRACTORS if ie.suitable(url)), None)


def clean_title(title, page):
    """'(3) Some video - YouTube' → 'Some video'."""
    parts = (urlsplit(page).hostname or "").split(".")
    site = re.escape(parts[-2] if len(parts) > 1 else parts[0])
    title = re.sub(r"^\(\d+\+?\)\s*", "", title.strip())
    return re.sub(rf"\s*[-|/•·–—]\s*{site}$", "", title, flags=re.I).strip()


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def ytdlp(url, key, name="%(title).150B", ext="%(ext)s", extra=(), audio=False, split=True):
    if key not in downloads:  # canceled meanwhile, e.g. between the page try and the stream fallback
        return {"ok": False, "stopped": "cancel"}
    out = Path(stems[key]).parent if key in stems else Path(settings["out"])  # a resume stays with its partial file
    # video: best video + best audio merged; audio: the best audio track, made into a top-quality MP3
    fmt = (["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"] if audio
           else ["-f", "bv*+ba/b", "--merge-output-format", "mp4"])
    cmd = [sys.executable, "-u", "-m", "yt_dlp", "--no-playlist", "--max-downloads", "1", "--encoding", "utf-8",
           "--js-runtimes", "node", *fmt, "--concurrent-fragments", "8",
           "-o", str(out / f"{name}.{ext}"), "--print", "before_dl:FILE %(filename)s",
           "--print", "before_dl:THUMB %(thumbnail)s", "--print", "after_move:DONE %(filepath)s",
           "--progress", "--newline", "--progress-template", PROGRESS, *extra]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    # Many sites cap each connection; aria2c splits plain files over 16. Not YouTube: it's fast already,
    # and hammering it gets the IP flagged. HLS/DASH stay native, fetching 8 fragments at once.
    split = split and ARIA2C and not YOUTUBE_VIDEO.search(url)
    if split:  # its resume file saved every second: pause loses ≤1 s
        cmd += ["--downloader", "aria2c", "--downloader", "dash,m3u8:native",
                "--downloader-args", "aria2c:--auto-save-interval=1"]
    p = subprocess.Popen(cmd + ["--", url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    with lock:
        procs[key] = p
    file, errors = None, []
    for line in p.stdout:
        line = line.strip()
        if line.startswith("PROG "):
            vcodec, acodec, status, done, total, estimate, speed = (line.split() + ["NA"] * 8)[1:8]
            with lock:
                jobs[key] = {"part": "audio" if vcodec == "none" else "video" if acodec == "none" else "",
                             "status": status, "done": num(done), "total": num(total) or num(estimate),
                             "speed": num(speed)}
        elif m := ARIA2_READOUT.search(line):
            done, total, speed = (float(m[i]) * UNITS.get(m[i + 1], 1) for i in (1, 3, 5))
            with lock:
                jobs[key] = {"part": "", "status": "downloading", "done": done, "total": total or None, "speed": speed}
        elif line.startswith("FILE "):
            stems[key] = os.path.splitext(line[5:])[0]  # where its partial files go, for cancel to clean up
            with lock:
                if key in downloads and not downloads[key]["title"]:  # a pasted link: name its row after the file
                    downloads[key]["title"] = Path(stems[key]).name
        elif line.startswith("THUMB "):
            with lock:
                if key in downloads and not downloads[key].get("thumb") and line[6:].startswith("http"):
                    downloads[key]["thumb"] = line[6:]
        elif line.startswith("DONE "):
            file = line[5:]
        elif line.startswith("ERROR"):
            errors.append(line)
    p.wait()
    with lock:
        procs.pop(key, None)
        last = jobs.pop(key, None)
        stopped = stops.pop(key, None)
        if last and key in downloads:
            downloads[key]["prog"] = last  # what a paused download keeps showing
    if stopped:
        if stopped == "cancel":
            cleanup(key)
        return {"ok": False, "stopped": stopped}
    # --max-downloads exits non-zero even on success, so judge by the file yt-dlp reported
    if file:
        stems.pop(key, None)
        return {"ok": True, "file": file}
    if split and any("aria2c" in e for e in errors):  # some servers refuse split downloads: retry on one connection
        cleanup(key)  # aria2c's partial file has gaps, so it can't be resumed
        return ytdlp(url, key, name, ext, extra, audio, split=False)
    return {"ok": False, "error": re.sub(r";?\s*please report this issue.*", "", errors[-1]) if errors else "nothing downloaded"}


def download(b, key):
    if b.get("url"):
        return ytdlp(b["url"], key, audio=b.get("audio"))
    page, media, audio = b["page"], b["media"], b.get("audio")
    title = sanitize_filename(str(b.get("title") or ""))[:150].replace("%", "%%")  # % would be a template field
    if not extractor(page):  # feeds, random sites: just the stream we saw (the id keeps feed videos apart)
        return stream(media, key, f"{title or 'video'} [%(id).40B]", page, audio)
    res = ytdlp(page, key, name=title or "%(title).150B", audio=audio)  # named as the tab showed it
    # Broken extractor? Use the stream we saw instead, unless it's YouTube's (split per-track, useless raw).
    if res["ok"] or res.get("stopped") or (urlsplit(media).hostname or "").endswith("googlevideo.com"):
        return res
    fb = stream(media, key, title or "%(title).150B", page, audio)
    return fb if fb["ok"] or fb.get("stopped") else {"ok": False, "error": f"{res['error']} | stream: {fb['error']}"}


def stream(media, key, name, page, audio=False):
    """Download a raw stream seen in the tab; flag it when the site sent video without audio."""
    ext, extra = "%(ext)s", ["--referer", page]
    guess = determine_ext(media, None)
    if guess and guess.lower() not in MEDIA_EXTS:
        # Links like /remote_control.php?file=… make yt-dlp pick ".php" and refuse: its guard against a server
        # choosing a dangerous extension. We choose it here instead, so lifting the guard is safe.
        # ponytail: always .mp4; a php-served webm would get the wrong extension
        ext, extra = "mp4", extra + ["--compat-options", "allow-unsafe-ext"]
    res = ytdlp(media, key, name, ext, extra, audio)
    probe = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0"]
    if res["ok"] and not subprocess.run(probe + [res["file"]], capture_output=True, text=True).stdout.strip():
        res["note"] = "no audio track: this site streams audio separately, try the video's own page"
    return res


def run(b, key):
    """A download in the background; its row in the list records how it ended."""
    res = download(b, key)
    print("✓" if res["ok"] else "✗", res.get("file") or res.get("error") or res.get("stopped"))
    with lock:
        d = downloads.get(key)
        if not d:  # canceled: the row is gone
            return
        if res.get("stopped") == "pause":
            d["status"] = "paused"
        elif res["ok"]:
            d.update(status="done", file=res["file"], msg=res["file"] + (f" — {res['note']}" if res.get("note") else ""))
        else:
            d.update(status="error", msg=res.get("error") or "stopped")


def stop(key, action):
    """Pause kills yt-dlp but keeps its partial files, so the next run resumes them.
    Cancel deletes those too and drops the row; a finished file is kept."""
    with lock:
        p = procs.get(key)
        if p:
            stops[key] = action
        if action == "cancel":
            downloads.pop(key, None)
    if not p:  # paused, failed or done: nothing running, just the leftovers
        if action == "cancel":
            cleanup(key)
    elif os.name == "nt":  # /T takes yt-dlp's children too (ffmpeg merging, node solving, aria2c)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()


def cleanup(key):
    """Delete what an unfinished download left: .part/.ytdl files, fragments, unmerged formats."""
    stem = stems.pop(key, None)
    if not stem:
        return
    name = Path(stem).name
    for f in Path(stem).parent.glob(glob.escape(name) + ".*"):
        rest = f.name[len(name):]
        if ".part" in rest or rest.endswith(".ytdl") or re.fullmatch(r"\.f[\w-]+\.\w+", rest):
            try:
                f.unlink()
            except OSError:
                pass


def preview(key, page, media):
    """Thumbnail, length and size for a list entry; like Video DownloadHelper, a frame of the stream itself."""
    info = {}
    try:
        if yt := YOUTUBE_VIDEO.search(page):  # its streams are split per track, and it has thumbnails anyway
            info["thumb"] = f"https://i.ytimg.com/vi/{yt[1]}/mqdefault.jpg"
        else:
            net = ["-user_agent", std_headers["User-Agent"], "-headers", f"Referer: {page}\r\n"]
            out = subprocess.run(["ffprobe", "-v", "error", *net, "-show_entries", "format=duration,size", "-of", "json",
                                  media], capture_output=True, text=True, timeout=30).stdout
            fmt = json.loads(out or "{}").get("format", {})
            info = {"duration": num(fmt.get("duration")), "size": num(fmt.get("size"))}
            for at in (min(info["duration"] / 10, 30) if info["duration"] else 3, 0):  # 10% in skips black intros
                jpg = subprocess.run(["ffmpeg", "-v", "error", *net, "-ss", str(at), "-i", media, "-frames:v", "1",
                                      "-vf", "scale=320:-2", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                                     capture_output=True, timeout=30).stdout
                if jpg:
                    tid = hashlib.sha1(key.encode()).hexdigest()[:16]
                    with lock:
                        thumbs[tid] = jpg
                        while len(thumbs) > 50:  # ponytail: keeps the newest 50; the lists show far fewer
                            thumbs.pop(next(iter(thumbs)))
                    info["thumb"] = f"/thumb/{tid}"
                    break
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    found = {k: v for k, v in info.items() if v}
    with lock:
        previewing.discard(key)
        for it in sniffed:
            if it["key"] == key:
                it.update(found)
        if key in downloads:  # its Get was clicked before the preview was ready
            for k, v in found.items():
                downloads[key].setdefault(k, v)


def pick_folder(start):
    """Native folder dialog on the desktop: a web page can't hand the server a real path."""
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # else it can open behind the browser
    try:
        return filedialog.askdirectory(parent=root, initialdir=start, title="Save files to", mustexist=True)
    finally:
        root.destroy()


def write_cookies(cookies):
    """Chrome cookie objects → Netscape cookies.txt for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        f = [str(c.get(k, "")) for k in ("domain", "path", "name", "value")]
        if any(ch in s for s in f for ch in "\t\r\n") or not f[0]:
            continue
        domain, path, name, value = f
        sub = "FALSE" if c.get("hostOnly") else "TRUE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        lines.append("\t".join([domain, sub, path or "/", secure, str(int(c.get("expirationDate") or 0)), name, value]))
    COOKIES.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:  # DNS-rebinding guard
            return self.reply(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if page := PAGES.get(path):
            ctype = "text/css" if page.endswith(".css") else "text/html"
            self.reply(200, (Path(__file__).parent / page).read_bytes(), f"{ctype}; charset=utf-8")
        elif path == "/convert/state":  # ?tool=converter|editor|compressor: each page sees its own files
            self.reply(200, {**convert.state(parse_qs(query).get("tool", [None])[0]), "out": settings["convert_out"]})
        elif self.path == "/state":
            with lock:  # copies: other threads keep updating these rows
                state = {"sniffed": [dict(it) for it in sniffed], "jobs": dict(jobs), "out": settings["out"],
                         "downloads": [dict(d) for d in sorted(downloads.values(), key=lambda d: -d["ts"])],
                         "extension_dir": str(EXTENSION_DIR)}
            self.reply(200, state)
        elif self.path.startswith("/thumb/") and (jpg := thumbs.get(self.path[7:])):
            self.reply(200, jpg, "image/jpeg")
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        upload = self.path == "/convert/upload"
        # JSON and octet-stream bodies make browsers preflight cross-site requests, which this server never answers
        if (self.headers.get("Host") not in HOSTS or (origin and not origin.startswith(ORIGINS))
                or not self.headers.get("Content-Type", "").startswith("application/octet-stream" if upload else "application/json")):
            return self.reply(403, {"ok": False, "error": "forbidden"})
        if upload:
            job = convert.add(self.rfile, int(self.headers.get("Content-Length", 0)), unquote(self.headers.get("X-Filename", "file")),
                              self.headers.get("X-Tool", "converter"))
            return self.reply(200 if job else 400, {"ok": bool(job), "id": job and job["id"], "kind": job and job["kind"]})
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self.reply(400, {"ok": False, "error": "bad json"})
        if self.path == "/setup":  # extension install help: shows the extension folder in Explorer
            if b.get("open") != "folder" or os.name != "nt":
                return self.reply(400, {"ok": False, "error": "open the folder yourself"})
            os.startfile(EXTENSION_DIR)
            return self.reply(200, {"ok": True})
        if self.path == "/cookies":
            if not (origin.startswith("chrome-extension://") and isinstance(b.get("cookies"), list)):
                return self.reply(403, {"ok": False, "error": "extension only"})
            write_cookies(c for c in b["cookies"] if isinstance(c, dict))
            return self.reply(200, {"ok": True})
        if self.path == "/stop":
            if b.get("action") not in ("pause", "cancel"):
                return self.reply(400, {"ok": False, "error": "action must be pause or cancel"})
            stop(str(b.get("key")), b["action"])
            return self.reply(200, {"ok": True})
        if self.path.startswith("/convert/"):
            jid, action = str(b.get("id")), self.path[len("/convert/"):]
            if action == "start":
                return self.reply(200, {"ok": convert.start(jid, str(b.get("target")), settings["convert_out"], b.get("recipe"))})
            if action in ("pause", "resume"):
                convert.pause(jid, action == "pause")
            elif action == "cancel":
                convert.cancel(jid)
            elif action == "stop":  # the editor's ✕: stop the export, keep the file
                convert.halt(jid)
            else:
                return self.reply(404, {"ok": False, "error": "not found"})
            return self.reply(200, {"ok": True})
        if self.path == "/folder":
            which = "convert_out" if b.get("which") == "convert_out" else "out"
            if not picking.acquire(blocking=False):
                return self.reply(409, {"ok": False, "error": "the folder window is already open"})
            try:
                folder = pick_folder(settings[which])
            finally:
                picking.release()
            if not folder:  # closed without choosing
                return self.reply(200, {"ok": False})
            settings[which] = str(Path(folder))
            SETTINGS.write_text(json.dumps(settings), encoding="utf-8")
            return self.reply(200, {"ok": True, "out": settings[which]})
        if self.path == "/open":  # show a finished file in Explorer; only files this app produced
            f = b.get("file")
            with lock:
                ours = {d.get("file") for d in downloads.values()}
            ours |= {j.get("file") for j in convert.state()["jobs"]}
            if not (isinstance(f, str) and f in ours and Path(f).is_file()):
                return self.reply(404, {"ok": False, "error": "file moved or deleted"})
            if os.name == "nt":
                subprocess.Popen(f'explorer /select,"{f}"')  # paths can't contain quotes, so this can't break out
            else:
                subprocess.Popen(["xdg-open", str(Path(f).parent)])
            return self.reply(200, {"ok": True})
        urls = [b.get(k) for k in ("url", "page", "media") if b.get(k)]
        links_ok = bool(urls) and all(isinstance(u, str) and urlsplit(u).scheme in ("http", "https") for u in urls)

        if self.path == "/download":
            base, audio, err, job = str(b.get("key") or b.get("url") or b.get("page") or ""), bool(b.get("audio")), None, None
            key = base + "#audio" if audio else base  # the audio of a video is its own download row
            with lock:
                d = downloads.get(key)
                if not d:  # new: a caught video (it moves out of that list) or a pasted link
                    if links_ok and (b.get("url") or (b.get("page") and b.get("media"))):
                        caught = next((it for it in sniffed if it["key"] == base), None)
                        if caught:
                            sniffed.remove(caught)
                        d = downloads[key] = {
                            "key": key, "ts": time.time(), "url": b.get("url"), "page": b.get("page"),
                            "media": b.get("media"), "title": str(b.get("title") or "")[:200], "audio": audio,
                            **{k: caught[k] for k in ("thumb", "duration", "size") if caught and k in caught}}
                    else:
                        err = "need an http(s) link"
                if d and d.get("status") != "busy":  # new, or resume/retry with the stored (freshest) links
                    d.update(status="busy", msg="")
                    job = {k: d[k] for k in ("url", "page", "media", "title", "audio")}
            if err:
                return self.reply(400, {"ok": False, "error": err})
            if job:
                threading.Thread(target=run, args=(job, key), daemon=True).start()
            self.reply(200, {"ok": True})
        elif self.path == "/sniffed":
            page, media = b.get("page"), b.get("media")
            if not (links_ok and page and media):
                return self.reply(400, {"ok": False, "error": "need page + media"})
            ie = extractor(page)
            # YouTube streams are split, per-quality tracks: useless raw, and on home/channel pages
            # (hover previews) there's no telling which video they belong to.
            if (urlsplit(media).hostname or "").endswith("googlevideo.com") and not (ie and ie.is_single_video(page)):
                return self.reply(200, {"ok": True})
            key = page if ie else page + urlsplit(media).path  # one entry per video page, per stream on feeds
            fresh = False
            with lock:
                rows = [downloads[k] for k in (key, key + "#audio") if k in downloads]
                if rows:  # already downloading (video or audio): just keep its link fresh for resume/retry
                    for d in rows:
                        d["media"] = media
                else:
                    old = [it for it in sniffed if it["key"] == key]
                    for it in old:
                        sniffed.remove(it)
                    entry = {"key": key, "page": page, "media": media, "ts": time.time(),
                             "title": clean_title(str(b.get("title") or ""), page)[:200]}
                    if old:  # keep the preview already made for it
                        entry.update({k: old[0][k] for k in ("thumb", "duration", "size") if k in old[0]})
                    sniffed.appendleft(entry)
                    fresh = "thumb" not in entry and key not in previewing
                    if fresh:
                        previewing.add(key)
            if fresh:
                threading.Thread(target=preview, args=(key, page, media), daemon=True).start()
            self.reply(200, {"ok": True})
        else:
            self.reply(404, {"ok": False, "error": "not found"})

    def log_message(self, *_):  # silence per-request noise from the sniffer and polling
        pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console vs. unicode titles
    print(f"Multitool → http://127.0.0.1:{PORT}" + ("" if ARIA2C else "  (aria2c not found: one connection per download)"))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
