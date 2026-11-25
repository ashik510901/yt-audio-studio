"""
core.py - YT Audio Studio (Web) এর ইঞ্জিন।
এখানে কোনো Streamlit কোড নেই; শুধু ডাউনলোড + MP3 কনভার্ট + কভার আর্ট + ট্যাগের লজিক।

প্রধান ধাপ (সব সার্ভারে চলে):
  ১. yt-dlp দিয়ে শুধু অডিও স্ট্রিমের তথ্য আনা (ভিডিও নামে না)
  ২. সমান্তরাল রেঞ্জ-ডাউনলোড (না চললে সাধারণ yt-dlp ডাউনলোডে ফলব্যাক)
  ৩. FFmpeg দিয়ে এক ধাপে কনভার্ট + কভার আর্ট + ট্যাগ (লম্বা অডিওতে "টার্বো": সব CPU কোর)
"""

import io
import os
import re
import sys
import stat
import time
import shutil
import zipfile
import platform
import tempfile
import threading
import subprocess
import urllib.request

# ---------------------------------------------------------------------
#  ধ্রুবক
# ---------------------------------------------------------------------
FORMATS = {
    "MP3": {"codec": "mp3", "qualities": ["320", "256", "192", "128", "96"], "default": "192", "cover": True},
    "M4A - সবচেয়ে দ্রুত (কনভার্ট ছাড়া)": {"codec": "m4a", "qualities": ["Original"], "default": "Original", "cover": True},
    "OPUS": {"codec": "opus", "qualities": ["160", "128", "96"], "default": "128", "cover": False},
    "FLAC (Lossless)": {"codec": "flac", "qualities": ["Lossless"], "default": "Lossless", "cover": False},
    "WAV (Lossless)": {"codec": "wav", "qualities": ["Lossless"], "default": "Lossless", "cover": False},
}

COVER_SIZES = {
    "300×300 - সব ডিভাইস / বাটন ফোন (সেরা সামঞ্জস্য)": 300,
    "500×500 - স্ট্যান্ডার্ড": 500,
    "800×800 - হাই কোয়ালিটি (শুধু আধুনিক প্লেয়ার)": 800,
}

FMT_NORMAL = "bestaudio[ext=m4a]/bestaudio/best"
FMT_SMALL = "worstaudio[ext=m4a]/worstaudio/bestaudio[ext=m4a]/bestaudio/best"


class Cancelled(Exception):
    """ব্যবহারকারী কাজ বাতিল করেছে।"""


class TooLong(Exception):
    """ভিডিও সীমার চেয়ে লম্বা (ফ্রি সার্ভারের সুরক্ষা)।"""


# ---------------------------------------------------------------------
#  ছোট সহায়ক
# ---------------------------------------------------------------------
def fmt_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name, limit=140):
    name = _BAD_CHARS.sub("", name or "audio")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].strip(" .") or "audio"


def ffmpeg_bin():
    return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_ok():
    try:
        r = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def cpu_cores():
    return os.cpu_count() or 1


def ytdlp_version():
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "?"


def update_ytdlp():
    """yt-dlp আপডেট করার চেষ্টা। (আপডেটের পর অ্যাপ Reboot করতে হবে।)"""
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                       capture_output=True, text=True, timeout=300)
    return r.returncode == 0, (r.stdout + "\n" + r.stderr)[-1500:]


def ensure_deno():
    """yt-dlp-র YouTube চ্যালেঞ্জের জন্য JavaScript রানটাইম (Deno) লাগে। না থাকলে GitHub থেকে নামিয়ে নেয়।"""
    try:
        exe = shutil.which("deno")
        if exe:
            return exe
        if not sys.platform.startswith("linux"):
            return None
        arch = platform.machine().lower()
        target = "aarch64-unknown-linux-gnu" if arch in ("aarch64", "arm64") else "x86_64-unknown-linux-gnu"
        d = os.path.join(os.path.expanduser("~"), ".cache", "ytas_deno")
        exe = os.path.join(d, "deno")
        if not os.path.isfile(exe):
            os.makedirs(d, exist_ok=True)
            url = f"https://github.com/denoland/deno/releases/latest/download/deno-{target}.zip"
            zp = os.path.join(d, "deno.zip")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r, open(zp, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(zp) as z:
                z.extract("deno", d)
            os.remove(zp)
            os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        ok = subprocess.run([exe, "--version"], capture_output=True, timeout=30).returncode == 0
        return exe if ok else None
    except Exception:
        return None


def write_cookies(text):
    """Secrets-এ রাখা Netscape ফরম্যাটের কুকি টেক্সট থেকে ফাইল বানায়।"""
    if not text or not str(text).strip():
        return None
    p = os.path.join(tempfile.gettempdir(), "ytas_cookies.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(text).strip() + "\n")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


def cleanup_old(base_dir, max_age_sec=2 * 3600):
    """পুরনো অস্থায়ী ফাইল/ফোল্ডার মুছে সার্ভারের ডিস্ক ফাঁকা রাখে।"""
    try:
        now = time.time()
        for name in os.listdir(base_dir):
            p = os.path.join(base_dir, name)
            try:
                if now - os.path.getmtime(p) > max_age_sec:
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------
#  yt-dlp অপশন
# ---------------------------------------------------------------------
def ydl_base_opts(s, light=True):
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "retries": 8, "fragment_retries": 8, "socket_timeout": 30,
    }
    ea = {}
    if light:
        ea["skip"] = ["hls", "dash", "translated_subs"]   # হালকা ও দ্রুত তথ্য আনা
    clients = [c.strip() for c in (s.get("clients") or []) if c and c.strip()]
    if clients:
        ea["player_client"] = clients
    if ea:
        o["extractor_args"] = {"youtube": ea}
    if s.get("cookies"):
        o["cookiefile"] = s["cookies"]
    if s.get("proxy"):
        o["proxy"] = s["proxy"]
    return o


def get_info(url, s):
    """লিংকের শিরোনাম, সময়, থাম্বনেল ইত্যাদি (ডাউনলোড ছাড়া)।"""
    import yt_dlp
    opts = ydl_base_opts(s, light=True)
    opts.update({"skip_download": True, "extract_flat": "in_playlist"})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("লিংক থেকে তথ্য পাওয়া যায়নি।")
    if info.get("_type") == "playlist":
        raise ValueError("এটি প্লেলিস্টের লিংক। ফ্রি সার্ভারে প্লেলিস্ট সমর্থিত নয়, একটি ভিডিওর লিংক দিন।")
    return {
        "title": info.get("title") or "Unknown",
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
    }


# ---------------------------------------------------------------------
#  স্মার্ট ট্যাগ
# ---------------------------------------------------------------------
_NOISE = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*?(official|lyric|lyrics|video|audio|visuali[sz]er|\bhd\b|\bhq\b|4k|1080p|"
    r"remaster(?:ed)?|\bmv\b|m/v|full song|music video|with lyrics)[^\)\]\}]*?[\)\]\}]", re.I)


def smart_tags(info):
    """ভিডিওর তথ্য থেকে পরিষ্কার টাইটেল / আর্টিস্ট / অ্যালবাম বের করে।"""
    raw = (info.get("track") or info.get("title") or "").strip()
    title = _NOISE.sub("", raw).strip(" -–—|")
    artist = (info.get("artist") or "").strip()
    if not artist and not info.get("track"):
        m = re.match(r"^\s*(.{2,60}?)\s+[-–—]\s+(.+)$", title)
        if m:
            artist, title = m.group(1).strip(), m.group(2).strip()
    if not artist:
        up = info.get("uploader") or info.get("channel") or ""
        artist = re.sub(r"\s*-\s*Topic$|VEVO$", "", up, flags=re.I).strip()
    album = (info.get("album") or info.get("playlist_title") or "").strip()
    year = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")
    return {"title": title or raw, "artist": artist, "album": album, "date": year}


# ---------------------------------------------------------------------
#  কভার আর্ট (ফোন-সামঞ্জস্যপূর্ণ: বেসলাইন JPEG, ছোট সাইজ, ID3v2.3)
# ---------------------------------------------------------------------
def fetch_thumbnail(info):
    from PIL import Image
    job = info.get("_cover_job")
    if job:
        try:
            job["thread"].join(timeout=25)
        except Exception:
            pass
        if job.get("im") is not None:
            return job["im"]
    cands = []
    if info.get("thumbnail"):
        cands.append(info["thumbnail"])
    try:
        th = [t for t in (info.get("thumbnails") or []) if t.get("url")]
        th.sort(key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)), reverse=True)
        for t in th:
            if t["url"] not in cands and ".jpg" in t["url"].split("?")[0].lower():
                cands.append(t["url"])
    except Exception:
        pass
    vid = info.get("id")
    if vid:
        cands += [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                  f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
                  f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"]
    for u in cands:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=20).read()
            im = Image.open(io.BytesIO(data))
            im.load()
            if im.size[0] >= 100 and im.size[1] >= 100:
                return im
        except Exception:
            continue
    return None


def make_cover_file(im, size, square, folder):
    from PIL import Image, ImageFilter, ImageEnhance
    im = im.convert("RGB")
    w, h = im.size
    try:   # ইউটিউব থাম্বনেলের কালো পট্টি (letterbox) কেটে ফেলা
        mask = im.convert("L").point(lambda v: 255 if v > 18 else 0)
        bb = mask.getbbox()
        if bb and (bb[3] - bb[1]) < h * 0.96 and (bb[3] - bb[1]) > h * 0.4:
            im = im.crop((0, bb[1], w, bb[3]))
            w, h = im.size
    except Exception:
        pass
    if square:   # মাঝখান থেকে ক্রপ
        sq = min(w, h)
        im = im.crop(((w - sq) // 2, (h - sq) // 2, (w + sq) // 2, (h + sq) // 2)).resize((size, size), Image.LANCZOS)
    else:        # পুরো ছবি: স্কয়ার ক্যানভাসে মাঝখানে, পেছনে ঝাপসা ব্যাকগ্রাউন্ড
        bg_s = min(w, h)
        bg = im.crop(((w - bg_s) // 2, (h - bg_s) // 2, (w + bg_s) // 2, (h + bg_s) // 2)).resize((size, size), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(size // 18))
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        fg = im.copy()
        fg.thumbnail((size, size), Image.LANCZOS)
        bg.paste(fg, ((size - fg.size[0]) // 2, (size - fg.size[1]) // 2))
        im = bg
    path = os.path.join(folder, "cover.jpg")
    im.save(path, "JPEG", quality=90, progressive=False, optimize=False, subsampling=2)
    return path


def _popen_flags():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}


def has_cover(path):
    try:
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", path], capture_output=True, timeout=60,
                           **_popen_flags())
        return "Video:" in (r.stderr or b"").decode("utf-8", "replace")
    except Exception:
        return False


def id3_size(path):
    try:
        with open(path, "rb") as f:
            h = f.read(10)
        if h[:3] != b"ID3":
            return 0
        return ((h[6] & 127) << 21) | ((h[7] & 127) << 14) | ((h[8] & 127) << 7) | (h[9] & 127)
    except Exception:
        return 0


# ---------------------------------------------------------------------
#  সমান্তরাল ডাউনলোড
# ---------------------------------------------------------------------
def parallel_range_download(url, headers, part_path, on_progress, cancel, workers=12):
    """একই ফাইল অনেক সংযোগে টুকরো টুকরো করে একসাথে নামায়। সফল হলে True, না হলে False।"""
    def req(a, b):
        h = dict(headers)
        h["Range"] = f"bytes={a}-{b}"
        return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30)

    try:
        with req(0, 0) as r:
            cr = r.headers.get("Content-Range") or ""
            if getattr(r, "status", 206) != 206 or "/" not in cr:
                return False
            total = int(cr.split("/")[-1])
            r.read()
    except Exception:
        return False
    if total < 1024:
        return False

    chunk = 1024 * 1024
    chunks = [(a, min(a + chunk - 1, total - 1)) for a in range(0, total, chunk)]
    nxt, done, err = [0], [0], [None]
    lock, flock, stop = threading.Lock(), threading.Lock(), threading.Event()

    with open(part_path, "wb") as f:
        f.truncate(total)
    f = open(part_path, "r+b")

    def worker():
        while not stop.is_set() and err[0] is None:
            with lock:
                if nxt[0] >= len(chunks):
                    return
                a, b = chunks[nxt[0]]
                nxt[0] += 1
            for attempt in range(4):
                got = 0
                try:
                    with req(a, b) as r:
                        if getattr(r, "status", 206) != 206:
                            raise IOError("range not supported")
                        pos = a
                        while True:
                            buf = r.read(65536)
                            if not buf:
                                break
                            with flock:
                                f.seek(pos)
                                f.write(buf)
                            pos += len(buf)
                            got += len(buf)
                            with lock:
                                done[0] += len(buf)
                            if stop.is_set():
                                return
                        if pos != b + 1:
                            raise IOError("short read")
                    break
                except Exception as ex:
                    with lock:
                        done[0] -= got
                    if attempt == 3:
                        err[0] = ex
                        return
                    time.sleep(0.4 * (attempt + 1))

    n = max(1, min(workers, len(chunks)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    last = 0.0
    try:
        while any(t.is_alive() for t in threads):
            if cancel.is_set():
                break
            now = time.time()
            if now - last >= 0.3:
                last = now
                d = max(0, min(done[0], total))
                sp = d / max(now - t0, 0.001)
                on_progress(d / total, sp, ((total - d) / sp) if sp > 0 else 0)
            time.sleep(0.1)
    finally:
        stop.set()   # কোনো কারণে মূল থ্রেড থামলে (বাতিল/এরর) ওয়ার্কাররাও থামবে
        for t in threads:
            t.join(timeout=5)
        f.close()
    if cancel.is_set():
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise Cancelled()
    if err[0] is not None or done[0] < total:
        try:
            os.remove(part_path)
        except OSError:
            pass
        return False
    on_progress(1.0, total / max(time.time() - t0, 0.001), 0)
    return True


def _check_len(info, max_seconds):
    d = info.get("duration") or 0
    if max_seconds and d and d > max_seconds:
        raise TooLong(f"ভিডিওটি {fmt_time(d)} লম্বা; ফ্রি সার্ভারে সর্বোচ্চ {fmt_time(max_seconds)} সমর্থিত।")


def fast_fetch(url, s, src_base, on_progress, cancel, want_cover, max_seconds):
    """দ্রুত পথ: তথ্য আনা + সমান্তরাল ডাউনলোড। সফল হলে (পাথ, info, তথ্য-সময়, ডাউনলোড-সময়), না হলে None।"""
    import yt_dlp
    t0 = time.time()
    opts = ydl_base_opts(s, light=True)
    opts["format"] = FMT_SMALL if s.get("ultra") else FMT_NORMAL
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("_type") == "playlist":
        return None
    _check_len(info, max_seconds)
    if info.get("protocol") not in ("https", "http") or info.get("fragments") or not info.get("url"):
        return None
    t_extract = time.time() - t0
    job = None
    if want_cover:
        base = dict(info)
        job = {"im": None}

        def _bg():
            try:
                job["im"] = fetch_thumbnail(base)
            except Exception:
                job["im"] = None
        job["thread"] = threading.Thread(target=_bg, daemon=True)
        job["thread"].start()
    headers = dict(info.get("http_headers") or {})
    if info.get("cookies"):
        headers["Cookie"] = info["cookies"]
    path = f"{src_base}.{info.get('ext') or 'm4a'}"
    part = path + ".part"
    t1 = time.time()
    if not parallel_range_download(info["url"], headers, part, on_progress, cancel):
        return None
    os.replace(part, path)
    if job:
        info["_cover_job"] = job
    return path, info, t_extract, time.time() - t1


def download_fallback(url, s, src_base, on_progress, cancel, max_seconds):
    """সাধারণ yt-dlp ডাউনলোড (ফলব্যাক)। yt-dlp আলাদা থ্রেডে চলে, অগ্রগতি মূল থ্রেডে দেখানো হয়।"""
    import yt_dlp
    state = {"frac": 0.0, "speed": 0.0, "eta": 0.0}

    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        _check_len(d.get("info_dict") or {}, max_seconds)
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                state["frac"] = min(done / total, 1.0)
            elif d.get("fragment_count"):
                state["frac"] = min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0)
            state["speed"] = d.get("speed") or 0
            state["eta"] = d.get("eta") or 0

    opts = ydl_base_opts(s, light=False)
    opts.update({
        "format": FMT_SMALL if s.get("ultra") else FMT_NORMAL,
        "outtmpl": src_base + ".%(ext)s",
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
        "noprogress": True,
    })
    box = {}

    def run():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                box["info"] = ydl.extract_info(url, download=True)
        except BaseException as e:   # noqa
            box["err"] = e

    th = threading.Thread(target=run, daemon=True)
    t0 = time.time()
    th.start()
    last = 0.0
    try:
        while th.is_alive():
            now = time.time()
            if now - last >= 0.3:
                last = now
                on_progress(state["frac"], state["speed"], state["eta"])
            time.sleep(0.1)
    except BaseException:
        cancel.set()
        raise
    th.join(timeout=5)
    if "err" in box:
        raise box["err"]
    info = box.get("info") or {}
    rd = info.get("requested_downloads") or []
    path = rd[0].get("filepath") if rd else ""
    if not path or not os.path.isfile(path):
        import glob
        found = sorted(glob.glob(glob.escape(src_base) + ".*"))
        found = [p for p in found if not p.endswith(".part")]
        path = found[0] if found else ""
    if not path:
        raise RuntimeError("ডাউনলোড হওয়া ফাইল পাওয়া যায়নি।")
    return path, info, None, time.time() - t0


# ---------------------------------------------------------------------
#  FFmpeg
# ---------------------------------------------------------------------
def run_ff_progress(cmd, duration, on_progress, cancel, err_path, cwd=None):
    """ffmpeg চালায় এবং -progress থেকে লাইভ অগ্রগতি (0..1) জানায়। রিটার্ন: রিটার্নকোড বা None (বাতিল)।"""
    last = 0.0
    with open(err_path, "wb") as ef:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL,
                             cwd=cwd, **_popen_flags())
        try:
            for raw in p.stdout:
                if cancel.is_set():
                    p.kill()
                    p.wait()
                    return None
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith(("out_time_us=", "out_time_ms=")) and duration > 0:
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    now = time.time()
                    if now - last >= 0.3:
                        last = now
                        on_progress(max(0.0, min(us / 1e6 / duration, 1.0)))
        finally:
            if p.poll() is None:   # এরর/বাতিলে প্রসেস ফেলে রাখা যাবে না
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                p.stdout.close()
            except Exception:
                pass
        p.wait()
    return p.returncode


def encode_parts(src, duration, enc_audio, tmpdir, on_progress, cancel):
    """লম্বা অডিওকে সমান টুকরোয় ভাগ করে সব CPU কোরে একসাথে MP3 বানায়।
    রিটার্ন: পার্ট ফাইলের তালিকা / [] (টার্বো দরকার নেই বা হয়নি)।"""
    cores = cpu_cores()
    nparts = min(cores, 16, int(duration // 60))   # প্রতি অংশে অন্তত ~১ মিনিট
    if nparts < 2:
        return []
    seg = duration / nparts
    procs, parts, prog, threads, errs = [], [], [0.0] * nparts, [], []

    def reader(idx, pipe, plen):
        for raw in pipe:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    prog[idx] = max(0.0, min(int(line.split("=", 1)[1]) / 1e6 / plen, 1.0))
                except ValueError:
                    pass

    def kill_all():
        for pr in procs:
            try:
                pr.kill()
            except Exception:
                pass
        for pr in procs:
            try:
                pr.wait()
            except Exception:
                pass

    try:
        for k in range(nparts):
            part = os.path.join(tmpdir, f"part{k:02d}.mp3")
            parts.append(part)
            cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-progress", "pipe:1", "-nostats", "-ss", f"{k * seg:.3f}"]
            plen = seg
            if k < nparts - 1:
                cmd += ["-t", f"{seg:.3f}"]
            else:
                plen = max(duration - k * seg, 1.0)
            cmd += ["-i", src, "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                    *enc_audio, "-write_xing", "0", part]
            ef = open(os.path.join(tmpdir, f"err{k}.txt"), "wb")
            errs.append(ef)
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL,
                                  **_popen_flags())
            procs.append(pr)
            t = threading.Thread(target=reader, args=(k, pr.stdout, plen), daemon=True)
            t.start()
            threads.append(t)

        last = 0.0
        while any(pr.poll() is None for pr in procs):
            if cancel.is_set():
                kill_all()
                raise Cancelled()
            now = time.time()
            if now - last >= 0.3:
                last = now
                on_progress(sum(prog) / nparts)
            time.sleep(0.1)
        for t in threads:
            t.join(timeout=2)
        if any(pr.returncode != 0 for pr in procs):
            return []
        for part in parts:
            if not os.path.isfile(part) or os.path.getsize(part) < 1024:
                return []
        on_progress(1.0)
        return parts
    except Cancelled:
        raise
    except BaseException:
        kill_all()
        raise
    finally:
        for ef in errs:
            try:
                ef.close()
            except Exception:
                pass


def audio_args(codec, q, ultra, src_ext):
    if codec == "mp3":
        qq = str(min(int(q), 96)) if ultra else q
        return ["-c:a", "libmp3lame", "-b:a", f"{qq}k", "-compression_level", "9"] + \
               (["-ac", "1", "-ar", "22050"] if ultra else ["-ar", "44100"])
    if codec == "m4a":
        if src_ext in (".m4a", ".mp4"):
            return ["-c:a", "copy"]   # কনভার্ট ছাড়াই - সবচেয়ে দ্রুত
        return ["-c:a", "aac", "-b:a", f"{q}k"]
    if codec == "opus":
        return ["-c:a", "libopus", "-b:a", f"{q}k"]
    if codec == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "pcm_s16le"]   # wav


def convert_audio(src, dst, info, cfg, on_progress, cancel, note=None):
    """ডাউনলোড হওয়া অডিও থেকে এক ধাপেই কনভার্ট + কভার + ট্যাগ।
    রিটার্ন: (অবস্থা, বার্তা) - অবস্থা: "ok" / "fail" / "cancel"।"""
    codec = cfg["codec"]
    note = note or (lambda m: None)
    t_start = time.time()
    if not src or not os.path.isfile(src):
        return "fail", "⚠ ডাউনলোড হওয়া ফাইল খুঁজে পাওয়া যায়নি।"

    use_cover = bool(cfg.get("cover")) and codec in ("mp3", "m4a")
    if cfg.get("smart") and codec in ("mp3", "m4a"):
        tags = smart_tags(info)
    elif cfg.get("meta", True):
        tags = {"title": info.get("title") or "", "artist": info.get("uploader") or ""}
    else:
        tags = {}
    if cfg.get("date"):
        tags["title"] = f"{cfg['date']} {tags.get('title') or info.get('title') or ''}".strip()
    tags = {k: (v or "")[:150] for k, v in tags.items()}

    try:
        tmpdir = tempfile.mkdtemp(prefix=".ytas_", dir=os.path.dirname(os.path.abspath(dst)))
    except Exception:
        tmpdir = tempfile.mkdtemp(prefix="ytas_")
    try:
        cover = None
        cover_err = ""
        if use_cover:
            try:
                im = fetch_thumbnail(info)
                if im is not None:
                    cover = make_cover_file(im, int(cfg.get("size") or 300), bool(cfg.get("square")), tmpdir)
            except Exception as ex:
                cover_err = f"কভার তৈরি ব্যর্থ: {ex}"
            if cover is None:
                cover_err = cover_err or "থাম্বনেল নামানো যায়নি"

        duration = float(info.get("duration") or 0)
        src_ext = os.path.splitext(src)[1].lower()
        q = str(cfg.get("quality")) if str(cfg.get("quality")).isdigit() else "192"
        ultra = bool(cfg.get("ultra"))
        enc_audio = audio_args(codec, q, ultra, src_ext)
        tmp_out = os.path.join(tmpdir, "out" + os.path.splitext(dst)[1])
        err_path = os.path.join(tmpdir, "err.txt")

        if codec == "mp3":
            note(f"🔧 কনভার্ট শুরু: সার্ভারের CPU কোর {cpu_cores()}টি, অডিও {fmt_time(duration)}, "
                 f"টার্বো {'চালু' if cfg.get('turbo') else 'বন্ধ'}{', আল্ট্রা (মোনো)' if ultra else ''}")

        def build(with_tags, src_path):
            cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-progress", "pipe:1", "-nostats", "-i", src_path]
            if cover:
                cmd += ["-i", cover, "-map", "0:a:0", "-map", "1:v"]
            else:
                cmd += ["-map", "0:a:0"]
            cmd += ["-map_metadata", "-1"] + enc_audio
            if cover:
                cmd += ["-c:v", "copy", "-metadata:s:v", "title=Album cover",
                        "-metadata:s:v", "comment=Cover (front)"]
                if codec != "mp3":
                    cmd += ["-disposition:v:0", "attached_pic"]
            if codec == "mp3":
                cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
            if with_tags:
                for k, v in tags.items():
                    if v:
                        cmd += ["-metadata", f"{k}={v}"]
            return cmd + [tmp_out]

        done = False
        turbo_n = 0
        # ---- টার্বো: সমান্তরাল এনকোড (শুধু MP3, লম্বা অডিওতে) ----
        if codec == "mp3" and cfg.get("turbo") and duration >= 150:
            parts = encode_parts(src, duration, enc_audio, tmpdir, lambda f: on_progress(f * 0.92), cancel)
            if parts:
                with open(os.path.join(tmpdir, "list.txt"), "w", encoding="utf-8") as lf:
                    for pt in parts:
                        lf.write("file '%s'\n" % os.path.basename(pt))
                ccmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-progress", "pipe:1", "-nostats", "-f", "concat", "-safe", "0", "-i", "list.txt"]
                if cover:
                    ccmd += ["-i", cover, "-map", "0:a", "-map", "1:v"]
                else:
                    ccmd += ["-map", "0:a"]
                ccmd += ["-map_metadata", "-1", "-c", "copy", "-id3v2_version", "3", "-write_id3v1", "1"]
                if cover:
                    ccmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
                for k_, v_ in tags.items():
                    if v_:
                        ccmd += ["-metadata", f"{k_}={v_}"]
                ccmd += [tmp_out]
                rc = run_ff_progress(ccmd, duration, lambda f: on_progress(0.92 + f * 0.08),
                                     cancel, err_path, cwd=tmpdir)
                if rc is None:
                    return "cancel", ""
                if rc == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
                    done = True
                    turbo_n = len(parts)
                for pt in parts:
                    try:
                        os.remove(pt)
                    except OSError:
                        pass
            if not done:
                note("ℹ টার্বো ব্যবহার হয়নি (১ কোর / ছোট অডিও / ব্যর্থ) - একক পদ্ধতিতে কনভার্ট হচ্ছে")

        # ---- একক পদ্ধতি (+ ফলব্যাক) ----
        rc = -1
        for attempt in (range(2) if not done else []):
            # ১: ট্যাগসহ  ২: ট্যাগ ছাড়া
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            rc = run_ff_progress(build(attempt == 0, src), duration, on_progress, cancel, err_path)
            if rc is None:
                return "cancel", ""
            if rc == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
                done = True
                break
        if not done:
            try:
                with open(err_path, "rb") as f:
                    err = f.read().decode("utf-8", "replace").strip().splitlines()
            except Exception:
                err = []
            return "fail", "⚠ কনভার্ট ব্যর্থ: " + (err[-1][:160] if err else "অজানা ত্রুটি")

        el = time.time() - t_start
        if duration > 0 and el > 0:
            note(f"⚙ এনকোড গতি: {duration / el:.0f}× রিয়েলটাইম ({fmt_time(el)} এ {fmt_time(duration)} অডিও)")
        verified = has_cover(tmp_out) if cover else True
        kb = id3_size(tmp_out) / 1024
        os.replace(tmp_out, dst)   # একই ড্রাইভে (টেম্প ফোল্ডার আউটপুটের পাশেই)

        if cover:
            if verified:
                extra = f", ট্যাগ {kb:.0f} KB" if codec == "mp3" else ""
                turbo_txt = f" • টার্বো ×{turbo_n} কোর" if turbo_n else ""
                return "ok", f"🖼 কভার আর্ট বসেছে ({cfg.get('size')}×{cfg.get('size')}{extra}{turbo_txt}) ✔"
            return "ok", "⚠ কভার বসানো হয়েছিল, কিন্তু যাচাইয়ে ধরা পড়েনি।"
        if use_cover and cover_err:
            return "ok", f"⚠ {cover_err} - কভার ছাড়াই সেভ হয়েছে।"
        return "ok", "✔ কনভার্ট সম্পন্ন" + (f" • টার্বো ×{turbo_n} কোর" if turbo_n else "")
    except Cancelled:
        return "cancel", ""
    except Exception as e:
        return "fail", f"⚠ কনভার্ট ধাপে সমস্যা: {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------
#  একটি লিংকের সম্পূর্ণ কাজ
# ---------------------------------------------------------------------
def process_url(url, s, workdir, idx, on_update, cancel):
    """একটি লিংক: তথ্য → ডাউনলোড → কনভার্ট। on_update(মোট_অগ্রগতি 0..1, লেখা) দিয়ে অবস্থা জানায়।
    সফল হলে ফলাফলের ডিকশনারি ফেরত দেয়; ব্যর্থ হলে Exception ছোঁড়ে।"""
    codec = s["codec"]
    max_seconds = int(s.get("max_minutes") or 0) * 60
    dl_share = 0.6 if codec == "mp3" else 0.9
    t_item = time.time()
    src_base = os.path.join(workdir, f"src{idx}")
    dst = os.path.join(workdir, f"out{idx}.{codec}")
    notes = []

    def dl_cb(frac, speed, eta):
        on_update(frac * dl_share, f"⚡ ডাউনলোড {frac * 100:.0f}%  •  {speed / 1048576:.1f} MB/s  •  বাকি {fmt_time(eta)}")

    on_update(0.0, "তথ্য আনা হচ্ছে...")
    res = None
    try:
        res = fast_fetch(url, s, src_base, dl_cb, cancel, bool(s.get("cover")) and codec in ("mp3", "m4a"), max_seconds)
        if res is None:
            notes.append("ℹ সমান্তরাল ডাউনলোড সম্ভব হয়নি, সাধারণ পদ্ধতিতে নামানো হয়েছে।")
    except (Cancelled, TooLong):
        raise
    except Exception as ex:
        notes.append(f"ℹ দ্রুত ডাউনলোড হয়নি ({str(ex)[:90]}), সাধারণ পদ্ধতিতে চেষ্টা হচ্ছে।")
        res = None
    try:
        if res is None:
            res = download_fallback(url, s, src_base, dl_cb, cancel, max_seconds)
        src_path, info, t_ext, t_dl = res

        def conv_cb(frac):
            el = time.time() - t_conv0
            eta = f"  •  বাকি ~{fmt_time(el * (1 - frac) / frac)}" if frac > 0.04 else ""
            on_update(dl_share + frac * (1 - dl_share),
                      f"🎛 {codec.upper()}-তে কনভার্ট {frac * 100:.0f}%  •  কনভার্টে গেছে {fmt_time(el)}{eta}")

        on_update(dl_share, "ডাউনলোড শেষ, কনভার্ট শুরু হচ্ছে...")
        t_conv0 = time.time()
        st, msg = convert_audio(src_path, dst, info, s, conv_cb, cancel, notes.append)
        t_conv = time.time() - t_conv0
        if st == "cancel":
            raise Cancelled()
        if st == "fail":
            raise RuntimeError(msg)
        notes.append(msg)
        on_update(1.0, "সম্পন্ন")
    finally:
        for fn in os.listdir(workdir):   # মূল স্ট্রিম ফাইল ও অবশিষ্টাংশ মুছে সার্ভারের জায়গা বাঁচানো
            if fn.startswith(f"src{idx}"):
                try:
                    os.remove(os.path.join(workdir, fn))
                except OSError:
                    pass

    title = info.get("title") or "audio"
    date = s.get("date") or ""
    disp = safe_filename((date + " " if date else "") + title)
    return {
        "path": dst, "name": f"{disp}.{codec}", "title": title, "duration": info.get("duration") or 0,
        "size": os.path.getsize(dst), "codec": codec, "notes": notes,
        "t_extract": t_ext, "t_download": t_dl, "t_convert": t_conv, "t_total": time.time() - t_item,
        "uploader": info.get("uploader") or info.get("channel") or "",
    }
