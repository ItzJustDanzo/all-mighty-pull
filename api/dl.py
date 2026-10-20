from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import re
import urllib.request

import yt_dlp

# Only these sites are accepted, so the endpoint can't be used as an open proxy.
ALLOWED = (
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "snapchat.com", "youtube.com", "youtu.be",
)


def is_allowed(url):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(
        host == d or host.endswith("." + d) for d in ALLOWED
    )


def extract(url):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        try:
            cookie = ydl._calc_cookies(url)
        except Exception:
            cookie = ""
    return info, cookie


def is_direct(f):
    return f.get("protocol") in ("https", "http") and f.get("url")


def pick_formats(info):
    formats = info.get("formats") or []
    best_by_height = {}
    for f in formats:
        if not is_direct(f):
            continue
        if f.get("vcodec", "none") != "none" and f.get("acodec", "none") != "none":
            h = f.get("height") or 0
            cur = best_by_height.get(h)
            if cur is None or (f.get("tbr") or 0) > (cur.get("tbr") or 0):
                best_by_height[h] = f
    videos = sorted(best_by_height.values(), key=lambda f: f.get("height") or 0, reverse=True)
    out = []
    for f in videos:
        h = f.get("height")
        out.append({
            "id": f["format_id"],
            "label": f"{h}p video" if h else "Video",
            "ext": f.get("ext") or "mp4",
            "size": f.get("filesize") or f.get("filesize_approx"),
        })
    if not out and info.get("url"):
        out.append({"id": "best", "label": "Video", "ext": info.get("ext") or "mp4", "size": None})

    audios = [f for f in formats if is_direct(f)
              and f.get("vcodec", "none") == "none" and f.get("acodec", "none") != "none"]
    if audios:
        a = max(audios, key=lambda f: f.get("abr") or 0)
        out.append({
            "id": a["format_id"],
            "label": "Audio only",
            "ext": a.get("ext") or "m4a",
            "size": a.get("filesize") or a.get("filesize_approx"),
        })
    return out


def find_format(info, fid):
    for f in info.get("formats") or []:
        if f.get("format_id") == fid and is_direct(f):
            return f
    if fid == "best" and info.get("url"):
        return info
    return None


def safe_name(title, ext):
    base = re.sub(r"[^\w\- ]+", "", title or "video").strip()[:80] or "video"
    return f"{base}.{ext}"


class handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        url = (q.get("url") or [""])[0].strip()
        fid = (q.get("fid") or [""])[0]

        if not url or not is_allowed(url):
            return self._json(400, {"error": "Paste a link from TikTok, Instagram, Facebook, Snapchat or YouTube."})

        try:
            info, cookie = extract(url)
        except Exception as e:
            msg = str(e)
            if "Sign in" in msg or "login" in msg.lower() or "private" in msg.lower():
                msg = "This video needs a login or is private, so it can't be downloaded."
            else:
                msg = "Couldn't read that link. It may be private, removed, or blocked for this server."
            return self._json(422, {"error": msg})

        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]

        if not fid:
            return self._json(200, {
                "title": info.get("title") or "Video",
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "formats": pick_formats(info),
            })

        f = find_format(info, fid)
        if not f:
            return self._json(404, {"error": "That quality is no longer available. Try again."})

        headers = dict(f.get("http_headers") or info.get("http_headers") or {})
        if cookie:
            headers["Cookie"] = cookie
        try:
            upstream = urllib.request.urlopen(
                urllib.request.Request(f["url"], headers=headers), timeout=25
            )
        except Exception:
            return self._json(502, {"error": "The platform refused the download from this server."})

        ext = f.get("ext") or "mp4"
        self.send_response(200)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
        if upstream.headers.get("Content-Length"):
            self.send_header("Content-Length", upstream.headers["Content-Length"])
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name(info.get("title"), ext)}"')
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            upstream.close()