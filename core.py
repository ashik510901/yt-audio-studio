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
# HLS (m3u8): YouTube-এর যেসব ক্লায়েন্টে সাধারণ https লিংকে 403/SABR আসে, সেখানে HLS প্রায়ই PO Token ছাড়াই চলে।
# অডিও-অনলি HLS থাকলে সেটা, না থাকলে সবচেয়ে ছোট (144p) মিশ্র ফরম্যাট নামিয়ে শুধু অডিও নেওয়া হয়।
FMT_HLS = "bestaudio[protocol*=m3u8]/best[protocol*=m3u8]"
HLS_SORT = ["+res", "+size"]
MIN_YTDLP = (2026, 8, 19)   # এর আগের yt-dlp-তে YouTube 403 (android_vr/SABR) বাগ ছিল
LAST_GOOD = {}              # সর্বশেষ যে পদ্ধতি কাজ করেছে (পরেরবার সেটা আগে চেষ্টা হবে)


class Cancelled(Exception):
    """ব্যবহারকারী কাজ বাতিল করেছে।"""


class TooLong(Exception):
    """ভিডিও সীমার চেয়ে লম্বা (ফ্রি সার্ভারের সুরক্ষা)।"""


class HttpBlocked(Exception):
    """ডাউনলোড লিংকে YouTube 403/401/410 দিয়েছে (PO Token / SABR / IP ব্লক)।"""


class AllFailed(Exception):
    """সবগুলো ডাউনলোড পদ্ধতি ব্যর্থ। .attempts = [(পদ্ধতি, কারণ), ...]"""
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("সবগুলো ডাউনলোড পদ্ধতি ব্যর্থ: " + "; ".join(f"{n}: {r}" for n, r in attempts))


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


def detect_mode():
    """"local" (নিজের Windows/Mac পিসি) / "cloud" (Streamlit Community Cloud) / "server" (অন্য লিনাক্স সার্ভার)।
    জোর করে ঠিক করতে পরিবেশ-চলক APP_MODE=local|cloud ব্যবহার করুন।"""
    forced = os.environ.get("APP_MODE", "").strip().lower()
    if forced in ("local", "cloud", "server"):
        return forced
    if os.path.isdir("/mount/src") or os.environ.get("STREAMLIT_RUNTIME_ENV", "") == "cloud":
        return "cloud"
    return "local" if sys.platform.startswith(("win", "darwin")) else "server"


def ytdlp_version_ok():
    """yt-dlp কি MIN_YTDLP (2026.08.19)-এর সমান বা নতুন? ভার্সন না পড়া গেলে None।"""
    v = ytdlp_version()
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", v or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups()) >= MIN_YTDLP


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
    """yt-dlp-র YouTube চ্যালেঞ্জের জন্য JavaScript রানটাইম (Deno) লাগে। না থাকলে GitHub থেকে নামিয়ে নেয়।
    (Linux / Windows / macOS)। সফল হলে deno-র পাথ, না হলে None।"""
    try:
        exe = shutil.which("deno")
        if exe:
            return exe
        arch = platform.machine().lower()
        arm = arch in ("aarch64", "arm64")
        if sys.platform.startswith("linux"):
            target, exe_name = ("aarch64-unknown-linux-gnu" if arm else "x86_64-unknown-linux-gnu"), "deno"
        elif sys.platform == "win32":
            target, exe_name = ("aarch64-pc-windows-msvc" if arm else "x86_64-pc-windows-msvc"), "deno.exe"
        elif sys.platform == "darwin":
            target, exe_name = ("aarch64-apple-darwin" if arm else "x86_64-apple-darwin"), "deno"
        else:
            return None
        d = os.path.join(os.path.expanduser("~"), ".cache", "ytas_deno")
        exe = os.path.join(d, exe_name)
        if not os.path.isfile(exe):
            os.makedirs(d, exist_ok=True)
            url = f"https://github.com/denoland/deno/releases/latest/download/deno-{target}.zip"
            zp = os.path.join(d, "deno.zip")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r, open(zp, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(zp) as z:
                z.extract(exe_name, d)
            os.remove(zp)
            if exe_name == "deno":
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
def ydl_base_opts(s, clients=None, use_cookies=True):
    """yt-dlp-র সাধারণ অপশন। ধ্যান দিন: আগে এখানে HLS/DASH বাদ দেওয়া হতো, যা ২০২৬-এর YouTube-এ ক্ষতিকর
    (HLS-ই PO Token ছাড়া চলা একমাত্র পথ হতে পারে), তাই এখন কিছুই বাদ দেওয়া হয় না (শুধু অনুবাদ-সাবটাইটেল ছাড়া)।"""
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "retries": 8, "fragment_retries": 8, "socket_timeout": 30,
    }
    ea = {"skip": ["translated_subs"]}
    cl = clients if clients is not None else [c.strip() for c in (s.get("clients") or []) if c and c.strip()]
    if cl:
        ea["player_client"] = list(cl)
    o["extractor_args"] = {"youtube": ea}
    if use_cookies:
        if s.get("cookies"):
            o["cookiefile"] = s["cookies"]
        elif s.get("browser"):
            o["cookiesfrombrowser"] = (s["browser"],)   # লোকাল মোড: ব্রাউজারের লগইন সেশন (Firefox সবচেয়ে নির্ভরযোগ্য)
    if s.get("proxy"):
        o["proxy"] = s["proxy"]
    return o


def build_strategies(s):
    """ডাউনলোড পদ্ধতির তালিকা (একটা ব্যর্থ হলে পরেরটা)। সর্বশেষ কাজ করা পদ্ধতি সবার আগে।
    kind: "audio" = অডিও-অনলি https স্ট্রিম, "hls" = HLS (PO Token লাগে না এমন পথ)।"""
    custom = [c.strip() for c in (s.get("clients") or []) if c and c.strip()]
    st = []
    if custom:
        st.append({"name": "custom:" + ",".join(custom), "clients": custom, "kind": "audio", "cookies": True})
    st += [
        {"name": "default", "clients": [], "kind": "audio", "cookies": True},
        {"name": "android", "clients": ["android"], "kind": "audio", "cookies": False},      # এই ক্লায়েন্ট কুকি নেয় না
        {"name": "tv", "clients": ["tv"], "kind": "audio", "cookies": True},
        {"name": "web_embedded", "clients": ["web_embedded"], "kind": "audio", "cookies": True},
        {"name": "hls", "clients": ["web_safari"], "kind": "hls", "cookies": True},
        {"name": "hls-mweb", "clients": ["mweb"], "kind": "hls", "cookies": True},
    ]
    good = LAST_GOOD.get("name")
    st.sort(key=lambda x: 0 if x["name"] == good else 1)
    return st


def strategy_opts(s, strat):
    o = ydl_base_opts(s, clients=strat["clients"], use_cookies=strat.get("cookies", True))
    if strat["kind"] == "hls":
        o["format"] = FMT_HLS
        o["format_sort"] = list(HLS_SORT)
    else:
        o["format"] = FMT_SMALL if s.get("ultra") else FMT_NORMAL
    return o


def get_info(url, s):
    """লিংকের শিরোনাম, সময়, থাম্বনেল ইত্যাদি (ডাউনলোড ছাড়া)।"""
    import yt_dlp
    opts = ydl_base_opts(s)
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
def parallel_range_download(url, headers, part_path, on_progress, cancel, workers=6):
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
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 410, 429):
            raise HttpBlocked(f"HTTP {e.code}")   # ব্লক: এই পদ্ধতি বাদ, পরেরটা চেষ্টা হবে
        return False
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
                    if isinstance(ex, urllib.error.HTTPError) and ex.code in (401, 403, 410, 429):
                        err[0] = ex          # ব্লক হলে রিট্রাই করে লাভ নেই
                        return
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
        if isinstance(err[0], urllib.error.HTTPError) and err[0].code in (401, 403, 410, 429):
            raise HttpBlocked(f"HTTP {err[0].code}")
        return False
    on_progress(1.0, total / max(time.time() - t0, 0.001), 0)
    return True


def _check_len(info, max_seconds):
    d = info.get("duration") or 0
    if max_seconds and d and d > max_seconds:
        raise TooLong(f"ভিডিওটি {fmt_time(d)} লম্বা; ফ্রি সার্ভারে সর্বোচ্চ {fmt_time(max_seconds)} সমর্থিত।")


def fast_fetch(url, s, strat, src_base, on_progress, cancel, want_cover, max_seconds):
    """দ্রুত পথ: তথ্য আনা + সমান্তরাল ডাউনলোড। সফল হলে (পাথ, info, তথ্য-সময়, ডাউনলোড-সময়),
    https নয়/সমর্থিত নয় হলে None; লিংকে 403 হলে HttpBlocked।"""
    import yt_dlp
    t0 = time.time()
    opts = strategy_opts(s, strat)
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


def download_fallback(url, s, strat, src_base, on_progress, cancel, max_seconds):
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

    opts = strategy_opts(s, strat)
    opts.update({
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
    strategies = build_strategies(s)
    attempts = []
    res = None
    used = None
    want_cover = bool(s.get("cover")) and codec in ("mp3", "m4a")
    for si, strat in enumerate(strategies):
        if cancel.is_set():
            raise Cancelled()
        name = strat["name"]
        on_update(0.0, f"তথ্য আনা হচ্ছে... (পদ্ধতি {si + 1}/{len(strategies)}: {name})")
        try:
            r = None
            if strat["kind"] != "hls":
                r = fast_fetch(url, s, strat, src_base, dl_cb, cancel, want_cover, max_seconds)
            if r is None:
                r = download_fallback(url, s, strat, src_base, dl_cb, cancel, max_seconds)
            res, used = r, strat
            break
        except (Cancelled, TooLong):
            raise
        except HttpBlocked as e:
            attempts.append((name, f"{e} - ডাউনলোড লিংক ব্লক (PO Token/SABR/IP)"))
        except Exception as e:   # noqa
            attempts.append((name, str(e).replace("ERROR: ", "").strip().splitlines()[0][:150] if str(e).strip() else "অজানা ত্রুটি"))
        finally:
            if res is None:   # ব্যর্থ পদ্ধতির আংশিক ফাইল মুছে ফেলা
                for fn in os.listdir(workdir):
                    if fn.startswith(f"src{idx}"):
                        try:
                            os.remove(os.path.join(workdir, fn))
                        except OSError:
                            pass
    if res is None:
        raise AllFailed(attempts)
    LAST_GOOD["name"] = used["name"]
    if attempts:
        notes.append("ℹ আগের পদ্ধতি ব্যর্থ: " + " | ".join(f"{n}" for n, _ in attempts))
    notes.append(f"ℹ ডাউনলোড পদ্ধতি: {used['name']}" + (" (HLS থেকে শুধু অডিও নেওয়া হয়েছে)" if used["kind"] == "hls" else ""))
    try:
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


# ---------------------------------------------------------------------
#  ডায়াগনস্টিক: ডাউনলোড কেন ব্যর্থ হচ্ছে তা ধাপে ধাপে পরীক্ষা (কিছু নামায় না)
# ---------------------------------------------------------------------
_RELEVANT = re.compile(
    r"JS runtimes|JS Challenge|jsc|PO Token|SABR|Detected experiment|requires a GVS|skipped|Skipping|"
    r"DRM|Sign in|not a bot|n-sig|nsig|player API|Downloading m3u8|unavailable", re.I)
_DC_WORDS = ("google", "amazon", "aws", "microsoft", "azure", "digitalocean", "hetzner", "ovh", "linode",
             "akamai", "oracle", "vultr", "contabo", "leaseweb", "cloud", "hosting", "datacenter", "server")


class _Cap:
    """yt-dlp-র লগ থেকে শুধু কাজের লাইনগুলো ধরে রাখে।"""
    def __init__(self):
        self.lines = []

    def _add(self, m):
        m = str(m).strip()
        if m and _RELEVANT.search(m) and m not in self.lines and len(self.lines) < 40:
            self.lines.append(m[:300])

    debug = info = warning = error = _add


def _probe_http(url, headers):
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, str(e.reason)
    except Exception as e:   # noqa
        return None, str(e)[:100]


def _probe_hls(url, headers):
    from urllib.parse import urljoin
    cur = url
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(cur, headers=headers), timeout=20) as r:
                body = r.read(300000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, "প্লেলিস্ট"
        except Exception as e:   # noqa
            return None, str(e)[:100]
        lines = [x.strip() for x in body.splitlines() if x.strip() and not x.startswith("#")]
        if not lines:
            return None, "খালি প্লেলিস্ট"
        nxt = urljoin(cur, lines[0])
        if nxt.split("?")[0].endswith(".m3u8"):
            cur = nxt
            continue
        return _probe_http(nxt, headers)
    return None, "প্লেলিস্ট খুব গভীর"


def diagnose(url, s):
    """প্রতিটি পদ্ধতিতে: তথ্য আনা ✔/✖ → কোন ফরম্যাট → ডাউনলোড লিংকে প্রথম বাইট চেয়ে দেখা (HTTP স্ট্যাটাস)।
    কিছু ডাউনলোড/কনভার্ট করে না। রিটার্ন: ডিকশনারি (env, ip, attempts, verdict, log)।"""
    import yt_dlp
    rep = {"env": {}, "ip": {}, "attempts": [], "verdict": "", "advice": [], "log": []}

    deno = shutil.which("deno")
    deno_v = ""
    if deno:
        try:
            deno_v = subprocess.run([deno, "--version"], capture_output=True, text=True, timeout=20).stdout.splitlines()[0]
        except Exception:
            deno_v = "চালানো যায়নি"
    try:
        import yt_dlp_ejs
        ejs = getattr(yt_dlp_ejs, "__version__", "আছে")
    except Exception:
        ejs = "নেই (pip install 'yt-dlp[default]')"
    rep["env"] = {
        "মোড": detect_mode(), "yt-dlp": ytdlp_version(),
        f"yt-dlp ≥ {'.'.join(map(str, MIN_YTDLP))}": {True: "হ্যাঁ", False: "না - আপডেট করুন", None: "?"}[ytdlp_version_ok()],
        "yt-dlp-ejs": ejs, "Deno": f"{deno_v} ({deno})" if deno else "নেই",
        "FFmpeg": "আছে" if ffmpeg_ok() else "নেই", "CPU কোর": cpu_cores(),
        "কুকি ফাইল": "আছে" if s.get("cookies") else "নেই", "ব্রাউজার-কুকি": s.get("browser") or "নেই",
        "প্রক্সি": "আছে" if s.get("proxy") else "নেই",
    }
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=8) as r:
            import json as _json
            j = _json.loads(r.read().decode("utf-8", "replace"))
        org = j.get("org", "")
        rep["ip"] = {"org": org, "country": j.get("country", ""),
                     "datacenter": any(w in org.lower() for w in _DC_WORDS)}
    except Exception:
        rep["ip"] = {}

    seen = []
    for strat in build_strategies(s):
        cap = _Cap()
        row = {"পদ্ধতি": strat["name"], "তথ্য": "", "ফরম্যাট": "", "প্রোটোকল": "", "লিংক-পরীক্ষা": "", "মন্তব্য": ""}
        opts = strategy_opts(s, strat)
        opts.update({"skip_download": True, "verbose": True, "logger": cap})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            row["তথ্য"] = "✔"
            fmt = (info.get("requested_formats") or [info])[0]
            row["ফরম্যাট"] = f"{fmt.get('format_id')} {fmt.get('ext')}"
            proto = str(fmt.get("protocol") or "")
            row["প্রোটোকল"] = proto
            furl = fmt.get("url")
            if not furl:
                row["লিংক-পরীক্ষা"] = "—"
                row["মন্তব্য"] = "ফরম্যাটে URL নেই (SABR বা অনুপলব্ধ)"
            else:
                headers = dict(fmt.get("http_headers") or info.get("http_headers") or {})
                code, why = _probe_hls(furl, headers) if "m3u8" in proto else _probe_http(furl, headers)
                row["লিংক-পরীক্ষা"] = str(code) if code else "ত্রুটি"
                row["মন্তব্য"] = "ডাউনলোড সম্ভব" if code in (200, 206) else (why or "")
        except Exception as e:   # noqa
            row["তথ্য"] = "✖"
            row["মন্তব্য"] = (str(e).replace("ERROR: ", "").strip().splitlines() or ["অজানা"])[0][:160]
        for ln in cap.lines:
            if ln not in seen and len(seen) < 40:
                seen.append(ln)
        rep["attempts"].append(row)
    rep["log"] = seen

    ok_rows = [r for r in rep["attempts"] if r["লিংক-পরীক্ষা"] in ("200", "206")]
    extr_rows = [r for r in rep["attempts"] if r["তথ্য"] == "✔"]
    errs = " ".join(r["মন্তব্য"] for r in rep["attempts"]).lower()
    dc = rep["ip"].get("datacenter")
    if ok_rows:
        rep["verdict"] = f"✔ ডাউনলোড সম্ভব। কাজ করা পদ্ধতি: {', '.join(r['পদ্ধতি'] for r in ok_rows)}"
    elif "sign in" in errs or "not a bot" in errs:
        rep["verdict"] = "⛔ YouTube বট-চেক দিচ্ছে (Sign in to confirm you're not a bot) - প্রধানত IP-এর কারণে"
        rep["advice"] = ["কুকি দিন (Secrets-এ YT_COOKIES, বা লোকাল মোডে ব্রাউজার-কুকি)।",
                         "নিজের বাসার IP থেকে (লোকাল মোডে) চালিয়ে দেখুন।"]
    elif extr_rows:
        rep["verdict"] = ("⛔ ফরম্যাট লিংক মিলেছে, কিন্তু সব পদ্ধতিতে ডাউনলোডে 403/ত্রুটি - "
                          + ("এই সার্ভারের IP ডেটাসেন্টারের (ব্লক হওয়ার সম্ভাবনা বেশি)" if dc
                             else "PO Token / SABR / IP ব্লকের লক্ষণ"))
        rep["advice"] = [
            "yt-dlp-কে সর্বশেষ সংস্করণে আপডেট করুন (Help → yt-dlp আপডেট → Reboot)।",
            "কুকি দিন (Secrets-এ YT_COOKIES, বা লোকাল মোডে ব্রাউজার-কুকি; Firefox সবচেয়ে নির্ভরযোগ্য)।",
            "একই ভিডিও নিজের কম্পিউটারে (লোকাল মোড) চালিয়ে দেখুন: সেখানে চললে সমস্যা ক্লাউড IP-এর।",
            "ফ্রি ক্লাউডে PO Token প্রোভাইডার চালানো বাস্তবসম্মত নয়; স্থায়ী সমাধান বাসার IP বা পেইড প্রক্সি।",
        ]
    else:
        rep["verdict"] = "⛔ কোনো পদ্ধতিতেই ভিডিওর তথ্য আনা যায়নি (লিংক ঠিক আছে কি? ভিডিও প্রাইভেট/ব্লকড?)"
        rep["advice"] = ["লিংক ব্রাউজারে খুলে দেখুন; yt-dlp আপডেট করুন।"]
    if not ytdlp_version_ok() and ytdlp_version_ok() is not None:
        rep["advice"].insert(0, f"আপনার yt-dlp পুরনো ({ytdlp_version()}); {'.'.join(map(str, MIN_YTDLP))} বা নতুন লাগবে।")
    if not deno:
        rep["advice"].append("Deno (JavaScript রানটাইম) পাওয়া যায়নি - অ্যাপ রিস্টার্ট করে আবার চেষ্টা করুন।")
    return rep


def format_report(rep):
    """diagnose()-এর ফলাফল সাধারণ লেখায় (টার্মিনাল / কপি করার জন্য)।"""
    out = ["=== পরিবেশ ==="]
    out += [f"  {k}: {v}" for k, v in rep["env"].items()]
    if rep.get("ip"):
        out.append(f"  সার্ভারের নেটওয়ার্ক: {rep['ip'].get('org')} ({rep['ip'].get('country')})"
                   + ("  ← ডেটাসেন্টার" if rep["ip"].get("datacenter") else ""))
    out += ["", "=== পদ্ধতি অনুযায়ী ফলাফল ==="]
    for r in rep["attempts"]:
        out.append(f"  [{r['পদ্ধতি']}] তথ্য={r['তথ্য']} ফরম্যাট={r['ফরম্যাট']} প্রোটো={r['প্রোটোকল']} "
                   f"লিংক={r['লিংক-পরীক্ষা']} | {r['মন্তব্য']}")
    if rep["log"]:
        out += ["", "=== yt-dlp-র গুরুত্বপূর্ণ বার্তা ==="] + [f"  {x}" for x in rep["log"]]
    out += ["", "=== সিদ্ধান্ত ===", "  " + rep["verdict"]]
    out += [f"  → {a}" for a in rep["advice"]]
    return "\n".join(out)
