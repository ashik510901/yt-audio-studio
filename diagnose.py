"""
diagnose.py - YouTube ডাউনলোড কেন ব্যর্থ হচ্ছে (403 / PO Token / IP ব্লক) টার্মিনালে পরীক্ষা করুন।
কিছু ডাউনলোড বা কনভার্ট করে না; প্রতিটি পদ্ধতিতে ডাউনলোড-লিংকে শুধু প্রথম বাইট চেয়ে দেখে।

ব্যবহার:
    python diagnose.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python diagnose.py "<লিংক>" --browser firefox        # নিজের পিসির ব্রাউজার-কুকি দিয়ে
    python diagnose.py "<লিংক>" --cookies cookies.txt    # Netscape cookies.txt দিয়ে

একই ভিডিওতে নিজের পিসিতে ও Streamlit Cloud-এর Help → Diagnostics-এ চালিয়ে ফল মিলিয়ে দেখুন।
"""
import argparse
import sys

import core


def main():
    ap = argparse.ArgumentParser(description="YouTube ডাউনলোড ডায়াগনস্টিক")
    ap.add_argument("url", help="YouTube ভিডিওর লিংক")
    ap.add_argument("--cookies", help="Netscape cookies.txt ফাইলের পাথ")
    ap.add_argument("--browser", choices=["firefox", "edge", "chrome", "brave"], help="ব্রাউজার থেকে কুকি নিন")
    ap.add_argument("--proxy", help="প্রক্সি (যেমন http://host:port)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("Deno (JavaScript রানটাইম) প্রস্তুত হচ্ছে...")
    core.ensure_deno()
    s = {"cookies": args.cookies, "browser": args.browser, "proxy": args.proxy}
    print("পরীক্ষা চলছে... (১ মিনিটের মতো লাগতে পারে)\n")
    print(core.format_report(core.diagnose(args.url, s)))


if __name__ == "__main__":
    main()
