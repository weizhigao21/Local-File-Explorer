"""内置网页服务：手机/平板用浏览器直接听音乐、看写真、看视频

为什么用标准库 `http.server` 而不是 Flask/FastAPI：
- 不增加打包体积与依赖，PyInstaller 不用额外 hiddenimports；
- 需要的只有「静态文件 + JSON + 带 Range 的媒体流」，框架能力全用不上；
- 纯 Python、无 Qt，能被单测直接起在临时端口上跑端到端请求。

安全模型（家里局域网，不假设有公网）：
1. **token 鉴权**：首次打开 `/?t=xxx` 种 cookie，之后同源请求自动带上；
2. **媒体只按数据库 id 取**：前端传不进路径，目录穿越无从谈起；
3. 默认只监听局域网地址，可在设置里改成「仅本机」。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import traceback
import urllib.parse
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resource_manager import config
from video_manager import probe
from web_server import http_util, library

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

COOKIE_NAME = "lrm_token"

# 上报进度这类小 JSON 的上限，防止有人拿它当上传口
MAX_JSON_BODY = 64 * 1024

# 转发给 ffmpeg 的块大小
RELAY_CHUNK = 64 * 1024

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "SAMEORIGIN"),
)

# 只允许本地资源：不允许外链脚本，避免网页被换成任意内容
CSP_HTML = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
)


class WebServerError(RuntimeError):
    """开服失败（端口占用/权限不足），消息可直接展示给用户"""


# ==================== 路由表 ====================

_ROUTE_DEFS = [
    ("GET", r"^/api/summary$", "api_summary"),
    # 写真
    ("GET", r"^/api/photos/authors$", "api_photo_authors"),
    ("GET", r"^/api/photos/works$", "api_photo_works"),
    ("GET", r"^/api/photos/works/(\d+)/images$", "api_photo_images"),
    # 音频
    ("GET", r"^/api/audio/playlists$", "api_audio_playlists"),
    ("GET", r"^/api/audio/playlists/(\d+)$", "api_audio_playlist"),
    ("GET", r"^/api/audio/tracks/(\d+)/subtitles$", "api_audio_subtitles"),
    # 视频
    ("GET", r"^/api/video/authors$", "api_video_authors"),
    ("GET", r"^/api/video/works$", "api_video_works"),
    ("GET", r"^/api/video/works/(\d+)/videos$", "api_video_work_videos"),
    ("GET", r"^/api/video/videos/(\d+)$", "api_video_info"),
    ("POST", r"^/api/video/videos/(\d+)/progress$", "api_video_save_progress"),
    # 媒体
    ("GET", r"^/media/photo/(\d+)$", "media_photo"),
    ("GET", r"^/media/photo-cover/(\d+)$", "media_photo_cover"),
    ("GET", r"^/media/audio/(\d+)$", "media_audio"),
    ("GET", r"^/media/audio-cover/(\d+)$", "media_audio_cover"),
    ("GET", r"^/media/video/(\d+)$", "media_video"),
    ("GET", r"^/media/video-remux/(\d+)$", "media_video_remux"),
    ("GET", r"^/media/video-cover/(\d+)$", "media_video_cover"),
]

_ROUTES = [(method, re.compile(pattern), name) for method, pattern, name in _ROUTE_DEFS]


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalResourceManager"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # 手机滑动一次会发几十个请求，逐条打日志会把控制台刷满
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        print(f"[网页服务] {fmt % args}")

    # ---------- HTTP 方法 ----------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---------- 分发 ----------

    def _dispatch(self, method):
        self._extra_headers = []
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)

            if not self._authorized(path, query):
                return self._send_unauthorized(path)

            if path in ("/", "/index.html"):
                return self._serve_static("index.html", max_age=0, html=True)
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):], max_age=86400)
            if path == "/favicon.ico":
                return self._serve_static("icon.svg", max_age=86400)

            for route_method, pattern, handler_name in _ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    return getattr(self, handler_name)(match, query)

            return self._json({"error": "没有这个接口"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            # 手机上快速滑走/拖动进度条会主动断连，不是错误
            return None
        except Exception as e:  # noqa: BLE001 - 单个请求出错不能拖垮服务线程
            traceback.print_exc()
            return self._json({"error": f"服务器内部错误: {e}"}, 500)

    # ---------- 鉴权 ----------

    def _authorized(self, path, query):
        token = getattr(self.server, "token", "") or ""
        if not token:
            return True

        provided = ""
        from_query = (query.get("t") or [""])[0]
        if from_query:
            provided = from_query
        if not provided:
            provided = self.headers.get("X-Token", "") or ""
        if not provided:
            for chunk in (self.headers.get("Cookie", "") or "").split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == COOKIE_NAME:
                    provided = value
                    break

        if not hmac.compare_digest(provided, token):
            return False

        if from_query:
            # 从链接/二维码进来的：顺手种 cookie，后面 <img>/<video> 才能免参数加载
            self._extra_headers.append(
                ("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; Max-Age=31536000; SameSite=Lax")
            )
        return True

    def _send_unauthorized(self, path):
        if path.startswith(("/api/", "/media/")):
            return self._json({"error": "需要访问令牌"}, 401)
        body = _LOGIN_HTML.encode("utf-8")
        self._send_headers(
            401,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        if self.command != "HEAD":
            self.wfile.write(body)
        return None

    # ---------- 响应基础设施 ----------

    def _send_headers(self, status, headers):
        self.send_response(status)
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in headers:
            self.send_header(key, value)
        for key, value in self._extra_headers:
            self.send_header(key, value)
        self.end_headers()

    def _json(self, data, status=200, max_age=0):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cache = "no-store" if not max_age else f"private, max-age={max_age}"
        self._send_headers(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", cache),
            ],
        )
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_static(self, rel, max_age=3600, html=False):
        full = http_util.static_file_path(STATIC_DIR, rel)
        if not full:
            return self._json({"error": "文件不存在"}, 404)
        content_type = http_util.guess_content_type(full)
        if html:
            self._extra_headers.append(("Content-Security-Policy", CSP_CONTENT))
        return self._serve_file(full, content_type=content_type, max_age=max_age)

    def _serve_file(self, path, content_type=None, max_age=3600):
        """带 Range 的文件响应：视频拖动、大图分段下载都靠它"""
        try:
            stat = os.stat(path)
        except OSError:
            return self._json({"error": "文件不存在"}, 404)
        size = int(stat.st_size)
        etag = http_util.etag_for(path)

        cache = "no-store" if not max_age else f"private, max-age={max_age}"

        # 图片墙/封面重复请求多，命中 ETag 直接 304，省流量也省解码
        if etag and self.headers.get("If-None-Match") == etag:
            return self._send_headers(304, [("ETag", etag), ("Cache-Control", cache)])

        headers = [
            ("Content-Type", content_type or http_util.guess_content_type(path)),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", cache),
            ("ETag", etag),
            ("Last-Modified", self.date_time_string(stat.st_mtime)),
        ]

        if self.headers.get("Range"):
            try:
                parsed = http_util.parse_range(self.headers.get("Range"), size)
            except http_util.RangeNotSatisfiable:
                return self._send_headers(
                    416,
                    [
                        ("Content-Range", f"bytes */{size}"),
                        ("Content-Length", "0"),
                        ("Cache-Control", "no-store"),
                    ],
                )
        else:
            parsed = None

        if parsed is None:
            start, end = 0, size - 1
            status = 200
            headers.append(("Content-Length", str(max(size, 0))))
        else:
            start, end = parsed
            status = 206
            headers.append(("Content-Range", http_util.content_range_header(start, end, size)))
            headers.append(("Content-Length", str(end - start + 1)))

        self._send_headers(status, headers)
        if self.command == "HEAD" or size == 0:
            return None

        remaining = end - start + 1
        with open(path, "rb") as file_obj:
            file_obj.seek(start)
            while remaining > 0:
                chunk = file_obj.read(min(http_util.CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return None

    # ---------- API ----------

    def api_summary(self, match, query):
        return self._json(library.summary())

    def api_photo_authors(self, match, query):
        return self._json({"authors": library.photo_authors()}, max_age=30)

    def api_photo_works(self, match, query):
        # 0 会被当成「作者 id = 0」查空集，必须转成 None 表示不过滤
        author_id = _int_param(query, "author_id") or None
        return self._json(
            {"works": library.photo_works(author_id, _text_param(query, "q"))}, max_age=30
        )

    def api_photo_images(self, match, query):
        data = library.photo_work_images(int(match.group(1)))
        if data is None:
            return self._json({"error": "作品不存在"}, 404)
        return self._json(data, max_age=30)

    def api_audio_playlists(self, match, query):
        parent = _text_param(query, "parent")
        return self._json({"playlists": library.audio_playlists(parent)}, max_age=30)

    def api_audio_playlist(self, match, query):
        data = library.audio_playlist(int(match.group(1)))
        if data is None:
            return self._json({"error": "歌单不存在"}, 404)
        return self._json(data, max_age=10)

    def api_audio_subtitles(self, match, query):
        data = library.audio_subtitle_cues(int(match.group(1)))
        if data is None:
            return self._json({"cues": []}, 200)
        return self._json(data, max_age=60)

    def api_video_authors(self, match, query):
        return self._json({"authors": library.video_authors()}, max_age=30)

    def api_video_works(self, match, query):
        author_id = _int_param(query, "author_id") or None
        return self._json(
            {"works": library.video_works(author_id, _text_param(query, "q"))}, max_age=30
        )

    def api_video_work_videos(self, match, query):
        data = library.video_work_videos(int(match.group(1)))
        if data is None:
            return self._json({"error": "作品不存在"}, 404)
        return self._json(data)

    def api_video_info(self, match, query):
        data = library.video_play_info(int(match.group(1)))
        if data is None:
            return self._json({"error": "视频不存在"}, 404)
        return self._json(data)

    def api_video_save_progress(self, match, query):
        length = http_util.parse_byte_size(self.headers.get("Content-Length"))
        if length > MAX_JSON_BODY:
            return self._json({"error": "请求体过大"}, 413)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "请求体必须是对象"}, 400)

        ok = library.save_video_progress(
            int(match.group(1)),
            payload.get("position_ms", 0),
            payload.get("duration_ms", 0),
        )
        if not ok:
            return self._json({"error": "视频不存在"}, 404)
        return self._json({"ok": True})

    # ---------- 媒体 ----------

    def media_photo(self, match, query):
        path = library.photo_image_path(int(match.group(1)))
        if not path or not os.path.isfile(path):
            return self._json({"error": "图片不存在"}, 404)
        width = _int_param(query, "w") or 0
        served = library.scaled_photo(path, width) if width else path
        return self._serve_file(served, max_age=86400)

    def media_photo_cover(self, match, query):
        path = library.photo_cover(int(match.group(1)))
        if not path:
            return self._json({"error": "没有封面"}, 404)
        return self._serve_file(path, content_type="image/jpeg", max_age=86400)

    def media_audio(self, match, query):
        track = library.audio_track(int(match.group(1)))
        if not track or not os.path.isfile(track["path"]):
            return self._json({"error": "音频不存在"}, 404)
        # 音频也要支持 Range：iOS 上 <audio> 没有 206 就无法 seek
        return self._serve_file(track["path"], max_age=3600)

    def media_audio_cover(self, match, query):
        path = library.audio_cover_path(int(match.group(1)))
        if not path:
            return self._json({"error": "没有封面"}, 404)
        return self._serve_file(path, max_age=86400)

    def media_video(self, match, query):
        video = library.video_by_id(int(match.group(1)))
        if not video or not os.path.isfile(video["path"]):
            return self._json({"error": "视频不存在"}, 404)
        return self._serve_file(video["path"], max_age=600)

    def media_video_cover(self, match, query):
        path = library.video_cover(int(match.group(1)))
        if not path:
            return self._json({"error": "没有封面"}, 404)
        return self._serve_file(path, content_type="image/jpeg", max_age=86400)

    def media_video_remux(self, match, query):
        """浏览器不认的容器（mkv/avi…）走 ffmpeg 转封装成 fMP4 直接吐给前端

        只转封装不重编码画面（`-c:v copy`），CPU 占用很低；音频统一转 AAC，
        否则 DTS/AC3/FLAC 音轨塞进 mp4 后浏览器大概率没声音。

        代价：输出不可 seek（管道流），前端会明确提示「此模式不能拖动」。
        """
        video = library.video_by_id(int(match.group(1)))
        if not video or not os.path.isfile(video["path"]):
            return self._json({"error": "视频不存在"}, 404)
        ffmpeg = probe.find_tool("ffmpeg")
        if not ffmpeg:
            return self._json({"error": "服务器未安装 ffmpeg，无法转封装"}, 503)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        cmd = [
            ffmpeg, "-nostdin", "-loglevel", "error",
            "-i", video["path"],
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        try:
            proc = subprocess.Popen(  # noqa: S603 - 参数为内部路径，无 shell
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as e:
            return self._json({"error": f"启动 ffmpeg 失败: {e}"}, 500)

        # 没有 Content-Length 的流式响应：告诉客户端「读到连接关闭为止」
        self.close_connection = True
        self._send_headers(
            200,
            [
                ("Content-Type", "video/mp4"),
                ("Cache-Control", "no-store"),
                ("Accept-Ranges", "none"),
            ],
        )
        try:
            while True:
                chunk = proc.stdout.read(RELAY_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
            with suppress(OSError):
                proc.stdout.close()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        return None


# ==================== 服务实例 ====================

class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, token=""):
        self.token = token
        super().__init__(address, handler)


class WebServer:
    """一个进程内只有一个实例（`get_server()`），窗口与 main 共用同一份状态"""

    def __init__(self):
        self._httpd = None
        self._thread = None
        self.port = 0
        self.host = ""
        self.token = ""
        self.last_error = ""

    @property
    def running(self):
        return self._httpd is not None

    def start(self, port=None, lan=None, token=None):
        """启动服务，返回实际端口；失败抛 WebServerError

        token 传空 = 用配置里保存的令牌（没有就生成一个）；测试可显式指定。
        """
        if self._httpd is not None:
            return self.port

        errors = library.init_all()
        for message in errors:
            print(f"[网页服务] 初始化数据库失败 {message}")

        port = int(port if port is not None else config.WEB_PORT)
        if lan is None:
            lan = bool(getattr(config, "WEB_ALLOW_LAN", True))
        host = "0.0.0.0" if lan else "127.0.0.1"

        self.token = token or ensure_token()
        try:
            httpd = _ThreadingServer((host, port), RequestHandler, self.token)
        except OSError as e:
            self.last_error = _bind_error_message(e, port)
            raise WebServerError(self.last_error) from e

        self._httpd = httpd
        self.host = host
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="web-server",
            daemon=True,
        )
        self._thread.start()
        print(f"[网页服务] 已启动 http://{host}:{self.port}/")
        return self.port

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        # 关服失败不该弹窗：端口已经释放不了也只能等进程退出
        with suppress(Exception):
            httpd.shutdown()
        with suppress(Exception):
            httpd.server_close()
        self._thread = None
        print("[网页服务] 已停止")

    def url(self, host="127.0.0.1", with_token=True):
        port = self.port or int(config.WEB_PORT)
        return build_url(host, port, self.token, with_token)


_server = None
_server_lock = threading.Lock()


def get_server():
    global _server
    with _server_lock:
        if _server is None:
            _server = WebServer()
        return _server


def ensure_token():
    """取访问令牌，没有就生成一个并写进 config.json（重启后手机不用重新登录）"""
    token = str(getattr(config, "WEB_TOKEN", "") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(9)
    config.set_user_config("WEB_TOKEN", token)
    config.save_user_config()
    return token


def build_url(host, port, token="", with_token=True):
    base = f"http://{host}:{int(port)}/"
    if with_token and token:
        return f"{base}?t={urllib.parse.quote(token)}"
    return base


def lan_addresses():
    """本机局域网 IPv4 列表（优先 192.168 私有段，其次 10 / 172）"""
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass

    if not ips:
        # 兜底：UDP connect 不会真的发包，只是让系统选一张出口网卡
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()

    ips = {ip for ip in ips if not ip.startswith(("127.", "169.254.")) and ip != "0.0.0.0"}
    return sorted(ips, key=_address_rank)


def _address_rank(ip):
    if ip.startswith("192.168."):
        return (0, ip)
    if ip.startswith("10."):
        return (1, ip)
    if ip.startswith("172."):
        return (2, ip)
    return (3, ip)


def _bind_error_message(error, port):
    code = getattr(error, "errno", None)
    if code in (10048, 48):  # WSAEADDRINUSE / EADDRINUSE
        return f"端口 {port} 已被占用，换一个端口（在下方设置里改）再试"
    if code in (10013, 13):
        return f"没有权限监听端口 {port}（1024 以下端口需要管理员权限）"
    return f"启动失败：{error}"


def _int_param(query, name, default=0):
    values = query.get(name) or []
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def _text_param(query, name):
    values = query.get(name) or []
    return values[0] if values else ""


CSP_CONTENT = CSP_HTML

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>需要访问令牌</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #17130f; color: #efe6da; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .box { width: min(88vw, 380px); padding: 28px 24px; border-radius: 16px;
         background: #211b15; border: 1px solid #3a2f25; }
  h1 { font-size: 18px; margin: 0 0 8px; }
  p { font-size: 13px; line-height: 1.7; color: #a2937f; margin: 0 0 18px; }
  input { width: 100%; box-sizing: border-box; padding: 12px 14px; font-size: 16px;
          border-radius: 10px; border: 1px solid #4a3b2d; background: #17130f; color: #efe6da; }
  button { width: 100%; margin-top: 12px; padding: 12px; font-size: 15px; border: 0;
           border-radius: 10px; background: #d4886a; color: #221a14; font-weight: 600; }
</style>
</head>
<body>
  <form class="box" method="get" action="/">
    <h1>需要访问令牌</h1>
    <p>请在电脑的「手机访问」窗口里复制完整链接，或把链接里的令牌粘贴到下面。</p>
    <input name="t" placeholder="访问令牌" autocomplete="off" autocapitalize="off" spellcheck="false" autofocus>
    <button type="submit">进入</button>
  </form>
</body>
</html>
"""
