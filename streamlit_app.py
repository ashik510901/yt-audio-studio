"""
YT Audio Studio - Web Version (Streamlit)

YouTube লিংক থেকে সরাসরি অডিও (MP3/M4A/OPUS/FLAC/WAV) + কভার আর্ট + স্মার্ট ট্যাগ।
সব প্রসেসিং সার্ভারে হয়; আপনার কম্পিউটারে শুধু ব্রাউজার লাগে।

ফাইল: streamlit_app.py (UI)  •  core.py (ইঞ্জিন)  •  requirements.txt  •  packages.txt
"""

import os
import re
import time
import hmac
import tempfile
import threading
import datetime

import streamlit as st

import core

# ---------------------------------------------------------------------
#  পেজ সেটআপ
# ---------------------------------------------------------------------
st.set_page_config(page_title="YT Audio Studio", page_icon="🎧", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1150px;}
#MainMenu, footer {visibility: hidden;}
.ycard {background:#161920; border:1px solid #232733; border-radius:16px; padding:14px 18px; margin-bottom:12px;}
.chip {display:inline-block; padding:3px 11px; border-radius:999px; background:#1d212b; color:#c9d1e3;
       font-size:12.5px; margin:2px 6px 2px 0; border:1px solid #2a3040;}
.chip.ok {color:#2ecc71; border-color:#2ecc7155;}
.chip.bad {color:#ff7b7b; border-color:#ff7b7b55;}
.durbox {background:linear-gradient(90deg,#16261e,#161920); border:1px solid #2ecc7166; color:#2ecc71;
         border-radius:14px; padding:10px 18px; font-size:19px; font-weight:700; margin:6px 0 10px 0;}
.title {font-size:30px; font-weight:800; margin-bottom:0;}
.sub {color:#8b93a7; margin-top:-4px; margin-bottom:10px;}
.timer {font-size:20px; font-weight:800; color:#f5a623;}
</style>
""", unsafe_allow_html=True)

BASE_DIR = os.path.join(tempfile.gettempdir(), "ytas_web")
MAX_LINKS = 10
RETAIN_RESULTS = 3          # সেশনে সর্বোচ্চ কয়টি ফাইল ডাউনলোডের জন্য রাখা হবে
RETAIN_MB = 300             # মোট সর্বোচ্চ সংরক্ষিত ফাইলের সাইজ (RAM/ডিস্ক বাঁচাতে)


# ---------------------------------------------------------------------
#  Secrets (সব ঐচ্ছিক)
# ---------------------------------------------------------------------
def secret(name, default=None):
    try:
        v = st.secrets.get(name, default)
        return default if v is None else v
    except Exception:
        return default


try:
    MAX_MINUTES = max(5, min(int(secret("MAX_MINUTES", 180)), 720))
except (TypeError, ValueError):
    MAX_MINUTES = 180
TZ_NAME = str(secret("TIMEZONE", "Asia/Dhaka"))


def today_str():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=6)))
    return now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------
#  সার্ভার প্রস্তুতি (একবারই চলে)
# ---------------------------------------------------------------------
@st.cache_resource(show_spinner="সার্ভার প্রস্তুত হচ্ছে... (প্রথমবার একটু সময় লাগে)")
def boot():
    os.makedirs(BASE_DIR, exist_ok=True)
    return {
        "deno": core.ensure_deno(),
        "ffmpeg": core.ffmpeg_ok(),
        "lock": threading.Lock(),            # একসাথে একটাই ভারী কাজ (ফ্রি সার্ভারের CPU/RAM বাঁচাতে)
        "cookies": core.write_cookies(secret("YT_COOKIES", "")),
    }


ENV = boot()


# ---------------------------------------------------------------------
#  পাসওয়ার্ড গেট (APP_PASSWORD সেট করা থাকলে)
# ---------------------------------------------------------------------
def gate():
    pw = secret("APP_PASSWORD")
    if pw and str(pw).startswith("CHANGE_ME"):
        st.error("সিক্রেটসে APP_PASSWORD এখনো উদাহরণের মান (CHANGE_ME...) আছে। "
                 "Settings → Secrets-এ নিজের পাসওয়ার্ড দিন, নইলে অ্যাপ নিরাপদ নয়।")
        st.stop()
    if not pw or st.session_state.get("auth"):
        return
    st.markdown("<div class='title'>🎧 YT Audio Studio</div><div class='sub'>প্রবেশের জন্য পাসওয়ার্ড দিন</div>",
                unsafe_allow_html=True)
    with st.form("login"):
        p = st.text_input("পাসওয়ার্ড", type="password")
        ok = st.form_submit_button("প্রবেশ", type="primary")
    if ok:
        if hmac.compare_digest(str(p), str(pw)):
            st.session_state["auth"] = True
            st.rerun()
        else:
            time.sleep(1.2)
            st.error("পাসওয়ার্ড ভুল।")
    st.stop()


gate()

st.session_state.setdefault("results", [])
st.session_state.setdefault("history", [])
st.session_state.setdefault("info_cache", {})
st.session_state.setdefault("last_took", None)


# ---------------------------------------------------------------------
#  সহায়ক
# ---------------------------------------------------------------------
def parse_urls(text):
    urls = []
    for tok in (text or "").split():
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


def friendly(err):
    m = str(err)
    low = m.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube এই সার্ভারকে (ক্লাউড/ডেটাসেন্টার IP) ব্লক করেছে। "
                "README-র “কুকি” অংশ দেখুন; তাতেও না হলে ফ্রি ক্লাউডে এটা চলবে না।")
    if "private video" in low:
        return "এটি প্রাইভেট ভিডিও।"
    if "video unavailable" in low or "unavailable" in low:
        return "ভিডিওটি পাওয়া যাচ্ছে না (মুছে ফেলা / দেশ-নিষিদ্ধ / বয়স-সীমা)।"
    if "ffmpeg" in low and "not found" in low:
        return "FFmpeg পাওয়া যায়নি - packages.txt-এ ffmpeg আছে কি না দেখুন।"
    return m.replace("ERROR: ", "").strip().splitlines()[0][:220] if m.strip() else "অজানা ত্রুটি"


def base_settings(clients_text=""):
    return {
        "cookies": ENV.get("cookies"),
        "proxy": secret("YT_PROXY", "") or None,
        "clients": [c for c in re.split(r"[,\s]+", clients_text or "") if c],
    }


def prune_results():
    """সেশনের পুরনো ফাইল মুছে মেমরি/ডিস্ক বাঁচায়।"""
    res = st.session_state["results"]
    while len(res) > RETAIN_RESULTS or sum(r.get("size", 0) for r in res) > RETAIN_MB * 1024 * 1024:
        if len(res) <= 1:
            break
        old = res.pop(0)
        try:
            os.remove(old["path"])
        except OSError:
            pass


MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "opus": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav"}

# ---------------------------------------------------------------------
#  হেডার
# ---------------------------------------------------------------------
st.markdown("<div class='title'>🎧 YT Audio Studio <span style='font-size:15px;color:#8b93a7'>Web</span></div>"
            "<div class='sub'>লিংক দিন → সার্ভারে MP3 তৈরি হবে → ব্রাউজার থেকে ডাউনলোড করুন</div>",
            unsafe_allow_html=True)


def chip(text, ok=None):
    cls = "chip ok" if ok is True else "chip bad" if ok is False else "chip"
    return f"<span class='{cls}'>{text}</span>"


st.markdown(
    chip(f"FFmpeg {'✔' if ENV['ffmpeg'] else '✖'}", ENV["ffmpeg"]) +
    chip(f"JS রানটাইম (Deno) {'✔' if ENV['deno'] else '✖'}", bool(ENV["deno"])) +
    chip(f"CPU কোর {core.cpu_cores()}") +
    chip(f"yt-dlp {core.ytdlp_version()}") +
    chip(f"কুকি {'✔' if ENV['cookies'] else '— নেই'}", True if ENV["cookies"] else None) +
    chip(f"সর্বোচ্চ {MAX_MINUTES // 60}h {MAX_MINUTES % 60}m / ভিডিও"),
    unsafe_allow_html=True)

hist = st.session_state["history"]
c1, c2, c3 = st.columns(3)
c1.metric("🎵 Converted files", len(hist))
c2.metric("⏱ Total audio", core.fmt_time(sum(h["duration"] for h in hist)))
c3.metric("⚡ Last file took", core.fmt_time(st.session_state["last_took"]) if st.session_state["last_took"] is not None else "—")

tab_conv, tab_hist, tab_help = st.tabs(["🎛 Convert", "🕘 History", "ℹ Help / Server"])

# =====================================================================
#  Convert ট্যাব
# =====================================================================
with tab_conv:
    links_text = st.text_area(
        "YouTube লিংক (একাধিক হলে প্রতি লাইনে একটা করে দিন)", height=110, key="links",
        placeholder="https://www.youtube.com/watch?v=...\nhttps://youtu.be/...")
    urls = parse_urls(links_text)
    if len(urls) > MAX_LINKS:
        st.warning(f"একবারে সর্বোচ্চ {MAX_LINKS}টি লিংক নেওয়া হবে; বাকিগুলো বাদ গেল।")
        urls = urls[:MAX_LINKS]

    with st.expander("⚙ সেটিংস", expanded=True):
        fa, fb = st.columns(2)
        fmt_name = fa.selectbox("Format", list(core.FORMATS.keys()), index=0)
        fmt = core.FORMATS[fmt_name]
        quality = fb.selectbox("Quality (kbps)", fmt["qualities"],
                               index=fmt["qualities"].index(fmt["default"]), key=f"q_{fmt['codec']}")

        t1, t2 = st.columns(2)
        use_cover = t1.toggle("🖼 থাম্বনেল কভার আর্ট হিসেবে যোগ করুন", value=fmt["cover"], disabled=not fmt["cover"],
                              key=f"cover_{fmt['codec']}")
        crop = t2.toggle("কভার মাঝখান থেকে ক্রপ করুন (বন্ধ থাকলে পুরো ছবি দেখাবে)", value=False,
                         disabled=not (fmt["cover"] and use_cover))
        cover_label = st.selectbox("Cover size (কভার ছবির সাইজ)", list(core.COVER_SIZES.keys()),
                                   disabled=not (fmt["cover"] and use_cover))
        t3, t4 = st.columns(2)
        smart = t3.toggle("🏷 স্মার্ট ট্যাগ: (Official Video) ইত্যাদি বাদ + আর্টিস্ট/টাইটেল আলাদা", value=True,
                          disabled=fmt["codec"] not in ("mp3", "m4a"))
        date_on = t4.toggle(f"📅 ফাইলের নাম ও টাইটেলের শুরুতে আজকের তারিখ ({today_str()})", value=True)
        t5, t6 = st.columns(2)
        turbo = t5.toggle("⚡ টার্বো: লম্বা অডিওতে সার্ভারের সব কোর একসাথে (MP3)", value=True,
                          disabled=fmt["codec"] != "mp3")
        ultra = t6.toggle("🚀 আল্ট্রা স্পিড: ছোট স্ট্রিম + মোনো MP3 (বক্তৃতা/ওয়াজে সেরা, গানে মান কমে)", value=False)
        with st.expander("উন্নত (Advanced)"):
            clients_text = st.text_input(
                "YouTube player client (ঐচ্ছিক, কমা দিয়ে - যেমন: android_vr, web_safari)", value="",
                help="সাধারণত খালি রাখুন। ব্লক সমস্যায় বিকল্প ক্লায়েন্ট চেষ্টা করতে চাইলে দিন।")

    base_s = base_settings(clients_text)

    # ---- লিংক দিলেই ভিডিওর সময় দেখানো ----
    cache = st.session_state["info_cache"]
    for u in urls:
        if u not in cache:
            with st.spinner(f"তথ্য আনা হচ্ছে... {u[:60]}"):
                try:
                    cache[u] = {"ok": True, **core.get_info(u, base_s)}
                except Exception as e:
                    cache[u] = {"ok": False, "error": friendly(e)}
    if urls:
        good = [cache[u] for u in urls if cache[u].get("ok")]
        bad = [u for u in urls if not cache[u].get("ok")]
        total_dur = sum(g["duration"] for g in good)
        if good:
            label = "ভিডিও" if len(urls) == 1 else f"মোট ({len(good)}টি ভিডিও)"
            st.markdown(f"<div class='durbox'>⏱ {label}: {core.fmt_time(total_dur)}</div>", unsafe_allow_html=True)
        for u in urls:
            g = cache[u]
            if g.get("ok"):
                ca, cb = st.columns([1, 6])
                if g.get("thumbnail"):
                    ca.image(g["thumbnail"], width=96)
                cb.markdown(f"**{g['title']}**  \n{g['uploader']} • ⏱ {core.fmt_time(g['duration'])}"
                            + ("  \n⚠ **এই ভিডিও সীমার চেয়ে লম্বা, কনভার্ট হবে না**" if g["duration"] > MAX_MINUTES * 60 else ""))
            else:
                st.error(f"{u[:70]} → {g['error']}")
        if bad and st.button("🔄 ব্যর্থ লিংকের তথ্য আবার আনুন"):
            for u in bad:
                cache.pop(u, None)
            st.rerun()

    settings = {
        **base_s,
        "codec": fmt["codec"], "quality": quality,
        "cover": bool(use_cover and fmt["cover"]), "size": core.COVER_SIZES[cover_label], "square": bool(crop),
        "smart": bool(smart), "meta": True, "turbo": bool(turbo), "ultra": bool(ultra),
        "max_minutes": MAX_MINUTES,
    }

    go = st.button("⬇  Convert to Audio", type="primary", disabled=not urls)

    # =================================================================
    #  কনভার্ট চালানো
    # =================================================================
    if go and urls:
        if not ENV["ffmpeg"]:
            st.error("FFmpeg পাওয়া যায়নি। রিপোজিটরিতে packages.txt-এ `ffmpeg` আছে কি না দেখুন।")
            st.stop()

        core.cleanup_old(BASE_DIR)
        lock = ENV["lock"]
        ph_stop = st.empty()
        ph_prog = st.progress(0.0, text="0%")
        ph_status = st.empty()
        ph_timer = st.empty()

        waited = 0
        while not lock.acquire(blocking=False):
            ph_status.info("⏳ সার্ভারে অন্য একটি কাজ চলছে, শেষ হলে আপনারটা শুরু হবে...")
            time.sleep(1)
            waited += 1
            if waited > 300:
                st.error("অনেকক্ষণ অপেক্ষা করেও সার্ভার ফাঁকা হয়নি। একটু পরে আবার চেষ্টা করুন।")
                st.stop()
        ph_status.empty()

        workdir = tempfile.mkdtemp(prefix="job_", dir=BASE_DIR)
        cancel = threading.Event()
        n = len(urls)
        t_run = time.time()
        ok_count, fail_msgs = 0, []
        try:
            ph_stop.button("⏹ Stop (বন্ধ করুন)", key="stop_btn",
                           help="চাপলে চলমান কাজ থেমে যাবে।")
            for i, u in enumerate(urls):
                info = cache.get(u, {})
                if info.get("ok") and info["duration"] > MAX_MINUTES * 60:
                    fail_msgs.append(f"({i + 1}/{n}) {info['title'][:50]} → সীমার চেয়ে লম্বা, বাদ দেওয়া হয়েছে")
                    continue

                def on_update(frac, text, i=i):
                    overall = max(0.0, min((i + frac) / n, 1.0))
                    ph_prog.progress(overall, text=f"{overall * 100:.0f}%")
                    ph_status.markdown(f"**({i + 1}/{n})** {text}")
                    ph_timer.markdown(f"<span class='timer'>⏱ {core.fmt_time(time.time() - t_run)}</span>",
                                      unsafe_allow_html=True)

                s_item = dict(settings, date=today_str() if date_on else "")
                try:
                    res = core.process_url(u, s_item, workdir, i, on_update, cancel)
                except core.TooLong as e:
                    fail_msgs.append(f"({i + 1}/{n}) {e}")
                    continue
                except core.Cancelled:
                    fail_msgs.append("বাতিল করা হয়েছে।")
                    break
                except Exception as e:   # noqa
                    fail_msgs.append(f"({i + 1}/{n}) ব্যর্থ: {friendly(e)}")
                    continue

                ok_count += 1
                st.session_state["results"].append(res)
                st.session_state["history"].append({
                    "সময়": time.strftime("%H:%M:%S"), "টাইটেল": res["title"][:80], "ফরম্যাট": res["codec"].upper(),
                    "অডিও": core.fmt_time(res["duration"]), "লেগেছে": core.fmt_time(res["t_total"]),
                    "duration": res["duration"],
                })
                st.session_state["last_took"] = res["t_total"]
                prune_results()
        except BaseException:
            cancel.set()   # Stop চাপলে / সেশন বন্ধ হলে ডাউনলোড ও ffmpeg থামানো
            raise
        finally:
            lock.release()

        total_s = time.time() - t_run
        ph_stop.empty()
        ph_prog.progress(1.0, text="100%")
        ph_timer.markdown(f"<span class='timer'>⏱ মোট {core.fmt_time(total_s)}</span>", unsafe_allow_html=True)
        if ok_count and not fail_msgs:
            ph_status.success(f"✔ সব সম্পন্ন! {ok_count}টি ফাইল তৈরি হয়েছে (নিচ থেকে ডাউনলোড করুন)")
        elif ok_count:
            ph_status.warning(f"{ok_count}টি সফল, {len(fail_msgs)}টি ব্যর্থ")
        else:
            ph_status.error("কোনো ফাইল তৈরি হয়নি")
        for m in fail_msgs:
            st.error(m)

    # =================================================================
    #  ফলাফল / ডাউনলোড
    # =================================================================
    results = st.session_state["results"]
    if results:
        st.markdown("### 📥 তৈরি ফাইল")
        for r in reversed(results):
            with st.container(border=True):
                st.markdown(f"**{r['name']}**")
                t_ext = f"তথ্য {r['t_extract']:.1f}s • " if r.get("t_extract") is not None else ""
                st.caption(f"{r['codec'].upper()} • অডিও {core.fmt_time(r['duration'])} • {core.fmt_size(r['size'])}"
                           f" • ⏱ {t_ext}ডাউনলোড {r['t_download']:.1f}s • কনভার্ট {r['t_convert']:.1f}s"
                           f" • মোট {r['t_total']:.1f}s")
                for note in r["notes"]:
                    st.caption(note)
                if os.path.isfile(r["path"]):
                    with open(r["path"], "rb") as fh:
                        st.download_button("⬇ ডাউনলোড করুন", data=fh.read(), file_name=r["name"],
                                           mime=MIME.get(r["codec"], "application/octet-stream"),
                                           key=f"dl_{r['path']}", type="primary")
                else:
                    st.caption("⌛ ফাইলের মেয়াদ শেষ (সার্ভার থেকে মুছে গেছে)। আবার কনভার্ট করুন।")
        if st.button("🧹 সব ফাইল মুছুন (সার্ভার থেকে)"):
            for r in results:
                try:
                    os.remove(r["path"])
                except OSError:
                    pass
            st.session_state["results"] = []
            st.rerun()

# =====================================================================
#  History
# =====================================================================
with tab_hist:
    if not hist:
        st.info("এই সেশনে এখনো কিছু কনভার্ট করা হয়নি। (ব্রাউজার রিফ্রেশ করলে হিস্টরি মুছে যায়।)")
    else:
        st.dataframe([{k: v for k, v in h.items() if k != "duration"} for h in reversed(hist)],
                     hide_index=True)
        if st.button("History মুছুন"):
            st.session_state["history"] = []
            st.rerun()

# =====================================================================
#  Help / Server
# =====================================================================
with tab_help:
    st.markdown(f"""
**এই সার্ভারের সীমা (ফ্রি Streamlit Community Cloud):** আনুমানিক ২ CPU কোর ও ~২.৭ GB RAM। তাই:
- একসাথে একটাই কাজ চলে, বাকিরা অপেক্ষা করে।
- প্রতি ভিডিও সর্বোচ্চ **{MAX_MINUTES // 60} ঘণ্টা {MAX_MINUTES % 60} মিনিট**; একবারে সর্বোচ্চ {MAX_LINKS}টি লিংক।
- প্লেলিস্ট সমর্থিত নয় (একটি একটি ভিডিওর লিংক দিন)।
- ১২ ঘণ্টা কেউ না ঢুকলে অ্যাপ ঘুমিয়ে যায়; আবার ঢুকলে "Yes, get this app back up!" চাপুন।
- তৈরি ফাইল সার্ভারে অল্প সময় থাকে, তাই কনভার্ট শেষে সঙ্গে সঙ্গে ডাউনলোড করে নিন।

**YouTube "Sign in to confirm you're not a bot" দেখালে:** ক্লাউড সার্ভারের IP-কে YouTube প্রায়ই ব্লক করে।
README-র কুকি অংশ অনুযায়ী `YT_COOKIES` সেট করুন। এটা সবসময় কাজ করার নিশ্চয়তা নেই।
""")
    with st.expander("🔧 রক্ষণাবেক্ষণ"):
        st.write(f"yt-dlp সংস্করণ: **{core.ytdlp_version()}**")
        if st.button("yt-dlp আপডেটের চেষ্টা করুন"):
            with st.spinner("আপডেট হচ্ছে..."):
                ok, out = core.update_ytdlp()
            (st.success if ok else st.error)("সম্পন্ন - এখন Manage app → Reboot করুন।" if ok else "আপডেট হয়নি।")
            st.code(out)
        st.caption("YouTube বদলালে yt-dlp আপডেট জরুরি। সবচেয়ে নিশ্চিত উপায়: GitHub-এ যেকোনো ছোট কমিট করলে "
                   "বা Streamlit-এর Reboot দিলে নতুন সংস্করণ বসে।")
