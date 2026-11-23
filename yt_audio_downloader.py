"""
YT Audio Studio
ইউটিউব ভিডিও লিংক থেকে সরাসরি অডিও (MP3 / M4A / OPUS / FLAC / WAV) + কভার আর্ট।
ভিডিও ডাউনলোড হয় না - শুধু অডিও স্ট্রিম নামে, তারপর কনভার্ট হয়।

চালাতে: python yt_audio_downloader.py
(প্রথমবার দরকারি প্যাকেজ নিজে নিজেই ইনস্টল হবে, ইন্টারনেট লাগবে)
"""

import os
import sys
import time
import json
import queue
import shutil
import threading
import traceback
import subprocess
import importlib

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CRASH_LOG = os.path.join(APP_DIR, "crash_log.txt")
DATA_DIR = os.path.join(os.path.expanduser("~"), ".yt_audio_studio")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")


# =====================================================================
#  স্টার্টআপ: দরকারি প্যাকেজ নিজে ইনস্টল + ক্র্যাশ হলে এরর দেখানো
# =====================================================================
def _pause():
    try:
        input("\nEnter চাপুন বন্ধ করতে...")
    except Exception:
        pass


def show_fatal(text):
    """ক্র্যাশ হলে লগ ফাইলে লিখে + বার্তা দেখায়, যাতে উইন্ডো চুপচাপ বন্ধ না হয়।"""
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + text + "\n\n")
    except Exception:
        pass
    print(text)
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        r = _tk.Tk()
        r.withdraw()
        _mb.showerror("YT Audio Studio - সমস্যা হয়েছে",
                      "প্রোগ্রাম চালু হতে পারেনি।\n\n" + text[-1200:] +
                      f"\n\nপুরো এরর এখানে সেভ হয়েছে:\n{CRASH_LOG}")
        r.destroy()
    except Exception:
        _pause()


def ensure_package(pip_name, import_name):
    try:
        return importlib.import_module(import_name)
    except ImportError:
        print(f"[সেটআপ] '{pip_name}' ইনস্টল হচ্ছে... একটু অপেক্ষা করুন")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", pip_name])
        importlib.invalidate_caches()
        return importlib.import_module(import_name)


try:
    ensure_package("yt-dlp", "yt_dlp")
    ensure_package("customtkinter", "customtkinter")
    ensure_package("pillow", "PIL")
except Exception:
    show_fatal("দরকারি প্যাকেজ ইনস্টল করা যায়নি। ইন্টারনেট চেক করুন, অথবা CMD-তে চালান:\n"
               "pip install -U yt-dlp customtkinter pillow\n\n" + traceback.format_exc())
    sys.exit(1)

# FFmpeg না থাকলে নিজে থেকে একটা বান্ডেল্ড FFmpeg আনা হবে
FFMPEG_PATH = None  # None = সিস্টেমের ffmpeg ব্যবহার হবে
if not shutil.which("ffmpeg"):
    try:
        _iio = ensure_package("imageio-ffmpeg", "imageio_ffmpeg")
        FFMPEG_PATH = _iio.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = False  # পাওয়া যায়নি

import io
import re
import tempfile
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
import yt_dlp
from yt_dlp.utils import DownloadCancelled

# =====================================================================
#  থিম
# =====================================================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C_BG = "#0e1014"
C_SIDEBAR = "#0a0b0f"
C_CARD = "#161920"
C_CARD2 = "#1d212b"
C_HOVER = "#272c39"
C_ACCENT = "#7c5cff"
C_ACCENT_H = "#6a49f0"
C_TEXT_DIM = "#8b93a7"
C_OK = "#2ecc71"
C_ERR = "#ff5d5d"
C_WARN = "#f5a623"

FORMATS = {
    "MP3": ("mp3", ["320", "256", "192", "128", "96"], "192", True),
    "M4A - সবচেয়ে দ্রুত (কনভার্ট ছাড়া)": ("m4a", ["Original"], "Original", True),
    "OPUS": ("opus", ["160", "128", "96"], "128", True),
    "FLAC (Lossless)": ("flac", ["Lossless"], "Lossless", True),
    "WAV (Lossless)": ("wav", ["Lossless"], "Lossless", False),
}

COVER_SIZES = {
    "300×300 - বাটন ফোন / সব ডিভাইস (সেরা সামঞ্জস্য)": 300,
    "500×500 - স্ট্যান্ডার্ড": 500,
    "800×800 - হাই কোয়ালিটি (শুধু আধুনিক প্লেয়ার)": 800,
}

SQUARE_CROP = ["-c:v", "mjpeg", "-q:v", "2", "-vf",
               "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"]


def fmt_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# =====================================================================
#  স্মার্ট ট্যাগ + ফোন-সামঞ্জস্যপূর্ণ কভার আর্ট
# =====================================================================
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


def _ff():
    return FFMPEG_PATH if FFMPEG_PATH else "ffmpeg"


def _run_ff(args):
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000  # কনসোল উইন্ডো দেখাবে না
    return subprocess.run(args, capture_output=True, **kw)


def _fetch_thumbnail(info):
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


def _make_cover_file(im, size, square, folder):
    """ছবিকে বেসলাইন JPEG (প্রগ্রেসিভ নয়), 4:2:0, ছোট সাইজে বানায় - বাটন ফোনও পড়তে পারে।"""
    im = im.convert("RGB")
    w, h = im.size
    # ইউটিউবের থাম্বনেলের কালো পট্টি (letterbox) কেটে ফেলা
    try:
        mask = im.convert("L").point(lambda v: 255 if v > 18 else 0)
        bb = mask.getbbox()
        if bb and (bb[3] - bb[1]) < h * 0.96 and (bb[3] - bb[1]) > h * 0.4:
            im = im.crop((0, bb[1], w, bb[3]))
            w, h = im.size
    except Exception:
        pass
    if square:
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
        im = im.resize((size, size), Image.LANCZOS)
    else:
        # পুরো ছবি দেখানো: স্কয়ার ক্যানভাসে মাঝখানে বসানো, পেছনে ঝাপসা ব্যাকগ্রাউন্ড
        from PIL import ImageFilter, ImageEnhance
        bg_s = min(im.size)
        bg = im.crop(((im.size[0] - bg_s) // 2, (im.size[1] - bg_s) // 2,
                      (im.size[0] + bg_s) // 2, (im.size[1] + bg_s) // 2)).resize((size, size), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(size // 18))
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        fg = im.copy()
        fg.thumbnail((size, size), Image.LANCZOS)
        bg.paste(fg, ((size - fg.size[0]) // 2, (size - fg.size[1]) // 2))
        im = bg
    path = os.path.join(folder, "cover.jpg")
    im.save(path, "JPEG", quality=90, progressive=False, optimize=False, subsampling=2)
    return path


def _id3_size(path):
    try:
        with open(path, "rb") as f:
            h = f.read(10)
        if h[:3] != b"ID3":
            return 0
        return ((h[6] & 127) << 21) | ((h[7] & 127) << 14) | ((h[8] & 127) << 7) | (h[9] & 127)
    except Exception:
        return 0


def _has_cover(path):
    try:
        r = _run_ff([_ff(), "-hide_banner", "-i", path])
        err = (r.stderr or b"").decode("utf-8", "replace")
        return "Video:" in err
    except Exception:
        return False


def _parallel_range_download(url, headers, part_path, on_progress, cancel, workers=12):
    """একই ফাইল অনেকগুলো সংযোগে টুকরো টুকরো করে একসাথে নামায় (সাধারণ ডাউনলোডের চেয়ে বহুগুণ দ্রুত হতে পারে)।
    রিটার্ন: True (সফল) / False (হয়নি - তখন সাধারণ পদ্ধতিতে ফিরবে)। বাতিল হলে DownloadCancelled।"""
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
    nxt = [0]
    done = [0]
    err = [None]
    lock = threading.Lock()
    flock = threading.Lock()
    stop = threading.Event()

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
                stop.set()
                break
            now = time.time()
            if now - last >= 0.3:
                last = now
                d = max(0, min(done[0], total))
                sp = d / max(now - t0, 0.001)
                on_progress(d / total, sp, ((total - d) / sp) if sp > 0 else 0)
            time.sleep(0.1)
    finally:
        if cancel.is_set():
            stop.set()
        for t in threads:
            t.join(timeout=5)
        f.close()
    if cancel.is_set():
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise DownloadCancelled()
    if err[0] is not None or done[0] < total:
        try:
            os.remove(part_path)
        except OSError:
            pass
        return False
    on_progress(1.0, total / max(time.time() - t0, 0.001), 0)
    return True


def fast_fetch(url, fopts, on_progress, cancel, want_cover=False):
    """দ্রুত পথ: তথ্য আনা + সমান্তরাল ডাউনলোড। সফল হলে (পাথ, info, তথ্য-সময়, ডাউনলোড-সময়), নাহলে None।"""
    t0 = time.time()
    with yt_dlp.YoutubeDL(fopts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") == "playlist":
            return None
        if info.get("protocol") not in ("https", "http") or info.get("fragments") or not info.get("url"):
            return None
        path = ydl.prepare_filename(info)
    t_extract = time.time() - t0
    job = None
    if want_cover:
        base = dict(info)
        job = {"im": None}

        def _bg():
            try:
                job["im"] = _fetch_thumbnail(base)
            except Exception:
                job["im"] = None
        job["thread"] = threading.Thread(target=_bg, daemon=True)
        job["thread"].start()
    headers = dict(info.get("http_headers") or {})
    if info.get("cookies"):
        headers["Cookie"] = info["cookies"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part = path + ".part"
    t1 = time.time()
    if not _parallel_range_download(info["url"], headers, part, on_progress, cancel):
        return None
    os.replace(part, path)
    if job:
        info["_cover_job"] = job
    return path, info, t_extract, time.time() - t1


def _run_ff_progress(cmd, duration, on_progress, cancel, err_path, cwd=None):
    """ffmpeg চালায় এবং -progress থেকে লাইভ অগ্রগতি (0..1) জানায়। রিটার্ন: রিটার্নকোড বা None (বাতিল)।"""
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000
    last = 0.0
    with open(err_path, "wb") as ef:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL, cwd=cwd, **kw)
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
            try:
                p.stdout.close()
            except Exception:
                pass
        p.wait()
    return p.returncode


def _encode_parts(src, duration, enc_audio, tmpdir, on_progress, cancel):
    """লম্বা অডিওকে সমান টুকরোয় ভাগ করে সব CPU কোরে একসাথে MP3 বানায়।
    রিটার্ন: পার্ট ফাইলের তালিকা / [] (টার্বো দরকার নেই বা হয়নি) / "cancel"।"""
    cores = os.cpu_count() or 1
    nparts = min(cores, 16, int(duration // 60))   # প্রতি অংশে অন্তত ~১ মিনিট
    if nparts < 2:
        return []
    seg = duration / nparts
    flags = 0
    if sys.platform.startswith("win"):
        flags = 0x08000000   # কনসোল লুকানো (পূর্ণ শক্তিতে চলবে)
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
            cmd = [_ff(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
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
                                  creationflags=flags) if flags else \
                subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
            procs.append(pr)
            t = threading.Thread(target=reader, args=(k, pr.stdout, plen), daemon=True)
            t.start()
            threads.append(t)

        last = 0.0
        while any(pr.poll() is None for pr in procs):
            if cancel.is_set():
                kill_all()
                return "cancel"
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
    except Exception:
        kill_all()
        return []
    finally:
        for ef in errs:
            try:
                ef.close()
            except Exception:
                pass


def convert_audio(src, dst, info, cfg, on_progress, cancel, note=None):
    """ডাউনলোড হওয়া অডিও স্ট্রিম থেকে এক ধাপেই MP3/M4A + কভার + ট্যাগ বানায়।
    রিটার্ন: (অবস্থা, বার্তা) - অবস্থা: "ok" / "fail" / "cancel"।"""
    codec = cfg["codec"]
    note = note or (lambda m: None)
    t_start = time.time()
    if not src or not os.path.isfile(src):
        return "fail", "⚠ ডাউনলোড হওয়া ফাইল খুঁজে পাওয়া যায়নি।"

    if cfg["smart"]:
        tags = smart_tags(info)
    elif cfg["meta"]:
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
        if cfg["cover"]:
            try:
                im = _fetch_thumbnail(info)
                if im is not None:
                    cover = _make_cover_file(im, cfg["size"], cfg["square"], tmpdir)
            except Exception as ex:
                cover_err = f"কভার তৈরি ব্যর্থ: {ex}"
            if cover is None:
                cover_err = cover_err or "থাম্বনেল নামানো যায়নি"

        duration = float(info.get("duration") or 0)
        src_ext = os.path.splitext(src)[1].lower()
        q = cfg["quality"] if str(cfg["quality"]).isdigit() else "192"
        ultra = bool(cfg.get("ultra"))
        if ultra:
            q = str(min(int(q), 96))
        enc_audio = ["-c:a", "libmp3lame", "-b:a", f"{q}k", "-compression_level", "9"] + \
                    (["-ac", "1", "-ar", "22050"] if ultra else ["-ar", "44100"])

        # বাংলা/ইউনিকোড ফাইলনেম হলে আগেই ইংরেজি নামের লিংক বানানো - ffmpeg যেন প্রথমবারেই ঠিকমতো চলে
        work_src = src
        if not src.isascii():
            try:
                work_src = os.path.join(tmpdir, "in" + src_ext)
                try:
                    os.link(src, work_src)
                except OSError:
                    shutil.copyfile(src, work_src)
            except Exception:
                work_src = src
        if codec == "mp3":
            note(f"🔧 কনভার্ট শুরু: CPU কোর {os.cpu_count() or 1}টি, অডিও {fmt_time(duration)}, "
                 f"টার্বো {'চালু' if cfg.get('turbo') else 'বন্ধ'}{', আল্ট্রা (মোনো)' if ultra else ''}")
        tmp_out = os.path.join(tmpdir, "out" + os.path.splitext(dst)[1])
        err_path = os.path.join(tmpdir, "err.txt")

        def build(with_tags, src_path):
            cmd = [_ff(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-progress", "pipe:1", "-nostats", "-i", src_path]
            if cover:
                cmd += ["-i", cover, "-map", "0:a:0", "-map", "1:v"]
            else:
                cmd += ["-map", "0:a:0"]
            cmd += ["-map_metadata", "-1"]
            if codec == "mp3":
                cmd += enc_audio
            elif src_ext in (".m4a", ".mp4"):
                cmd += ["-c:a", "copy"]          # কনভার্ট ছাড়াই - সবচেয়ে দ্রুত
            else:
                cmd += ["-c:a", "aac", "-b:a", f"{q}k"]
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
        rc = -1
        turbo_n = 0
        if codec == "mp3" and cfg.get("turbo") and duration >= 150:
            parts = _encode_parts(work_src, duration, enc_audio, tmpdir, lambda f: on_progress(f * 0.92), cancel)
            if parts == "cancel":
                return "cancel", ""
            if parts:
                with open(os.path.join(tmpdir, "list.txt"), "w", encoding="utf-8") as lf:
                    for pt in parts:
                        lf.write("file '%s'\n" % os.path.basename(pt))
                ccmd = [_ff(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-progress", "pipe:1", "-nostats",
                        "-f", "concat", "-safe", "0", "-i", "list.txt"]
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
                rc = _run_ff_progress(ccmd, duration, lambda f: on_progress(0.92 + f * 0.08),
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
        if codec == "mp3" and not done:
            note("ℹ টার্বো ব্যবহার হয়নি (১ কোর / বন্ধ / ছোট অডিও / ব্যর্থ) - একক পদ্ধতিতে কনভার্ট হচ্ছে")
        src_path = work_src
        for attempt in (range(3) if not done else []):
            # ১: ট্যাগসহ  ২: ট্যাগ ছাড়া  ৩: ইংরেজি নামের কপি থেকে (বাংলা ফাইলনেম সমস্যা এড়াতে)
            if attempt == 2:
                try:
                    src_path = os.path.join(tmpdir, "in" + src_ext)
                    shutil.copyfile(src, src_path)
                except Exception:
                    break
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            rc = _run_ff_progress(build(attempt != 1, src_path), duration, on_progress, cancel, err_path)
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
            return "fail", "⚠ কনভার্ট ব্যর্থ: " + (err[-1][:160] if err else "অজানা ত্রুটি") + \
                   " (ডাউনলোড করা মূল ফাইল সেভ ফোল্ডারে রাখা আছে)"

        el = time.time() - t_start
        if duration > 0 and el > 0:
            note(f"⚙ এনকোড গতি: {duration / el:.0f}× রিয়েলটাইম ({fmt_time(el)} এ {fmt_time(duration)} অডিও)")
        verified = _has_cover(tmp_out) if cover else True
        kb = _id3_size(tmp_out) / 1024
        try:
            os.replace(tmp_out, dst)
        except OSError:
            shutil.copyfile(tmp_out, dst)   # ভিন্ন ড্রাইভ হলেও কাজ করবে
        if os.path.abspath(src) != os.path.abspath(dst):
            try:
                os.remove(src)             # মূল স্ট্রিম ফাইল মুছে ফেলা
            except OSError:
                pass

        if cover:
            if verified:
                extra = f", ট্যাগ {kb:.0f} KB" if codec == "mp3" else ""
                turbo_txt = f" • টার্বো ×{turbo_n} কোর" if turbo_n else ""
                return "ok", f"🖼 কভার আর্ট বসেছে ({cfg['size']}×{cfg['size']}{extra}{turbo_txt}) ✔"
            return "ok", "⚠ কভার বসানো হয়েছিল, কিন্তু যাচাইয়ে ধরা পড়েনি (ffmpeg সংস্করণ পুরনো হতে পারে)।"
        if cfg["cover"] and cover_err:
            return "ok", f"⚠ {cover_err} - কভার ছাড়াই সেভ হয়েছে।"
        return "ok", "✔ কনভার্ট সম্পন্ন" + (f" • টার্বো ×{turbo_n} কোর" if turbo_n else "") + " (কভার বন্ধ)।"
    except Exception as e:
        return "fail", f"⚠ কনভার্ট ধাপে সমস্যা: {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# =====================================================================
#  মূল অ্যাপ
# =====================================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YT Audio Studio")
        self.geometry("1120x%d" % min(800, max(640, self.winfo_screenheight() - 90)))
        self.minsize(980, 620)
        self.configure(fg_color=C_BG)

        self.q = queue.Queue()
        self.busy = False
        self.cancel_flag = threading.Event()
        self._last_ui = 0.0
        self._n = 1
        self._i = 0
        self._dl_share = 0.5
        self._thumb_img = None
        self.history = self._load_history()
        self.out_dir = os.path.join(os.path.expanduser("~"), "Music", "YT Audio")

        self._t_start = 0.0
        self._conv_t0 = 0.0
        self.info_cache = {}
        self._fetch_gen = 0
        self._fetch_after = None
        self._build_layout()
        self._build_dashboard()
        self._build_history_page()
        self.show_page("dash")
        self._refresh_stats()
        self._pump()
        self._tick()

        if FFMPEG_PATH is False:
            self.log("⚠ FFmpeg পাওয়া যায়নি। ইন্টারনেট দিয়ে আবার চালু করুন অথবা FFmpeg ইনস্টল করুন।", C_ERR)
        elif FFMPEG_PATH:
            self.log("ℹ বান্ডেল্ড FFmpeg ব্যবহার হচ্ছে।", C_TEXT_DIM)

    # ---------- থ্রেড-সেফ UI কিউ ----------
    def ui(self, fn, *a, **kw):
        self.q.put((fn, a, kw))

    def _pump(self):
        try:
            while True:
                fn, a, kw = self.q.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _tick(self):
        """চলমান কাজের সময় (লাইভ ঘড়ি)।"""
        if self.busy:
            self.timer_label.configure(text="⏱ " + fmt_time(time.time() - self._t_start),
                                       text_color=C_WARN)
        else:
            self.timer_label.configure(text_color=C_OK if self._t_start else C_TEXT_DIM)
        self.after(250, self._tick)

    def report_callback_exception(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + text + "\n")
        except Exception:
            pass
        self.log("ত্রুটি: " + str(val), C_ERR)

    # ---------- লেআউট ----------
    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # সাইডবার
        sb = ctk.CTkFrame(self, fg_color=C_SIDEBAR, corner_radius=0, width=230)
        sb.grid(row=0, column=0, sticky="ns")
        sb.grid_propagate(False)

        ctk.CTkLabel(sb, text="🎧", font=ctk.CTkFont(size=34)).pack(anchor="w", padx=24, pady=(28, 0))
        ctk.CTkLabel(sb, text="YT Audio Studio", font=ctk.CTkFont(size=19, weight="bold")).pack(
            anchor="w", padx=24)
        ctk.CTkLabel(sb, text="Fast audio converter", text_color=C_TEXT_DIM).pack(
            anchor="w", padx=24, pady=(0, 28))

        self.nav = {}
        for key, label in (("dash", "▶   Dashboard"), ("hist", "🕘   History")):
            b = ctk.CTkButton(sb, text=label, anchor="w", height=44, corner_radius=10,
                              fg_color="transparent", hover_color=C_CARD2,
                              font=ctk.CTkFont(size=14),
                              command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=14, pady=3)
            self.nav[key] = b

        ff_ok = FFMPEG_PATH is not False
        ctk.CTkLabel(sb, text=("● FFmpeg ready" if ff_ok else "● FFmpeg missing"),
                     text_color=(C_OK if ff_ok else C_ERR)).pack(side="bottom", anchor="w", padx=24, pady=22)

        # কনটেন্ট
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(22, 24), pady=20)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    def show_page(self, key):
        for k, b in self.nav.items():
            b.configure(fg_color=(C_ACCENT if k == key else "transparent"))
        self.page_dash.grid_remove()
        self.page_hist.grid_remove()
        (self.page_dash if key == "dash" else self.page_hist).grid(row=0, column=0, sticky="nsew")
        if key == "hist":
            self._render_history()

    # ---------- ড্যাশবোর্ড ----------
    def _stat_card(self, parent, col, icon, title):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=16)
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 10, 0))
        ctk.CTkLabel(card, text=icon, font=ctk.CTkFont(size=22)).grid(
            row=0, column=0, rowspan=2, padx=(18, 12), pady=10)
        ctk.CTkLabel(card, text=title, text_color=C_TEXT_DIM).grid(row=0, column=1, sticky="w", pady=(10, 0))
        val = ctk.CTkLabel(card, text="-", font=ctk.CTkFont(size=19, weight="bold"))
        val.grid(row=1, column=1, sticky="w", pady=(0, 10))
        return val

    def _build_dashboard(self):
        p = self.page_dash = ctk.CTkFrame(self.content, fg_color="transparent")
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(2, weight=1)

        # স্ট্যাটস
        stats = ctk.CTkFrame(p, fg_color="transparent")
        stats.grid(row=0, column=0, sticky="ew")
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="s")
        self.st_count = self._stat_card(stats, 0, "🎵", "Converted files")
        self.st_time = self._stat_card(stats, 1, "⏱", "Total audio")
        self.st_last = self._stat_card(stats, 2, "⏱", "Last file took")
        self.st_state = self._stat_card(stats, 3, "⚡", "Status")

        # লিংক বক্স (একাধিক লিংক - প্রতি লাইনে একটা)
        uc = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=16)
        uc.grid(row=1, column=0, sticky="ew", pady=10)
        uc.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(uc, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 0))
        ctk.CTkLabel(head, text="YouTube লিংক (একাধিক হলে প্রতি লাইনে একটা করে দিন)",
                     text_color=C_TEXT_DIM).pack(side="left")
        self.link_count = ctk.CTkLabel(head, text="0 link", text_color=C_ACCENT)
        self.link_count.pack(side="right")
        self.dur_label = ctk.CTkLabel(head, text="", text_color=C_OK,
                                      font=ctk.CTkFont(size=15, weight="bold"))
        self.dur_label.pack(side="right", padx=16)

        self.url_box = ctk.CTkTextbox(uc, height=74, fg_color=C_CARD2, corner_radius=12,
                                      border_width=0, font=ctk.CTkFont(size=13))
        self.url_box.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(6, 14))
        self.url_box.bind("<KeyRelease>", lambda e: self._update_link_count())

        bcol = ctk.CTkFrame(uc, fg_color="transparent")
        bcol.grid(row=1, column=1, padx=(0, 16), pady=(6, 14))
        ctk.CTkButton(bcol, text="Paste", width=96, height=32, corner_radius=10, fg_color=C_CARD2,
                      hover_color=C_HOVER, command=self.paste).grid(row=0, column=0, padx=(0, 6), pady=(0, 6))
        ctk.CTkButton(bcol, text="Clear", width=96, height=32, corner_radius=10, fg_color=C_CARD2,
                      hover_color=C_HOVER, command=self.clear_links).grid(row=0, column=1, pady=(0, 6))
        ctk.CTkButton(bcol, text="Fetch Info", width=198, height=32, corner_radius=10,
                      fg_color=C_ACCENT, hover_color=C_ACCENT_H,
                      command=self.fetch_info).grid(row=1, column=0, columnspan=2)

        # মাঝের অংশ: প্রিভিউ + সেটিংস
        mid = ctk.CTkFrame(p, fg_color="transparent")
        mid.grid(row=2, column=0, sticky="nsew")
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_rowconfigure(0, weight=1)

        prev = ctk.CTkFrame(mid, fg_color=C_CARD, corner_radius=16, width=250)
        prev.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        prev.grid_propagate(False)
        self.thumb_label = ctk.CTkLabel(prev, text="No preview", text_color=C_TEXT_DIM,
                                        width=214, height=214, fg_color=C_CARD2, corner_radius=12)
        self.thumb_label.pack(padx=18, pady=(16, 10))
        self.title_label = ctk.CTkLabel(prev, text="ভিডিওর নাম এখানে দেখাবে", wraplength=212,
                                        justify="left", anchor="w",
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.title_label.pack(fill="x", padx=18)
        self.meta_label = ctk.CTkLabel(prev, text="", text_color=C_TEXT_DIM, wraplength=212,
                                       justify="left", anchor="w")
        self.meta_label.pack(fill="x", padx=18, pady=(4, 0))

        st = ctk.CTkScrollableFrame(mid, fg_color=C_CARD, corner_radius=16)
        st.grid(row=0, column=1, sticky="nsew")
        st.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(st, text="Format", text_color=C_TEXT_DIM).grid(row=0, column=0, sticky="w", padx=14, pady=(8, 0))
        ctk.CTkLabel(st, text="Quality (kbps)", text_color=C_TEXT_DIM).grid(row=0, column=1, sticky="w", padx=14, pady=(8, 0))

        self.format_var = tk.StringVar(value="MP3")
        self.format_menu = ctk.CTkOptionMenu(st, values=list(FORMATS.keys()), variable=self.format_var,
                                             height=36, fg_color=C_CARD2, button_color=C_ACCENT,
                                             button_hover_color=C_ACCENT_H, command=self.on_format_change)
        self.format_menu.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 8))
        self.quality_var = tk.StringVar(value="192")
        self.quality_menu = ctk.CTkOptionMenu(st, values=FORMATS["MP3"][1], variable=self.quality_var,
                                              height=36, fg_color=C_CARD2, button_color=C_ACCENT,
                                              button_hover_color=C_ACCENT_H)
        self.quality_menu.grid(row=1, column=1, sticky="ew", padx=14, pady=(4, 8))

        self.embed_thumb = tk.BooleanVar(value=True)
        self.square_crop = tk.BooleanVar(value=False)
        self.embed_meta = tk.BooleanVar(value=True)
        self.playlist = tk.BooleanVar(value=False)

        self.sw_thumb = ctk.CTkSwitch(st, text="থাম্বনেল কভার আর্ট হিসেবে যোগ করুন",
                                      variable=self.embed_thumb, progress_color=C_ACCENT)
        self.sw_thumb.grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=4)
        self.sw_crop = ctk.CTkSwitch(st, text="কভার মাঝখান থেকে ক্রপ করুন (বন্ধ থাকলে পুরো ছবি দেখাবে)",
                                     variable=self.square_crop, progress_color=C_ACCENT)
        self.sw_crop.grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=4)
        ctk.CTkSwitch(st, text="টাইটেল / আর্টিস্ট মেটাডাটা যোগ করুন", variable=self.embed_meta,
                      progress_color=C_ACCENT).grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=4)
        ctk.CTkSwitch(st, text="প্লেলিস্ট লিংক হলে পুরো প্লেলিস্ট নামান", variable=self.playlist,
                      progress_color=C_ACCENT).grid(row=5, column=0, columnspan=2, sticky="w", padx=14, pady=4)

        ctk.CTkLabel(st, text="Save to", text_color=C_TEXT_DIM).grid(row=6, column=0, sticky="w", padx=14, pady=(8, 0))
        fr = ctk.CTkFrame(st, fg_color="transparent")
        fr.grid(row=7, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 8))
        fr.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(fr, height=36, fg_color=C_CARD2, border_width=0)
        self.dir_entry.insert(0, self.out_dir)
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(fr, text="Browse", width=90, height=36, fg_color=C_CARD2, hover_color=C_HOVER,
                      command=self.choose_dir).grid(row=0, column=1)

        ctk.CTkLabel(st, text="Cover size (কভার ছবির সাইজ)", text_color=C_TEXT_DIM).grid(
            row=8, column=0, columnspan=2, sticky="w", padx=14, pady=(6, 0))
        self.cover_var = tk.StringVar(value=list(COVER_SIZES.keys())[0])
        ctk.CTkOptionMenu(st, values=list(COVER_SIZES.keys()), variable=self.cover_var, height=36,
                          fg_color=C_CARD2, button_color=C_ACCENT, button_hover_color=C_ACCENT_H
                          ).grid(row=9, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 6))
        self.turbo = tk.BooleanVar(value=True)
        self.ultra = tk.BooleanVar(value=False)
        self.date_prefix = tk.BooleanVar(value=True)
        self.smart_tags = tk.BooleanVar(value=True)
        ctk.CTkSwitch(st, text="স্মার্ট ট্যাগ: নাম থেকে (Official Video) ইত্যাদি বাদ + আর্টিস্ট/টাইটেল আলাদা",
                      variable=self.smart_tags, progress_color=C_ACCENT).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 4))
        ctk.CTkSwitch(st, text="⚡ টার্বো: ১০+ মিনিটের অডিওতে সব CPU কোর একসাথে (MP3 দ্রুততম)",
                      variable=self.turbo, progress_color=C_ACCENT).grid(
            row=11, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 4))
        ctk.CTkSwitch(st, text="🚀 আল্ট্রা স্পিড: ছোট স্ট্রিম + মোনো MP3 (বক্তৃতা/ওয়াজে সেরা, গানে মান কমে)",
                      variable=self.ultra, progress_color=C_ACCENT).grid(
            row=12, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 4))
        ctk.CTkSwitch(st, text=f"📅 ফাইলের নাম ও টাইটেলের শুরুতে আজকের তারিখ বসান (যেমন {time.strftime('%Y-%m-%d')})",
                      variable=self.date_prefix, progress_color=C_ACCENT).grid(
            row=13, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 12))

        # অ্যাকশন কার্ড: বাটন + পার্সেন্ট + স্ট্যাটাস (সবসময় দেখা যাবে)
        ac = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=16)
        ac.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ac.grid_columnconfigure(0, weight=1)

        self.dl_btn = ctk.CTkButton(ac, text="⬇  Convert to Audio", height=46, corner_radius=12,
                                    font=ctk.CTkFont(size=16, weight="bold"), fg_color=C_ACCENT,
                                    hover_color=C_ACCENT_H, command=self.start_download)
        self.dl_btn.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=(14, 8))
        self.cancel_btn = ctk.CTkButton(ac, text="Cancel", width=100, height=46, corner_radius=12,
                                        fg_color=C_CARD2, hover_color=C_ERR, state="disabled",
                                        command=self.cancel)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 16), pady=(14, 8))

        pr = ctk.CTkFrame(ac, fg_color="transparent")
        pr.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16)
        pr.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(pr, height=12, progress_color=C_ACCENT, fg_color=C_CARD2)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pct_label = ctk.CTkLabel(pr, text="0%", width=64, anchor="e",
                                      font=ctk.CTkFont(size=18, weight="bold"), text_color=C_ACCENT)
        self.pct_label.grid(row=0, column=1, padx=(10, 0))
        self.timer_label = ctk.CTkLabel(pr, text="⏱ 0:00", width=84, anchor="e", text_color=C_TEXT_DIM,
                                        font=ctk.CTkFont(size=15, weight="bold"))
        self.timer_label.grid(row=0, column=2, padx=(8, 0))

        self.status = ctk.CTkLabel(ac, text="Ready", text_color=C_TEXT_DIM, anchor="w")
        self.status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 12))

        # লগ
        self.log_box = ctk.CTkTextbox(p, height=110, fg_color=C_CARD, corner_radius=14,
                                      text_color=C_TEXT_DIM, font=ctk.CTkFont(size=12))
        self.log_box.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.log_box.configure(state="disabled")

    # ---------- হিস্টরি পেজ ----------
    def _build_history_page(self):
        p = self.page_hist = ctk.CTkFrame(self.content, fg_color="transparent")
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(p, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(top, text="History", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkButton(top, text="Clear", width=80, fg_color=C_CARD2, hover_color=C_ERR,
                      command=self.clear_history).pack(side="right")
        self.hist_list = ctk.CTkScrollableFrame(p, fg_color=C_CARD, corner_radius=16)
        self.hist_list.grid(row=1, column=0, sticky="nsew")
        self.hist_list.grid_columnconfigure(0, weight=1)

    def _render_history(self):
        for w in self.hist_list.winfo_children():
            w.destroy()
        if not self.history:
            ctk.CTkLabel(self.hist_list, text="এখনো কিছু কনভার্ট করা হয়নি।", text_color=C_TEXT_DIM).pack(pady=40)
            return
        for item in reversed(self.history[-200:]):
            row = ctk.CTkFrame(self.hist_list, fg_color=C_CARD2, corner_radius=12)
            row.pack(fill="x", padx=8, pady=4)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=item.get("title", "?"), anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
            sub = f"{item.get('fmt', '')}  •  অডিও {fmt_time(item.get('duration', 0))}  •  {item.get('date', '')}"
            if item.get("secs"):
                sub += f"  •  ⏱ লেগেছে {fmt_time(item['secs'])}"
            ctk.CTkLabel(row, text=sub, text_color=C_TEXT_DIM, anchor="w").grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))
            path = item.get("path", "")
            ctk.CTkButton(row, text="Open folder", width=100, height=32, fg_color=C_CARD, hover_color=C_HOVER,
                          command=lambda pth=path: open_path(os.path.dirname(pth) if pth else self.out_dir)
                          ).grid(row=0, column=1, rowspan=2, padx=12)

    # ---------- হিস্টরি ডাটা ----------
    def _load_history(self):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history[-500:], f, ensure_ascii=False)
        except Exception:
            pass

    def clear_history(self):
        self.history = []
        self._save_history()
        self._render_history()
        self._refresh_stats()

    def _refresh_stats(self):
        self.st_count.configure(text=str(len(self.history)))
        self.st_time.configure(text=fmt_time(sum(i.get("duration", 0) for i in self.history)))
        self.st_state.configure(text="Working..." if self.busy else "Idle",
                                text_color=(C_WARN if self.busy else C_OK))

    # ---------- সহায়ক ----------
    def log(self, msg, color=None):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "log.txt"), "a", encoding="utf-8") as lf:
                lf.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
        except Exception:
            pass

        def _w():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.ui(_w)

    def set_status(self, text):
        self.ui(lambda: self.status.configure(text=text))

    def get_urls(self):
        """বক্স থেকে সব লিংক বের করে (ডুপ্লিকেট বাদ, ফাঁকা বাদ)।"""
        urls = []
        for tok in self.url_box.get("1.0", "end").split():
            tok = tok.strip().strip(",;")
            if not tok:
                continue
            if not tok.lower().startswith(("http://", "https://")):
                if "youtu" in tok.lower():
                    tok = "https://" + tok
                else:
                    continue
            if tok not in urls:
                urls.append(tok)
        return urls

    def _update_link_count(self):
        n = len(self.get_urls())
        self.link_count.configure(text=f"{n} link{'s' if n != 1 else ''}")
        self._refresh_duration_label()
        if self._fetch_after is not None:
            try:
                self.after_cancel(self._fetch_after)
            except Exception:
                pass
        self._fetch_after = self.after(900, self._start_auto_fetch)   # লিংক দেওয়ার একটু পরে নিজে থেকেই তথ্য আনবে

    def paste(self):
        try:
            txt = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not txt:
            return
        cur = self.url_box.get("1.0", "end").strip()
        if cur:
            self.url_box.insert("end", ("" if self.url_box.get("end-2c", "end-1c") == "\n" else "\n"))
        self.url_box.insert("end", txt + "\n")
        self._update_link_count()

    def clear_links(self):
        if self.busy:
            return
        self.url_box.delete("1.0", "end")
        self._update_link_count()

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_entry.get() or self.out_dir)
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def on_format_change(self, choice):
        _, quals, default, thumb_ok = FORMATS[choice]
        self.quality_menu.configure(values=quals)
        self.quality_var.set(default)
        st = "normal" if thumb_ok else "disabled"
        self.sw_thumb.configure(state=st)
        self.sw_crop.configure(state=st)
        self.embed_thumb.set(bool(thumb_ok))

    # ---------- তথ্য আনা ----------
    def fetch_info(self):
        self._start_auto_fetch(force=True)

    def _refresh_duration_label(self):
        urls = self.get_urls()
        if not urls:
            self.dur_label.configure(text="")
            return
        got = [self.info_cache.get(u) for u in urls]
        pending = sum(1 for g in got if g is None)
        ok = [g for g in got if g and "error" not in g]
        errs = sum(1 for g in got if g and "error" in g)
        total = sum((g.get("duration") or 0) for g in ok)
        txt = ""
        if ok:
            if len(urls) == 1:
                g = ok[0]
                txt = (f"⏱ প্লেলিস্ট • {g.get('count', 0)}টি ভিডিও" if g.get("playlist")
                       else f"⏱ ভিডিও {fmt_time(total)}")
            else:
                txt = f"⏱ মোট {fmt_time(total)}"
            if pending:
                txt += f"  ({len(ok)}/{len(urls)})"
        elif pending:
            txt = "⏳ তথ্য আনা হচ্ছে..."
        if errs:
            txt += ("  " if txt else "") + f"⚠ {errs}টি লিংক পড়া যায়নি"
        self.dur_label.configure(text=txt)

    def _show_first_preview(self):
        urls = self.get_urls()
        if not urls:
            return
        g = self.info_cache.get(urls[0])
        if g and "error" not in g:
            meta = f"{g.get('uploader', '')}\nDuration: {fmt_time(g.get('duration'))}"
            self._show_info(g.get("title", "Unknown"), meta, g.get("img"))

    def _start_auto_fetch(self, force=False):
        self._fetch_after = None
        urls = self.get_urls()
        if force:
            for u in urls:
                self.info_cache.pop(u, None)
        self._refresh_duration_label()
        if not urls:
            return
        if self.busy and not force:
            return
        if all(u in self.info_cache for u in urls):
            self._show_first_preview()
            return
        self._fetch_gen += 1
        threading.Thread(target=self._auto_fetch_worker, args=(urls, self._fetch_gen), daemon=True).start()

    def _auto_fetch_worker(self, urls, gen):
        for idx, u in enumerate(urls):
            if gen != self._fetch_gen:
                return
            if u in self.info_cache:
                continue
            try:
                opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                        "extract_flat": "in_playlist",
                        "extractor_args": {"youtube": {"skip": ["hls", "dash", "translated_subs"]}}}
                if FFMPEG_PATH:
                    opts["ffmpeg_location"] = FFMPEG_PATH
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(u, download=False)
                if info.get("_type") == "playlist":
                    ents = [e for e in (info.get("entries") or []) if e]
                    entry = {"title": info.get("title") or "Playlist", "playlist": True, "count": len(ents),
                             "duration": sum((e.get("duration") or 0) for e in ents),
                             "uploader": info.get("uploader") or info.get("channel") or ""}
                else:
                    img = None
                    turl = info.get("thumbnail")
                    if idx == 0 and turl:
                        try:
                            data = urllib.request.urlopen(turl, timeout=15).read()
                            im = Image.open(io.BytesIO(data)).convert("RGB")
                            w, h = im.size
                            sq = min(w, h)
                            img = im.crop(((w - sq) // 2, (h - sq) // 2, (w + sq) // 2, (h + sq) // 2)).resize((214, 214))
                        except Exception:
                            img = None
                    entry = {"title": info.get("title", "Unknown"), "duration": info.get("duration") or 0,
                             "uploader": info.get("uploader") or info.get("channel") or "", "img": img}
                self.info_cache[u] = entry
            except Exception as e:
                self.info_cache[u] = {"error": str(e)[:100]}
            if gen == self._fetch_gen:
                self.ui(self._refresh_duration_label)
                if idx == 0:
                    self.ui(self._show_first_preview)

    def _show_info(self, title, meta, img):
        self.title_label.configure(text=title)
        self.meta_label.configure(text=meta)
        if img is not None:
            self._thumb_img = ctk.CTkImage(light_image=img, dark_image=img, size=(214, 214))
            self.thumb_label.configure(image=self._thumb_img, text="")

    # ---------- ডাউনলোড / কনভার্ট ----------
    def start_download(self):
        if self.busy:
            return
        urls = self.get_urls()
        if not urls:
            messagebox.showwarning("লিংক নেই", "আগে অন্তত একটি YouTube লিংক দিন।")
            return
        if FFMPEG_PATH is False:
            messagebox.showerror("FFmpeg দরকার", "FFmpeg পাওয়া যায়নি। ইন্টারনেট চালু রেখে অ্যাপ আবার খুলুন।")
            return
        out = self.dir_entry.get().strip() or self.out_dir
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as e:
            messagebox.showerror("ফোল্ডার সমস্যা", str(e))
            return

        # সব সেটিংস মেইন থ্রেডে পড়ে নেওয়া হচ্ছে (থ্রেড-সেফ)
        opts = self._build_opts(out)
        fmt_name = self.format_var.get().split(" ")[0]
        codec, _, _, thumb_ok = FORMATS[self.format_var.get()]
        fmt_fast = ("worstaudio[ext=m4a]/worstaudio/bestaudio[ext=m4a]/bestaudio/best" if self.ultra.get()
                    else "bestaudio[ext=m4a]/bestaudio/best")
        if codec in ("mp3", "m4a"):
            opts["format"] = fmt_fast
        cfg = {
            "codec": codec,
            "cover": bool(self.embed_thumb.get() and thumb_ok and codec in ("mp3", "m4a")),
            "size": COVER_SIZES.get(self.cover_var.get(), 300),
            "square": bool(self.square_crop.get()),
            "smart": bool(self.smart_tags.get()) and codec in ("mp3", "m4a"),
            "meta": bool(self.embed_meta.get()),
            "quality": self.quality_var.get(),
            "fast": codec in ("mp3", "m4a"),   # এক ধাপের দ্রুত পাইপলাইন
            "turbo": bool(self.turbo.get()),
            "ultra": bool(self.ultra.get()),
            "date": self._date_pfx().strip(),
            "playlist": bool(self.playlist.get()),
            "fopts": {
                "quiet": True, "no_warnings": True, "noplaylist": True, "retries": 10,
                "format": fmt_fast,
                "extractor_args": {"youtube": {"skip": ["hls", "dash", "translated_subs"]}},  # হালকা ও দ্রুত তথ্য আনা
                "outtmpl": opts["outtmpl"],
                **({"ffmpeg_location": FFMPEG_PATH} if FFMPEG_PATH else {}),
            },
        }
        # ডাউনলোড বনাম কনভার্টের মধ্যে প্রোগ্রেস বার ভাগ
        self._dl_share = {"mp3": 0.6, "m4a": 0.9}.get(codec, 0.9)

        self.busy = True
        self._t_start = time.time()
        self.cancel_flag.clear()
        self._n = len(urls)
        self._i = 0
        self.dl_btn.configure(state="disabled", text="Working...")
        self.cancel_btn.configure(state="normal")
        self._apply_progress(0.0)
        self._refresh_stats()
        self.log(f"▶ {len(urls)}টি লিংক কিউতে দেওয়া হলো।")
        threading.Thread(target=self._download_worker, args=(urls, out, opts, fmt_name, cfg), daemon=True).start()

    def cancel(self):
        self.cancel_flag.set()
        self.set_status("বাতিল করা হচ্ছে...")

    def _apply_progress(self, overall):
        overall = max(0.0, min(1.0, overall))
        self.progress.set(overall)
        self.pct_label.configure(text=f"{overall * 100:.0f}%")

    def _item_progress(self, frac):
        """বর্তমান লিংকের অগ্রগতি (0..1) থেকে মোট অগ্রগতি বের করে দেখায়।"""
        overall = (self._i + frac) / max(self._n, 1)
        self.ui(self._apply_progress, overall)

    def _date_pfx(self):
        return (time.strftime("%Y-%m-%d") + " ") if self.date_prefix.get() else ""

    def _build_opts(self, out):
        name = self.format_var.get()
        codec, _, _, thumb_ok = FORMATS[name]
        quality = self.quality_var.get()

        extract = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if quality.isdigit():
            extract["preferredquality"] = quality
        else:
            extract["preferredquality"] = None  # কপি / লসলেস
        pps = [extract]

        use_thumb = self.embed_thumb.get() and thumb_ok
        # MP3/M4A-তে কভার আমরা নিজে বসাই (ফোন-সামঞ্জস্য), বাকি ফরম্যাটে yt-dlp
        custom_cover = use_thumb and codec in ("mp3", "m4a")
        yt_cover = use_thumb and not custom_cover
        if yt_cover:
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if self.embed_meta.get():
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
        if yt_cover:
            pps.append({"key": "EmbedThumbnail"})

        # শুধু অডিও স্ট্রিম - ভিডিও কখনোই নামে না
        fmt = "bestaudio/best"
        if codec in ("mp3", "m4a"):
            # দ্রুত পাইপলাইন: yt-dlp শুধু অডিও স্ট্রিম নামাবে (ছোট m4a পছন্দ), বাকি সব আমরা এক ধাপে করব
            pps = []
            yt_cover = False
            fmt = "bestaudio[ext=m4a]/bestaudio/best"

        opts = {
            "format": fmt,
            "outtmpl": os.path.join(out, self._date_pfx() + "%(title).140s.%(ext)s"),
            "noplaylist": not self.playlist.get(),
            "postprocessors": pps,
            "writethumbnail": yt_cover,
            "progress_hooks": [self._hook],
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,   # দ্রুত ডাউনলোড
            "http_chunk_size": 10 * 1024 * 1024,  # স্পিড থ্রোটল এড়াতে
            "noprogress": True,
            "updatetime": False,
        }
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH
        if yt_cover and self.square_crop.get():
            opts["postprocessor_args"] = {"thumbnailsconvertor+ffmpeg_o": SQUARE_CROP}
        return opts

    def _hook(self, d):
        if self.cancel_flag.is_set():
            raise DownloadCancelled()
        status = d.get("status")
        info = d.get("info_dict") or {}
        pi = info.get("playlist_index")
        pn = info.get("n_entries") or info.get("playlist_count")
        tag = f"({self._i + 1}/{self._n}) "

        def scale(f):
            # ডাউনলোড = ০-৯০%, কনভার্ট = ৯০-১০০% (প্লেলিস্ট হলে ভাগ করে)
            if pi and pn and pn > 1:
                return ((pi - 1) + f) / pn
            return f

        if status == "downloading":
            now = time.time()
            if now - self._last_ui < 0.35:  # লো-কনফিগ পিসির জন্য UI আপডেট কমানো
                return
            self._last_ui = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                f = done / total
            elif d.get("fragment_count"):
                f = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                f = 0.0
            f = max(0.0, min(f, 1.0))
            self._item_progress(scale(f) * self._dl_share)

            sp = d.get("speed")
            eta = d.get("eta")
            sp_txt = f"{sp / 1048576:.1f} MB/s" if sp else "..."
            eta_txt = fmt_time(eta) if eta else "--:--"
            self.set_status(f"{tag}ডাউনলোড {f * 100:.0f}%   •   {sp_txt}   •   বাকি {eta_txt}")
        elif status == "finished":
            self._item_progress(scale(1.0) * self._dl_share)
            self.set_status(f"{tag}ডাউনলোড শেষ, কনভার্ট শুরু হচ্ছে...")

    def _download_worker(self, urls, out, opts, fmt_name, cfg):
        ok = fail = 0
        cov_ok = cov_bad = 0
        total = len(urls)
        try:
            for i, url in enumerate(urls):
                if self.cancel_flag.is_set():
                    break
                self._i = i
                self._item_progress(0.0)
                self.set_status(f"({i + 1}/{total}) তথ্য আনা হচ্ছে...")
                try:
                    t_item = time.time()
                    t_ext = t_dl = t_conv = None
                    fast_path = None
                    fast_res = None
                    if cfg["fast"] and not cfg["playlist"]:
                        def dl_cb(frac, speed, eta, i=i):
                            self._item_progress(frac * self._dl_share)
                            self.set_status(f"({i + 1}/{total}) ⚡ দ্রুত ডাউনলোড {frac * 100:.0f}%   •   "
                                            f"{speed / 1048576:.1f} MB/s   •   বাকি {fmt_time(eta)}")
                        try:
                            self.set_status(f"({i + 1}/{total}) তথ্য আনা হচ্ছে...")
                            fast_res = fast_fetch(url, cfg["fopts"], dl_cb, self.cancel_flag, cfg["cover"])
                        except DownloadCancelled:
                            raise
                        except Exception as ex:
                            self.log(f"ℹ দ্রুত ডাউনলোড হয়নি ({str(ex)[:80]})")
                            fast_res = None
                        if fast_res is None:
                            self.log("ℹ সমান্তরাল ডাউনলোড সম্ভব হয়নি, সাধারণ পদ্ধতিতে নামানো হচ্ছে...")
                    if fast_res:
                        fast_path, info, t_ext, t_dl = fast_res
                    else:
                        with yt_dlp.YoutubeDL(dict(opts)) as ydl:
                            info = ydl.extract_info(url, download=True)
                        t_dl = time.time() - t_item
                    entries = info.get("entries") if info and info.get("_type") == "playlist" else [info]
                    entries = [e for e in (entries or []) if e]
                    n_e = max(len(entries), 1)
                    item_failed = False
                    for k, e in enumerate(entries):
                        rd = e.get("requested_downloads") or []
                        path = fast_path or (rd[0].get("filepath") if rd else "")
                        if cfg["fast"]:
                            dst = os.path.splitext(path)[0] + "." + cfg["codec"]
                            label = cfg["codec"].upper()

                            def cb(frac, k=k, i=i, n_e=n_e, label=label):
                                self._item_progress(self._dl_share + ((k + frac) / n_e) * (1 - self._dl_share))
                                el = time.time() - self._conv_t0
                                eta = f"  •  বাকি ~{fmt_time(el * (1 - frac) / frac)}" if frac > 0.04 else ""
                                self.set_status(f"({i + 1}/{total}) {label}-তে কনভার্ট {frac * 100:.0f}%"
                                                f"  •  কনভার্টে গেছে {fmt_time(el)}{eta}")

                            self.set_status(f"({i + 1}/{total}) {label}-তে কনভার্ট ও কভার বসানো হচ্ছে...")
                            t0 = time.time()
                            self._conv_t0 = t0
                            st, msg = convert_audio(path, dst, e, cfg, cb, self.cancel_flag, self.log)
                            t_conv = (t_conv or 0) + (time.time() - t0)
                            if st == "cancel":
                                raise DownloadCancelled()
                            self.log(msg)
                            if cfg["cover"]:
                                if msg.startswith("🖼"):
                                    cov_ok += 1
                                else:
                                    cov_bad += 1
                            if st == "fail":
                                item_failed = True
                                continue
                            path = dst
                        self.history.append({
                            "title": e.get("title", "?"), "duration": e.get("duration") or 0,
                            "fmt": fmt_name, "path": path or out,
                            "secs": round(time.time() - t_item, 1),
                            "date": time.strftime("%Y-%m-%d %H:%M"),
                        })
                    self._save_history()
                    if item_failed:
                        fail += 1
                    else:
                        ok += 1
                        self.log(f"✔ ({i + 1}/{total}) সম্পন্ন: {(info or {}).get('title', url)}")
                        parts_t = []
                        if t_ext is not None:
                            parts_t.append(f"তথ্য {t_ext:.1f}s")
                        if t_dl is not None:
                            parts_t.append(f"ডাউনলোড {t_dl:.1f}s")
                        if t_conv is not None:
                            parts_t.append(f"কনভার্ট {t_conv:.1f}s")
                        self.ui(lambda v=fmt_time(time.time() - t_item): self.st_last.configure(text=v))
                        self.log("⏱ " + " • ".join(parts_t) + f" • মোট {time.time() - t_item:.1f}s"
                                 f"  (অডিও {fmt_time((info or {}).get('duration'))})")
                    self._item_progress(1.0)
                except DownloadCancelled:
                    break
                except Exception as e:
                    if self.cancel_flag.is_set():
                        break
                    fail += 1
                    msg = str(e).replace("ERROR: ", "").strip().splitlines()[0][:160]
                    self.log(f"✖ ({i + 1}/{total}) ব্যর্থ: {msg}")
                    self._item_progress(1.0)  # পরের লিংকে এগোনো, বার ঠিক রাখা

            if self.cancel_flag.is_set():
                self.set_status(f"বাতিল করা হয়েছে (সফল {ok}, ব্যর্থ {fail})")
                self.log("■ বাতিল করা হয়েছে।")
            else:
                self.ui(self._apply_progress, 1.0)
                cov_txt = ""
                if cfg["cover"]:
                    cov_txt = f"  •  কভার ✔ {cov_ok}" + (f"  ⚠ {cov_bad} (লগ দেখুন)" if cov_bad else "")
                if fail == 0:
                    self.set_status(f"✔ সব সম্পন্ন! {ok}টি ফাইল সেভ হয়েছে{cov_txt}")
                else:
                    self.set_status(f"শেষ: {ok}টি সফল, {fail}টি ব্যর্থ (লগ দেখুন)")
                self.log(f"📁 সেভ ফোল্ডার: {out}")
        except Exception as e:
            self.set_status("ব্যর্থ হয়েছে")
            self.log(f"ত্রুটি: {e}")
        finally:
            total_s = time.time() - self._t_start
            self.busy = False
            self.ui(lambda: self.timer_label.configure(text="⏱ " + fmt_time(total_s)))
            self.ui(lambda: self.dl_btn.configure(state="normal", text="⬇  Convert to Audio"))
            self.ui(lambda: self.cancel_btn.configure(state="disabled"))
            self.ui(self._refresh_stats)


def main():
    try:
        app = App()
        app.mainloop()
    except Exception:
        show_fatal(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
