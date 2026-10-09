"""Multitool's media engine: the converter, and the exports of the video editor and compressor.

Video, audio and images go through ffmpeg; tables (CSV/TSV/JSON) and PDF text through plain Python.
Files arrive as uploads from the page (a browser can't share real paths), get probed, then processed on request.
Streams the target container already accepts are copied untouched: lossless and instant.
"""
import codecs, csv, ctypes, io, json, math, os, re, shutil, signal, subprocess, tempfile, threading, time, uuid
from pathlib import Path

from yt_dlp.utils import sanitize_filename

UPLOADS = Path(tempfile.gettempdir()) / "multitool-uploads"
shutil.rmtree(UPLOADS, ignore_errors=True)  # leftovers from the last run
UPLOADS.mkdir(parents=True, exist_ok=True)

H264 = ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k"]
VIDEO = {  # ext: (label, video encoder, audio encoder, video codecs kept as-is, audio codecs kept as-is)
    "mp4": ("MP4 · H.264", H264, AAC, {"h264", "hevc", "av1"}, {"aac", "mp3", "opus", "ac3", "eac3", "alac"}),
    "mkv": ("MKV · keeps the streams", None, None, None, None),
    "webm": ("WebM · VP9", ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1"],
             ["-c:a", "libopus", "-b:a", "160k"], {"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
    "mov": ("MOV · H.264", H264, AAC, {"h264", "hevc", "prores", "mjpeg"}, {"aac", "alac", "pcm_s16le"}),
    "avi": ("AVI · MPEG-4", ["-c:v", "mpeg4", "-q:v", "3"], ["-c:a", "libmp3lame", "-q:a", "2"],
            {"mpeg4", "mjpeg"}, {"mp3", "pcm_s16le"}),
    "gif": ("GIF · animated, 480px", None, None, None, None),
}
AUDIO = {  # ext: (label, encoder, codecs kept as-is)
    "mp3": ("MP3", ["-c:a", "libmp3lame", "-q:a", "0"], {"mp3"}),
    "m4a": ("M4A · AAC", ["-c:a", "aac", "-b:a", "256k"], {"aac", "alac"}),
    "wav": ("WAV", ["-c:a", "pcm_s16le"], {"pcm_s16le"}),
    "flac": ("FLAC · lossless", ["-c:a", "flac"], {"flac"}),
    "ogg": ("OGG · Vorbis", ["-c:a", "libvorbis", "-q:a", "6"], {"vorbis"}),
    "opus": ("Opus", ["-c:a", "libopus", "-b:a", "160k"], {"opus"}),
}
IMAGE = {  # ext: (label, encoder)
    "png": ("PNG", []),
    "jpg": ("JPG", ["-q:v", "2"]),
    "webp": ("WebP", ["-c:v", "libwebp", "-quality", "90"]),
    "avif": ("AVIF", ["-c:v", "libaom-av1", "-still-picture", "1", "-crf", "28"]),
    "bmp": ("BMP", []),
    "tiff": ("TIFF", []),
    "ico": ("ICO · 256px icon", ["-vf", "scale=256:256:force_original_aspect_ratio=decrease"]),
}
GIF = ["-an", "-vf", "fps=12,scale='min(480,iw)':-2:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
       "-loop", "0"]
PALETTE = "split[a][b];[a]palettegen[p];[b][p]paletteuse"  # GIFs look far better with their own palette
TABLES = {"csv": ",", "tsv": "\t", "tab": "\t", "json": None}  # ext: the delimiter it's read with if sniffing fails
CANVASES = {"16:9", "9:16", "1:1", "4:3", "4:5"}  # the editor's canvas shapes
TOOLS = {"converter", "editor", "compressor"}  # which page a file was dropped on
csv.field_size_limit(2 ** 31 - 1)  # cells can be long (notes, whole JSON documents…)

LIMIT = 2  # ponytail: conversions at once, fixed; more just fight over the CPU (raise it on a big machine)

jobs, progress, procs, lock = {}, {}, {}, threading.Lock()  # by job id; "_" fields never leave the server
gates, canceled, halted = {}, set(), set()  # gates: pause switch between steps (set = running); halted: stopped, file kept


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def clock(t):
    t = round(t)
    return f"{t // 3600}:{t // 60 % 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60}:{t % 60:02}"


def stopping(jid):
    return jid in canceled or jid in halted


def probe(path):
    """What a file is (video / audio / image), in words, its size, and the codecs that decide what can be copied."""
    ext = path.suffix[1:]
    if ext in TABLES or ext == "pdf":
        return probe_text(path, ext)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    data = json.loads(out or "{}")
    streams, fmt = data.get("streams", []), data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration, container = num(fmt.get("duration")), fmt.get("format_name", "")
    if v and not a and (container.endswith("_pipe") or container == "image2" or not duration or duration < 0.2):
        kind = "image"  # a single frame: photos, icons, a one-frame gif
    else:
        kind = "video" if v else "audio" if a else None
    w, h = (v.get("width"), v.get("height")) if v else (None, None)
    turn = next((num(d["rotation"]) for d in (v or {}).get("side_data_list", []) if "rotation" in d), None)
    if abs(turn or num((v or {}).get("tags", {}).get("rotate")) or 0) % 180 == 90:  # phone videos play turned
        w, h = h, w
    words = [kind.capitalize()] if kind else ["Unknown"]
    if v:
        words.append(f"{v['codec_name'].upper()} {w}×{h}")
    if a:
        words.append(a["codec_name"].upper())
    if duration and kind in ("video", "audio"):
        words.append(clock(duration))
    targets = ([(t, VIDEO[t][0]) for t in VIDEO] + ([(t, AUDIO[t][0]) for t in AUDIO] if a else []) if kind == "video"
               else [(t, AUDIO[t][0]) for t in AUDIO] if kind == "audio"
               else [(t, IMAGE[t][0]) for t in IMAGE] if kind == "image" else [])
    return {"kind": kind, "info": " · ".join(words), "targets": targets, "w": w, "h": h, "duration": duration,
            "_v": v and v["codec_name"], "_a": a and a["codec_name"]}


def scan(path):
    """Line count and text encoding, in one pass: UTF-8, else Windows-1252 (how Excel saves CSV)."""
    decoder, lines, utf8 = codecs.getincrementaldecoder("utf-8")(), 0, True
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            lines += chunk.count(b"\n")
            if utf8:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    utf8 = False
    return lines, "utf-8-sig" if utf8 else "cp1252"


def probe_text(path, ext):
    """Tables (CSV / TSV / JSON) and PDFs: what they are and what they can become."""
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return {"kind": "document", "info": "PDF", "targets": [], "_why": "PDFs need pypdf: py -m pip install pypdf"}
        try:
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                return {"kind": "document", "info": "PDF", "targets": [], "_why": "this PDF is password-protected"}
            pages = len(reader.pages)
        except Exception:  # pypdf has many error types for broken files
            return {"kind": "document", "info": "PDF", "targets": [], "_why": "can't read this PDF"}
        return {"kind": "document", "info": f"PDF · {pages} page{'s' * (pages != 1)}", "targets": [("txt", "TXT · its text")]}
    lines, enc = scan(path)
    if ext == "json":
        try:
            rows = json.loads(path.read_text(encoding=enc, errors="replace"))
        except ValueError:
            return {"kind": "table", "info": "JSON", "targets": [], "_why": "not valid JSON"}
        if not (isinstance(rows, list) and rows and all(isinstance(r, (dict, list)) for r in rows)):
            return {"kind": "table", "info": "JSON", "targets": [], "_why": "this JSON isn't a list of records, so it isn't a table"}
        return {"kind": "table", "info": f"Table · JSON · {len(rows):,} records", "targets": [("csv", "CSV"), ("tsv", "TSV")],
                "_enc": enc}
    with open(path, encoding=enc, errors="replace", newline="") as f:
        sample = f.read(1 << 16)
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter  # Excel in many countries uses ;
    except csv.Error:
        delim = TABLES[ext]
    columns = len(next(csv.reader(io.StringIO(sample), delimiter=delim), []))
    name, other = ("TSV", "csv") if ext in ("tsv", "tab") else ("CSV", "tsv")
    return {"kind": "table", "info": f"Table · {name} · {max(lines - 1, 0):,} rows · {columns} columns",
            "targets": [(other, other.upper()), ("json", "JSON")], "_enc": enc, "_delim": delim, "_lines": lines}


def add(body, length, name, tool="converter"):
    """Save an uploaded file and work out what it can become; None if the upload broke off."""
    jid = uuid.uuid4().hex[:12]
    ext = re.sub(r"\W", "", Path(name).suffix)[:10].lower()
    src = UPLOADS / f"{jid}.{ext or 'bin'}"
    left = length
    with open(src, "wb") as f:
        while left > 0 and (chunk := body.read(min(left, 1 << 20))):
            f.write(chunk)
            left -= len(chunk)
    if left:
        src.unlink(missing_ok=True)
        return None
    job = {"id": jid, "name": name, "ext": ext, "size": length, "ts": time.time(),
           "tool": tool if tool in TOOLS else "converter", "_src": src, **probe(src)}
    job.update(status="ready", msg="") if job["targets"] else job.update(
        status="error", msg=job.pop("_why", None) or "not a video, audio, image, table or PDF file")
    with lock:
        jobs[jid] = job
    return job


def args_for(job, target):
    """ffmpeg output options for a target. Streams the container already accepts are copied (lossless, instant)."""
    v, a = job["_v"], job["_a"]
    if target in IMAGE:
        return ["-map", "0:v:0", "-frames:v", "1", *IMAGE[target][1]]
    if target in AUDIO:
        _, enc, keep = AUDIO[target]
        return ["-map", "0:a:0", "-vn", *(["-c:a", "copy"] if a in keep else enc)]
    if target == "gif":
        return ["-map", "0:v:0", *GIF]
    if target == "mkv":  # Matroska takes nearly any codec: copy every video and audio track
        return ["-map", "0:v", "-map", "0:a?", "-c", "copy"]
    _, venc, aenc, vkeep, akeep = VIDEO[target]
    out = ["-map", "0:v:0", "-map", "0:a:0?", *(["-c:v", "copy"] if v in vkeep else venc),
           *(["-c:a", "copy"] if a in akeep else aenc)]
    if target in ("mp4", "mov"):
        out += ["-movflags", "+faststart", *(["-tag:v", "hvc1"] if v == "hevc" else [])]  # hvc1: plays on Apple
    return out


def clean_recipe(job, r):
    """The editor's / compressor's edit list, checked: only our own numbers and fixed choices ever reach ffmpeg's
    filter text (a filter string can do far more than crop, so nothing from the page goes in as text)."""
    if not isinstance(r, dict):
        raise ValueError("not an edit list")
    w, h, length = job["w"], job["h"], job["duration"]
    even = 2 if job["kind"] == "video" and job["ext"] != "gif" else 1  # H.264 needs even sizes

    def px(value, low, high):
        n = min(max(int(float(value)), low), high)
        return n - n % even

    out = {"flipH": bool(r.get("flipH")), "flipV": bool(r.get("flipV")), "mute": bool(r.get("mute"))}
    if c := r.get("crop"):
        x, y = px(c["x"], 0, w - 2), px(c["y"], 0, h - 2)
        out["crop"] = (px(c["w"], 2, w - x), px(c["h"], 2, h - y), x, y)
    if s := r.get("size"):
        out["size"] = (px(s["w"], 2, 8192), px(s["h"], 2, 8192))
    if r.get("canvas") in CANVASES:
        out["canvas"] = r["canvas"]
    turn = float(r.get("rotate") or 0) % 360
    if not math.isfinite(turn):
        raise ValueError("bad angle")
    if turn:
        out["rotate"] = round(turn, 2)
    if p := r.get("place"):  # where the picture sits on the canvas (it may hang off the edges)
        cw, ch = canvas_size(job, out)
        out["place"] = (min(max(int(float(p["x"])), -2 * cw), 2 * cw), min(max(int(float(p["y"])), -2 * ch), 2 * ch),
                        px(p["w"], 2, 4 * max(cw, ch)), px(p["h"], 2, 4 * max(cw, ch)))
    if (t := r.get("trim")) and length:
        start = min(max(float(t["start"]), 0.0), length)
        end = min(max(float(t["end"]), start + 0.1), length)
        if start > 0.01 or end < length - 0.01:
            out["trim"] = (start, end)
    if r.get("target_bytes"):
        out["target"] = max(int(float(r["target_bytes"])), 10_000)
    return out


def frame_size(job, r):
    return r["crop"][:2] if "crop" in r else (job["w"], job["h"])


def canvas_size(job, r):
    """The canvas: the (cropped) picture's own shape, or the chosen shape at the picture's short side
    (1920×1080 on 9:16 makes 1080×1920, 1:1 makes 1080×1080). Even pixels; the page works it out the same way."""
    fw, fh = frame_size(job, r)
    up = lambda n: math.ceil(n / 2) * 2
    if "canvas" not in r:
        return up(fw), up(fh)
    a, b = map(int, r["canvas"].split(":"))
    s = min(fw, fh)
    return (up(s * a / b), up(s)) if a >= b else (up(s), up(s * b / a))


def edit_filters(r):
    """The filter chain, in the order the preview shows it: mirror, crop, then the final size.
    (A canvas needs a filter graph instead: see picture().)"""
    chain = (["hflip"] if r["flipH"] else []) + (["vflip"] if r["flipV"] else [])
    if "crop" in r:
        chain.append("crop={}:{}:{}:{}".format(*r["crop"]))
    if "size" in r:
        chain.append("scale={}:{}:flags=lanczos".format(*r["size"]))
    return chain


def picture(job, r):
    """The editor's video options: one -vf chain, or, for a canvas or a picture moved / resized / rotated on it, a
    black canvas with the picture laid on top (made from the video itself, so it keeps the video's timing)."""
    if not {"canvas", "place", "rotate"} & r.keys():
        chain = edit_filters(r)
        return ["-vf", ",".join(chain)] if chain else []
    pre = ",".join((["hflip"] if r["flipH"] else []) + (["vflip"] if r["flipV"] else [])
                   + (["crop={}:{}:{}:{}".format(*r["crop"])] if "crop" in r else []))
    (cw, ch), (fw, fh) = canvas_size(job, r), frame_size(job, r)
    x, y, w, h = r.get("place") or ((cw - fw) // 2, (ch - fh) // 2, fw - fw % 2, fh - fh % 2)  # the page sends it; else centered
    turn = ""
    if "rotate" in r:  # turned around its center, with see-through corners
        a = f"{math.radians(r['rotate']):.6f}"
        turn = f",format=yuva420p,rotate={a}:ow='ceil(rotw({a})/2)*2':oh='ceil(roth({a})/2)*2':c=none"
    size = ",scale={}:{}:flags=lanczos".format(*r["size"]) if "size" in r else ""
    graph = (f"[0:v]{pre + ',' if pre else ''}split[a][b];[a]scale={cw}:{ch},drawbox=t=fill:c=black[bg];"
             f"[b]scale={w}:{h}:flags=lanczos{turn}[fg];"
             f"[bg][fg]overlay=x={x}+{w}/2-overlay_w/2:y={y}+{h}/2-overlay_h/2{size}[out]")
    return ["-filter_complex", graph, "-map", "[out]", "-map", "0:a?"]


def reserve(folder, stem, ext):
    """Create folder/stem.ext, or 'stem (2).ext' and so on: never overwrites an existing file."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, n = sanitize_filename(stem) or "converted", 1
    while True:
        out = folder / (f"{stem}.{ext}" if n == 1 else f"{stem} ({n}).{ext}")
        try:
            open(out, "x").close()
            return out
        except FileExistsError:
            n += 1


def start(jid, target, folder, recipe=None):
    """Queue a conversion, or an editor/compressor export when there's an edit list (a first one, another format,
    or a retry). False if it can't be queued, including when the edit list doesn't check out."""
    with lock:
        job = jobs.get(jid)
        if not job or job["status"] in ("queued", "busy", "paused") or target not in dict(job["targets"]):
            return False
        try:
            edits = None if recipe is None else clean_recipe(job, recipe)
        except (ValueError, TypeError, KeyError, OverflowError):
            return False
        suffix = {"editor": " (edited)", "compressor": " (compressed)"}.get(job["tool"], "") if edits is not None else ""
        job.update(status="queued", msg="", target=target, _recipe=edits, _stem=Path(job["name"]).stem + suffix,
                   _folder=Path(folder), _queued=time.time())
    pump()
    return True


def pump():
    """Start queued jobs, oldest first, while fewer than LIMIT run (paused ones don't count)."""
    with lock:
        free = LIMIT - sum(j["status"] == "busy" for j in jobs.values())
        nxt = sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: j["_queued"])[:max(free, 0)]
        for job in nxt:
            job["status"] = "busy"
    for job in nxt:
        out = reserve(job["_folder"], job["_stem"], job["target"])
        threading.Thread(target=run, args=(job, job["target"], out), daemon=True).start()


def run(job, target, out):
    jid = job["id"]
    with lock:
        if jid not in jobs:  # canceled while it was being started
            out.unlink(missing_ok=True)
            return
    worker = (convert_text if job["kind"] in ("table", "document")
              else export if job.get("_recipe") is not None else convert_media)
    ok, error, note = worker(job, target, out)
    with lock:
        procs.pop(jid, None)
        gates.pop(jid, None)
        progress.pop(jid, None)
        gone, halt = jid in canceled, jid in halted
        canceled.discard(jid)
        halted.discard(jid)
        if halt:
            job.update(status="ready", msg="export canceled")
        elif not gone and ok:
            job.update(status="done", msg=str(out), file=str(out), note=note, out_size=out.stat().st_size)
        elif not gone:
            job.update(status="error", msg=error)
    if gone or halt or not ok:
        out.unlink(missing_ok=True)  # never leave a half-written file behind
    if gone:
        job["_src"].unlink(missing_ok=True)  # cancel() can't delete it while something is still reading it
    pump()


def ffmpeg(job, args, out, report=None):
    """One ffmpeg run that pause and cancel can reach; report(stats) gets each block of progress stats.
    Returns (ok, error)."""
    jid = job["id"]
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args,
                          "-progress", "pipe:1", "-nostats", str(out)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    with lock:
        procs[jid] = p
        if stopping(jid):  # stopped while it was starting
            p.kill()
    stats, errors = {}, []
    for line in p.stdout:
        key, eq, val = line.strip().partition("=")
        if not eq or not re.fullmatch(r"\w+", key):  # not a progress stat: ffmpeg is reporting an error
            errors.append(line.strip())
            continue
        stats[key] = val
        if key == "progress" and report:  # end of one block of stats
            with lock:
                report(stats)
    p.wait()
    return p.returncode == 0, errors[-1] if errors else "ffmpeg stopped unexpectedly"


def convert_media(job, target, out):
    """The converter: one ffmpeg run. Returns (ok, error, note)."""
    def report(s):
        done = num(s.get("out_time_us"))
        progress[job["id"]] = {"done": done / 1e6 if done else None, "total": job["duration"],
                               "speed": s.get("speed"), "size": num(s.get("total_size"))}
    ok, error = ffmpeg(job, ["-i", str(job["_src"]), *args_for(job, target)], out, report)
    return ok, error, None


def export(job, target, out):
    """Editor / compressor: the whole edit list in one ffmpeg chain; two passes to hit a size goal for video;
    a few tries (quality first, then dimensions) for images and GIFs. Returns (ok, error, note)."""
    jid, r = job["id"], job["_recipe"]
    gate = threading.Event()
    gate.set()
    with lock:
        gates[jid] = gate  # pause also holds the export between ffmpeg runs
    trim = ["-ss", f"{r['trim'][0]:.3f}", "-to", f"{r['trim'][1]:.3f}"] if "trim" in r else []
    length = r["trim"][1] - r["trim"][0] if "trim" in r else job["duration"] or 0

    def once(args, dest, offset=0.0, total=None):
        gate.wait()
        if stopping(jid):
            return False, "stopped"

        def report(s):
            t = num(s.get("out_time_us"))
            progress[jid] = {"done": offset + (t / 1e6 if t else 0), "total": total or length, "speed": s.get("speed")}
        return ffmpeg(job, [*trim, "-i", str(job["_src"]), *args], dest, report)

    def vf(*extra):
        chain = [*edit_filters(r), *extra]
        return ["-vf", ",".join(chain)] if chain else []

    if target == "mp4":
        audio = ["-an"] if r["mute"] else ["-c:a", "aac", "-b:a", "128k"]
        if "target" not in r:
            return (*once([*picture(job, r), *H264, *audio, "-movflags", "+faststart"], out), None)
        kbps = (r["target"] * 8 / max(length, 0.1) - (0 if r["mute"] else 128_000)) / 1000
        if kbps < 40:
            return False, f"{r['target'] / 1048576:.2f} MB is too small for {clock(length)} of video", None
        log = UPLOADS / f"{jid}-pass"
        enc = [*picture(job, r), "-c:v", "libx264", "-b:v", f"{kbps:.0f}k", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-passlogfile", str(log)]
        try:  # pass 1 measures, pass 2 spends the bitrate where it's needed
            ok, error = once([*enc, "-pass", "1", "-an", "-f", "null"], os.devnull, 0, 2 * length)
            if ok:
                ok, error = once([*enc, "-pass", "2", *audio, "-movflags", "+faststart"], out, length, 2 * length)
        finally:
            for f in UPLOADS.glob(f"{jid}-pass*"):
                f.unlink(missing_ok=True)
        return ok, error, None

    gif = target == "gif"
    quality = None if gif or target == "png" else (  # 0–100, higher is better
        (lambda q: ["-q:v", str(round(31 - q * 0.29))]) if target == "jpg" else (lambda q: ["-c:v", "libwebp", "-quality", str(q)]))

    def encode(q=90, scale=1.0):
        extra = [f"scale=iw*{scale:.4f}:-1:flags=lanczos"] if scale < 1 else []
        args = (["-an", *vf(*extra, PALETTE), "-loop", "0"] if gif
                else [*vf(*extra), "-frames:v", "1", *(quality(q) if quality else [])])
        ok, error = once(args, out)
        return (out.stat().st_size if ok else None), error

    if "target" not in r:
        size, error = encode()
        return size is not None, error, None
    return squeeze(job, encode, r["target"], quality is not None)


def squeeze(job, encode, target, has_quality):
    """Fit a size goal: the best quality that fits first (if the format has a quality setting), then smaller
    dimensions. Returns (ok, error, note)."""
    jid, tries = job["id"], [0]

    def attempt(**kw):
        tries[0] += 1
        with lock:
            progress[jid] = {"done": tries[0], "total": 14, "unit": "tries"}
        return encode(**kw)

    q = 90
    if has_quality:  # binary search for the highest quality that fits
        low, high, best = 5, 95, None
        while low <= high and tries[0] < 7:
            mid = (low + high) // 2
            size, error = attempt(q=mid)
            if size is None:
                return False, error, None
            low, high, best = (mid + 1, high, mid) if size <= target else (low, mid - 1, best)
        if best is not None:
            size, error = attempt(q=best)  # the last try isn't always the best fit
            return size is not None, error, None
        q = 5
    size, error = attempt(q=q)
    scale = 1.0
    while size is not None and size > target and tries[0] < 14:  # still too big: fewer pixels
        scale *= max(0.1, min(0.9, (target / size) ** 0.5 * 0.97))
        size, error = attempt(q=q, scale=scale)
    if size is None:
        return False, error, None
    return True, None, (f"shrunk to {scale:.0%} of its width and height to fit" if size <= target
                        else f"couldn't reach the goal: the smallest try is {size / 1048576:.2f} MB")


def convert_text(job, target, out):
    """Tables and PDFs in Python, step by step so they can pause and cancel. Returns (ok, error, note)."""
    jid, gate, note = job["id"], threading.Event(), []
    gate.set()
    with lock:
        gates[jid] = gate
    try:
        for done, total, unit in text_steps(job, target, out, note):
            gate.wait()  # paused: wait here
            if stopping(jid):
                return False, "stopped", None
            with lock:
                progress[jid] = {"done": done, "total": total, "unit": unit}
    except Exception as e:  # any broken input: show it on the file's row instead of losing it in a thread
        return False, f"{type(e).__name__}: {e}"[:300], None
    return True, None, note[0] if note else None


def cell(value):
    """A JSON value as a table cell: nested data stays JSON, null becomes empty."""
    return "" if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else value


def text_steps(job, target, out, note):
    """Write the converted file, yielding (done, total, unit) along the way."""
    src, ext = job["_src"], job["ext"]
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(src)
        if reader.is_encrypted:
            reader.decrypt("")
        found = False
        with open(out, "w", encoding="utf-8") as f:
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                found = found or bool(text.strip())
                f.write(text.rstrip() + "\n\n")
                yield i, len(reader.pages), "pages"
        if not found:
            note.append("no text in it: probably a scanned PDF, which needs OCR")
        return
    sep = "\t" if target == "tsv" else ","
    # CSV/TSV get a BOM so Excel opens UTF-8 correctly; JSON stays plain UTF-8
    with open(src, encoding=job["_enc"], errors="replace", newline="") as fin, \
            open(out, "w", encoding="utf-8" if target == "json" else "utf-8-sig", newline="") as fout:
        if ext == "json":
            rows = json.load(fin)
            header = list(dict.fromkeys(k for r in rows if isinstance(r, dict) for k in r))  # every key, first-seen order
            w = csv.writer(fout, delimiter=sep)
            if header:
                w.writerow(header)
            for i, r in enumerate(rows, 1):
                w.writerow([cell(c) for c in ([r.get(k) for k in header] if isinstance(r, dict) else r)])
                if i % 1000 == 0 or i == len(rows):
                    yield i, len(rows), "rows"
            return
        reader, total = csv.reader(fin, delimiter=job["_delim"]), max(job["_lines"], 1)
        if target == "json":  # one object per row, keyed by the header row
            header = next(reader, [])
            fout.write("[")
            for i, row in enumerate(reader, 1):
                fout.write(("," if i > 1 else "") + "\n  " + json.dumps(dict(zip(header, row)), ensure_ascii=False))
                if i % 1000 == 0:
                    yield i, total, "rows"
            fout.write("\n]\n")
        else:
            w = csv.writer(fout, delimiter=sep)
            for i, row in enumerate(reader, 1):
                w.writerow(row)
                if i % 1000 == 0:
                    yield i, total, "rows"


def pause(jid, on):
    """Freeze or thaw a job. A running ffmpeg is suspended (it has no pause of its own, and a stopped encode can't
    pick up where it was); Python steps and multi-run exports also wait at their gate."""
    with lock:
        p, gate, job = procs.get(jid), gates.get(jid), jobs.get(jid)
        if not (job and (p or gate)):
            return
        job["status"] = "paused" if on else "busy"
    if gate:
        gate.clear() if on else gate.set()
    if p and p.poll() is None:  # only a live process (its id could belong to something else by now)
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, p.pid)  # PROCESS_SUSPEND_RESUME
            (ctypes.windll.ntdll.NtSuspendProcess if on else ctypes.windll.ntdll.NtResumeProcess)(handle)
            ctypes.windll.kernel32.CloseHandle(handle)
        else:
            p.send_signal(signal.SIGSTOP if on else signal.SIGCONT)
    if on:
        pump()  # a paused job frees its slot for the next queued one


def halt(jid):
    """Stop a running or queued export but keep the file loaded (the editor's ✕). Its partial output is deleted."""
    with lock:
        p, gate, job = procs.get(jid), gates.get(jid), jobs.get(jid)
        if not job or job["status"] not in ("queued", "busy", "paused"):
            return
        if job["status"] == "queued":
            job.update(status="ready", msg="export canceled")
            return
        halted.add(jid)
    if gate:
        gate.set()  # a paused one wakes up and sees it's stopped
    if p and p.poll() is None:
        p.kill()  # also ends a frozen one


def cancel(jid):
    """Stop a conversion (its partial output is deleted) and drop the file from the list. Converted files stay."""
    with lock:
        job, p, gate = jobs.pop(jid, None), procs.get(jid), gates.get(jid)
        if p or gate:
            canceled.add(jid)
    if gate:
        gate.set()  # a paused one wakes up and sees it's canceled
    if p and p.poll() is None:
        p.kill()  # also ends a frozen one
        p.wait()
    if job:
        try:
            job["_src"].unlink(missing_ok=True)
        except OSError:  # still being read: run() deletes it once the job stops
            pass


def state(tool=None):
    with lock:
        return {"jobs": [{k: v for k, v in j.items() if not k.startswith("_")}
                         for j in sorted(jobs.values(), key=lambda j: -j["ts"]) if tool in (None, j["tool"])],
                "progress": dict(progress)}
