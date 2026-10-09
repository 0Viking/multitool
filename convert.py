"""Universal converter: video, audio and images through ffmpeg.

Files arrive as uploads from the page (a browser can't share real paths), get probed, then converted on request.
Streams the target container already accepts are copied untouched: lossless and instant.
"""
import ctypes, json, os, re, shutil, signal, subprocess, tempfile, threading, time, uuid
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

LIMIT = 2  # ponytail: conversions at once, fixed; more just fight over the CPU (raise it on a big machine)

jobs, progress, procs, lock = {}, {}, {}, threading.Lock()  # by job id; "_" fields never leave the server
canceled = set()


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def clock(t):
    t = round(t)
    return f"{t // 3600}:{t // 60 % 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60}:{t % 60:02}"


def probe(path):
    """What a file is (video / audio / image), in words, and the codecs that decide what can be copied."""
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
    words = [kind.capitalize()] if kind else ["Unknown"]
    if v:
        words.append(f"{v['codec_name'].upper()} {v.get('width')}×{v.get('height')}")
    if a:
        words.append(a["codec_name"].upper())
    if duration and kind in ("video", "audio"):
        words.append(clock(duration))
    targets = ([(t, VIDEO[t][0]) for t in VIDEO] + ([(t, AUDIO[t][0]) for t in AUDIO] if a else []) if kind == "video"
               else [(t, AUDIO[t][0]) for t in AUDIO] if kind == "audio"
               else [(t, IMAGE[t][0]) for t in IMAGE] if kind == "image" else [])
    return {"kind": kind, "info": " · ".join(words), "targets": targets,
            "_v": v and v["codec_name"], "_a": a and a["codec_name"], "_duration": duration}


def add(body, length, name):
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
    job = {"id": jid, "name": name, "ext": ext, "size": length, "ts": time.time(), "_src": src, **probe(src)}
    job.update(status="ready", msg="") if job["kind"] else job.update(
        status="error", msg="not a video, audio or image file ffmpeg can read")
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


def start(jid, target, folder):
    """Queue a conversion (a first one, another format, or a retry). False if it can't be queued."""
    with lock:
        job = jobs.get(jid)
        if not job or job["status"] in ("queued", "busy", "paused") or target not in dict(job["targets"]):
            return False
        job.update(status="queued", msg="", target=target, _folder=Path(folder), _queued=time.time())
    pump()
    return True


def pump():
    """Start queued conversions, oldest first, while fewer than LIMIT run (paused ones don't count)."""
    with lock:
        free = LIMIT - sum(j["status"] == "busy" for j in jobs.values())
        nxt = sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: j["_queued"])[:max(free, 0)]
        for job in nxt:
            job["status"] = "busy"
    for job in nxt:
        out = reserve(job["_folder"], Path(job["name"]).stem, job["target"])
        threading.Thread(target=run, args=(job, job["target"], out), daemon=True).start()


def run(job, target, out):
    jid = job["id"]
    with lock:
        if jid not in jobs:  # canceled while it was being started
            out.unlink(missing_ok=True)
            return
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(job["_src"]),
                          *args_for(job, target), "-progress", "pipe:1", "-nostats", str(out)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    with lock:
        procs[jid] = p
    stats, errors = {}, []
    for line in p.stdout:
        key, _, val = line.strip().partition("=")
        if not re.fullmatch(r"\w+", key) or not _:  # not a progress stat: ffmpeg is reporting an error
            errors.append(line.strip())
            continue
        stats[key] = val
        if key == "progress":  # end of one block of stats
            done = num(stats.get("out_time_us"))
            with lock:
                progress[jid] = {"done": done / 1e6 if done else None, "total": job["_duration"],
                                 "speed": stats.get("speed"), "size": num(stats.get("total_size"))}
    p.wait()
    with lock:
        procs.pop(jid, None)
        progress.pop(jid, None)
        gone = jid in canceled
        canceled.discard(jid)
        if not gone:
            job.update(status="done", msg=str(out), file=str(out)) if p.returncode == 0 else job.update(
                status="error", msg=errors[-1] if errors else "ffmpeg stopped unexpectedly")
    if gone or p.returncode:
        out.unlink(missing_ok=True)  # never leave a half-written file behind
    pump()


def pause(jid, on):
    """Freeze or thaw ffmpeg: it has no pause of its own, and a stopped encode can't pick up where it was."""
    with lock:
        p, job = procs.get(jid), jobs.get(jid)
        if not (p and job):
            return
        job["status"] = "paused" if on else "busy"
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, p.pid)  # PROCESS_SUSPEND_RESUME
        (ctypes.windll.ntdll.NtSuspendProcess if on else ctypes.windll.ntdll.NtResumeProcess)(handle)
        ctypes.windll.kernel32.CloseHandle(handle)
    else:
        p.send_signal(signal.SIGSTOP if on else signal.SIGCONT)
    if on:
        pump()  # a paused conversion frees its slot for the next queued one


def cancel(jid):
    """Stop a conversion (its partial output is deleted) and drop the file from the list. Converted files stay."""
    with lock:
        job, p = jobs.pop(jid, None), procs.get(jid)
        if p:
            canceled.add(jid)
    if p:
        p.kill()  # also ends a frozen one
        p.wait()
    if job:
        job["_src"].unlink(missing_ok=True)


def state():
    with lock:
        return {"jobs": [{k: v for k, v in j.items() if not k.startswith("_")}
                         for j in sorted(jobs.values(), key=lambda j: -j["ts"])],
                "progress": dict(progress)}
