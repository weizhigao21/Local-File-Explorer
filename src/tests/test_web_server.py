"""网页服务（web_server）守卫：Range、鉴权、路径安全、数据视图、真实 HTTP 链路

这里刻意分成两层：
- 纯函数（Range / MIME / 静态路径 / VTT）用参数化单测钉死，跑得飞快；
- 端到端用标准库 urllib 打真实端口，覆盖「手机到底能不能取到字节」这件事——
  只测函数不测链路的话，`Content-Range` 拼错、HEAD 多发一个包这类问题照样漏。

不依赖 ffmpeg：封面抽帧用桩替换，测试机没装 ffmpeg 也能全绿。
"""
import ast
import json
import os
import re
import urllib.error
import urllib.request
import wave

import pytest

from audio_manager import database as audio_db
from resource_manager import config
from resource_manager import database as photo_db
from video_manager import database as video_db
from web_server import http_util, library
from web_server.app import WebServer, build_url, ensure_token

TOKEN = "testtoken9x"


# ==================== 夹具 ====================

@pytest.fixture
def env(tmp_path, monkeypatch):
    """三个模块各一套临时库 + 最小媒体文件，全部指向 tmp_path"""
    photo_root = tmp_path / "写真"
    photo_root.mkdir()
    video_root = tmp_path / "视频"
    video_root.mkdir()

    monkeypatch.setattr(config, "PHOTO_ROOT", str(photo_root))
    monkeypatch.setattr(config, "PHOTO_ROOTS", [str(photo_root)])
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "photo.db"))
    monkeypatch.setattr(config, "THUMBNAIL_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "WEB_CACHE_DIR", str(tmp_path / "web_cache"))
    monkeypatch.setattr(config, "VIDEO_ROOT", str(video_root))
    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(video_root)])
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    monkeypatch.setattr(config, "VIDEO_THUMB_DIR", str(tmp_path / "video_thumbs"))
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "WEB_TOKEN", TOKEN)
    monkeypatch.setattr(audio_db, "DB_PATH", str(tmp_path / "audio.db"))

    # 抽帧不真跑：桩只写一个假文件，验证「封面缺失时会去生成」即可
    def fake_thumbnail(video_path, out_path, at_ms=0):  # noqa: ARG001 - 签名对齐
        if "missing" in video_path:
            return None
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as handle:
            handle.write(b"\xff\xd8thumbnail")
        return out_path

    monkeypatch.setattr(library.probe, "make_thumbnail", fake_thumbnail)

    photo_db.init_db()
    audio_db.init_db()
    video_db.init_db()

    workspace = {"root": tmp_path, "photo_root": photo_root, "video_root": video_root}
    yield workspace

    video_db.end_persistent()
    photo_db.end_persistent()
    audio_db.end_persistent()


def _make_image(path, size=(240, 320), color=(184, 92, 62)):
    from PIL import Image

    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=90)
    return path


@pytest.fixture
def seeded(env):
    """一份最小的三模块数据：1 个写真作品、1 个歌单 1 首、1 个视频作品 2 个视频"""
    tmp = env["root"]

    image_path = _make_image(str(env["photo_root"] / "作者A" / "作品1" / "01.jpg"))
    photo_db.add_author_with_works(
        "作者A", [("作品1", str(env["photo_root"] / "作者A" / "作品1"), "", [(image_path, 0)])]
    )

    wav_path = str(tmp / "audio" / "01.wav")
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)
    with wave.open(wav_path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x01" * 8000)
    playlist_id = audio_db.add_playlist("歌单A", str(tmp / "audio"))
    audio_db.add_tracks(playlist_id, [("第一首", wav_path, 2.0, 1)])
    audio_db.update_playlist(playlist_id, track_count=1)

    work_dir = env["video_root"] / "作者V" / "作品V"
    work_dir.mkdir(parents=True)
    mp4 = work_dir / "01.mp4"
    mkv = work_dir / "02.mkv"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400)
    mkv.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 400)
    video_db.add_author_with_works(
        "作者V",
        [
            (
                "作品V",
                str(work_dir),
                "",
                [
                    (str(mp4), "01.mp4", 0, mp4.stat().st_size),
                    (str(mkv), "02.mkv", 0, mkv.stat().st_size),
                ],
            )
        ],
    )
    video_db.update_video_meta(str(mp4), duration_ms=600000, width=1280, height=720, codec="h264")
    video_db.update_video_meta(str(mkv), duration_ms=600000, width=1280, height=720, codec="h264")
    return env


@pytest.fixture
def server(seeded):
    instance = WebServer()
    port = instance.start(port=0, lan=False, token=TOKEN)
    yield instance, port
    instance.stop()


def _get(port, path, cookie=None, headers=None, method="GET", body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=body)
    if cookie:
        request.add_header("Cookie", f"lrm_token={cookie}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


# ==================== Range 解析 ====================

@pytest.mark.parametrize("header,size,expected", [
    ("bytes=0-99", 1000, (0, 99)),
    ("bytes=100-", 1000, (100, 999)),
    ("bytes=-300", 1000, (700, 999)),
    ("bytes=-5000", 1000, (0, 999)),        # 后缀比文件长：整份返回
    ("bytes=0-999999", 1000, (0, 999)),     # 末尾超界：夹到文件尾
    ("bytes=990-1200", 1000, (990, 999)),
    ("bytes=0-0", 1, (0, 0)),
    ("bytes=5-5", 10, (5, 5)),
    ("Bytes=10-20", 100, (10, 20)),         # 大小写不敏感
    ("bytes=0-9,20-29", 100, (0, 9)),       # 多段只取第一段
])
def test_parse_range(header, size, expected):
    assert http_util.parse_range(header, size) == expected


@pytest.mark.parametrize("header", [
    None, "", "items=0-1", "bytes=", "bytes=abc-def", "bytes=10-lots", "bytes=--5",
])
def test_parse_range_ignores_malformed(header):
    """格式不合法一律当没传 Range：回 200 整份，而不是 416 让播放器卡住"""
    assert http_util.parse_range(header, 1000) is None


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=2000-3000", "bytes=50-10", "bytes=-0"])
def test_parse_range_rejects_unsatisfiable(header):
    with pytest.raises(http_util.RangeNotSatisfiable):
        http_util.parse_range(header, 1000)


def test_parse_range_empty_file():
    with pytest.raises(http_util.RangeNotSatisfiable):
        http_util.parse_range("bytes=0-10", 0)


def test_content_range_header():
    assert http_util.content_range_header(0, 99, 1000) == "bytes 0-99/1000"
    assert http_util.content_range_header(900, 999, 1000) == "bytes 900-999/1000"


# ==================== MIME / 静态路径 ====================

@pytest.mark.parametrize("name,ctype", [
    ("a.mp4", "video/mp4"),
    ("a.MKV", "video/x-matroska"),
    ("a.mkv", "video/x-matroska"),
    ("a.rmvb", "video/vnd.rn-realvideo"),
    ("a.m4a", "audio/mp4"),
    ("a.flac", "audio/flac"),
    ("a.webp", "image/webp"),
    ("a.srt", "text/plain; charset=utf-8"),
])
def test_guess_content_type(name, ctype):
    assert http_util.guess_content_type(name) == ctype


def test_guess_content_type_falls_back():
    assert http_util.guess_content_type("a.unknownext") == "application/octet-stream"


def test_static_file_path_allows_inside(tmp_path):
    (tmp_path / "app.js").write_text("// x", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "a.css").write_text("a{}", encoding="utf-8")

    assert http_util.static_file_path(str(tmp_path), "app.js")
    assert http_util.static_file_path(str(tmp_path), "sub/a.css")
    assert http_util.static_file_path(str(tmp_path), "")  # 空路径回落 index.html


@pytest.mark.parametrize("rel", [
    "../app.py",
    "sub/../../app.py",
    "..\\app.py",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "sub/../../..",
    "a\x00.js",
])
def test_static_file_path_blocks_escape(tmp_path, rel):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "app.js").write_text("// x", encoding="utf-8")

    assert http_util.static_file_path(str(tmp_path), rel) is None


# ==================== 字幕转 VTT ====================

def test_cues_to_vtt_from_dicts():
    text = http_util.cues_to_vtt([
        {"start_ms": 0, "end_ms": 1500, "text": "第一句"},
        {"start_ms": 2000, "end_ms": 0, "text": "第二句"},  # end=0 → 兜底 3 秒
    ])
    assert text.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in text
    assert "00:00:02.000 --> 00:00:05.000" in text
    assert "第一句" in text


def test_cues_to_vtt_skips_empty_text():
    text = http_util.cues_to_vtt([{"start_ms": 0, "end_ms": 1000, "text": "   "}])
    assert "-->" not in text


def test_cues_to_vtt_accepts_objects():
    class Cue:
        start_ms = 1000
        end_ms = 2000
        text = "对象"

    assert "对象" in http_util.cues_to_vtt([Cue()])


# ==================== 可播性判断 ====================

@pytest.mark.parametrize("ext,codec,playable", [
    (".mp4", "h264", True),
    (".mp4", "", True),          # 编码未知：让浏览器自己试
    (".webm", "vp9", True),
    (".mp4", "hevc", False),     # 手机 Chrome 放不了 HEVC
    (".mkv", "h264", False),     # 容器不支持
    (".rmvb", "rv40", False),
])
def test_browser_playable(ext, codec, playable):
    assert http_util.browser_playable(ext, codec) is playable


# ==================== URL / 令牌 / 配置归一化 ====================

def test_build_url():
    assert build_url("192.168.1.5", 8735) == "http://192.168.1.5:8735/"
    assert build_url("127.0.0.1", 8735, "abc") == "http://127.0.0.1:8735/?t=abc"
    assert build_url("127.0.0.1", 8735, "abc", with_token=False) == "http://127.0.0.1:8735/"


def test_ensure_token_generates_and_persists(env, monkeypatch):
    monkeypatch.setattr(config, "WEB_TOKEN", "")
    token = ensure_token()
    assert token and len(token) >= 8

    with open(config.USER_CONFIG_PATH, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["WEB_TOKEN"] == token
    # 第二次调用复用同一个令牌（重启后手机不用重新登录）
    assert ensure_token() == token


@pytest.mark.parametrize("value,expected", [
    (8735, 8735),
    ("9000", 9000),
    (80, config.WEB_DEFAULT_PORT),        # 特权端口回落
    (70000, config.WEB_DEFAULT_PORT),
    ("abc", config.WEB_DEFAULT_PORT),
    (None, config.WEB_DEFAULT_PORT),
])
def test_normalize_web_port(value, expected):
    assert config._normalize_web_port(value) == expected


def test_normalize_web_token_strips_dangerous_chars():
    assert config._normalize_web_token(' "ab c/def" ') == "abcdef"


# ==================== 数据视图 ====================

def test_summary_reports_all_three_modules(seeded):
    data = library.summary()
    assert data["photo"]["works"] == 1
    assert data["photo"]["images"] == 1
    assert data["audio"]["tracks"] == 1
    assert data["video"]["videos"] == 2


def test_photo_works_without_author_filter(seeded):
    """回归：author_id 缺省时不能当成 0 去查，否则首页永远是空列表"""
    works = library.photo_works()
    assert len(works) == 1
    assert works[0]["author"] == "作者A"
    assert works[0]["cover"].startswith("/media/photo-cover/")


def test_photo_cover_is_generated_on_demand(seeded):
    work_id = library.photo_works()[0]["id"]
    path = library.photo_cover(work_id)
    assert path and os.path.isfile(path) and os.path.getsize(path) > 0
    # 第二次直接命中缓存，不再重复缩图
    assert library.photo_cover(work_id) == path


def test_scaled_photo_only_uses_whitelisted_widths(seeded):
    image_id = library.photo_work_images(library.photo_works()[0]["id"])["images"][0]["id"]
    original = library.photo_image_path(image_id)

    assert library.scaled_photo(original, 9999) == original   # 非白名单 → 原图
    scaled = library.scaled_photo(original, 320)
    assert scaled != original and os.path.isfile(scaled)
    from PIL import Image

    with Image.open(scaled) as image:
        assert image.width <= 320


def test_audio_playlist_lists_children_and_tracks(seeded):
    playlists = library.audio_playlists()
    assert [p["name"] for p in playlists] == ["歌单A"]
    assert playlists[0]["track_count"] == 1

    detail = library.audio_playlist(playlists[0]["id"])
    assert len(detail["tracks"]) == 1
    assert detail["tracks"][0]["title"] == "第一首"
    assert detail["tracks"][0]["duration_ms"] == 2000


def test_audio_playlist_missing_returns_none(seeded):
    assert library.audio_playlist(9999) is None


def test_video_work_marks_unplayable_containers(seeded):
    work_id = library.video_works()[0]["id"]
    data = library.video_work_videos(work_id)
    flags = {v["name"]: v["playable"] for v in data["videos"]}
    assert flags["01.mp4"] is True
    assert flags["02.mkv"] is False


def test_video_progress_roundtrip(seeded):
    work_id = library.video_works()[0]["id"]
    video_id = library.video_work_videos(work_id)["videos"][0]["id"]

    assert library.save_video_progress(video_id, 120000, 600000) is True
    assert library.video_play_info(video_id)["position_ms"] == 120000

    # 看到结尾 → 清空，否则下次一打开就立刻结束
    library.save_video_progress(video_id, 599000, 600000)
    assert library.video_play_info(video_id)["position_ms"] == 0


def test_video_progress_ignored_without_duration(seeded):
    work_id = library.video_works()[0]["id"]
    video_id = library.video_work_videos(work_id)["videos"][0]["id"]
    library.save_video_progress(video_id, 5000, 0)
    assert library.video_play_info(video_id)["position_ms"] == 0


# ==================== 端到端 HTTP ====================

def test_root_requires_token(server):
    _instance, port = server
    status, _headers, _body = _get(port, "/")
    assert status == 401
    assert _get(port, "/api/summary")[0] == 401
    assert _get(port, "/media/photo/1")[0] == 401


def test_token_in_query_sets_cookie(server):
    _instance, port = server
    status, headers, body = _get(port, f"/?t={TOKEN}")
    assert status == 200
    assert "lrm_token=" in (headers.get("Set-Cookie") or "")
    assert "text/html" in headers.get("Content-Type", "")
    assert "本地资源".encode() in body


def test_cookie_grants_access(server):
    _instance, port = server
    status, _headers, body = _get(port, "/api/summary", cookie=TOKEN)
    assert status == 200
    data = json.loads(body)
    assert data["photo"]["works"] == 1


def test_api_photo_works_and_images(server):
    _instance, port = server
    works = json.loads(_get(port, "/api/photos/works", cookie=TOKEN)[2])["works"]
    assert len(works) == 1

    images = json.loads(
        _get(port, f"/api/photos/works/{works[0]['id']}/images", cookie=TOKEN)[2]
    )["images"]
    assert len(images) == 1
    assert images[0]["thumb"].endswith("?w=640")


def test_media_range_returns_exact_bytes(seeded):
    """视频能拖动的前提：206 + Content-Range + 正确字节数"""
    instance = WebServer()
    port = instance.start(port=0, lan=False, token=TOKEN)
    try:
        video_id = library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"]
        status, headers, body = _get(
            port, f"/media/video/{video_id}", cookie=TOKEN, headers={"Range": "bytes=8-23"}
        )
        assert status == 206
        assert headers["Content-Range"].startswith("bytes 8-23/")
        assert len(body) == 16
    finally:
        instance.stop()


def test_media_head_has_no_body(server):
    _instance, port = server
    video_id = library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"]
    status, headers, body = _get(port, f"/media/video/{video_id}", cookie=TOKEN, method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) > 0


def test_media_out_of_range_is_416(server):
    _instance, port = server
    video_id = library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"]
    status, headers, _body = _get(
        port, f"/media/video/{video_id}", cookie=TOKEN, headers={"Range": "bytes=999999999-"}
    )
    assert status == 416
    assert headers["Content-Range"].startswith("bytes */")


def test_media_etag_hit_returns_304(server):
    _instance, port = server
    image_id = library.photo_work_images(library.photo_works()[0]["id"])["images"][0]["id"]
    status, headers, _body = _get(port, f"/media/photo/{image_id}", cookie=TOKEN)
    assert status == 200
    etag = headers["ETag"]

    status, _headers, body = _get(
        port, f"/media/photo/{image_id}", cookie=TOKEN, headers={"If-None-Match": etag}
    )
    assert status == 304
    assert body == b""


def test_static_assets_are_served(server):
    _instance, port = server
    for path in ("/static/app.js", "/static/style.css", "/static/icon.svg"):
        status, headers, body = _get(port, path, cookie=TOKEN)
        assert status == 200, path
        assert len(body) > 100, path


def test_static_traversal_is_404(server):
    _instance, port = server
    assert _get(port, "/static/../app.py", cookie=TOKEN)[0] == 404
    assert _get(port, "/static/%2e%2e/app.py", cookie=TOKEN)[0] == 404


def test_api_video_progress_post(server):
    _instance, port = server
    video_id = library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"]
    body = json.dumps({"position_ms": 90000, "duration_ms": 600000}).encode()
    status, _headers, _payload = _get(
        port,
        f"/api/video/videos/{video_id}/progress",
        cookie=TOKEN,
        method="POST",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert library.video_play_info(video_id)["position_ms"] == 90000


def test_api_video_progress_rejects_garbage(server):
    _instance, port = server
    video_id = library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"]
    status, _headers, _payload = _get(
        port,
        f"/api/video/videos/{video_id}/progress",
        cookie=TOKEN,
        method="POST",
        body=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400


def test_unknown_api_is_404(server):
    _instance, port = server
    assert _get(port, "/api/nope", cookie=TOKEN)[0] == 404


def test_second_start_reuses_running_server(server):
    instance, port = server
    assert instance.start(port=0, lan=False, token=TOKEN) == port
    assert instance.running is True


def test_stop_releases_port(server):
    instance, port = server
    instance.stop()
    assert instance.running is False
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)


def test_lan_addresses_excludes_loopback():
    from web_server.app import lan_addresses

    addresses = lan_addresses()
    assert isinstance(addresses, list)
    assert all(isinstance(ip, str) for ip in addresses)
    assert not any(ip.startswith("127.") for ip in addresses)


# ==================== 前端资源守卫 ====================

def test_index_references_existing_assets():
    """删掉/改名静态文件时必须当场失败，而不是手机上打开一片空白"""
    from web_server.app import STATIC_DIR

    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
        html = handle.read()

    assert 'name="viewport"' in html, "缺少 viewport，手机会按桌面宽度渲染成一团"
    for name in ("app.js", "compat.js", "style.css", "icon.svg"):
        assert name in html, f"index.html 没有引用 {name}"
        assert os.path.isfile(os.path.join(STATIC_DIR, name)), f"{name} 不存在"


def test_static_assets_carry_current_version():
    """`?v=` 必须等于 APP_VERSION

    服务端给静态资源 1 天缓存（max-age=86400）。版本号不变，平板升级后浏览器
    会继续用缓存里的旧 app.js —— 看起来就是「明明修好了，平板上还是坏的」。
    """
    from resource_manager import config as app_config

    html = _static_text("index.html")
    refs = re.findall(r"/static/[A-Za-z0-9_.]+", html)
    versions = re.findall(r"/static/[A-Za-z0-9_.]+\?v=([0-9.]+)", html)

    assert refs, "index.html 没有引用任何静态资源"
    assert len(versions) == len(refs), "有静态资源没带 ?v=，升级后会被浏览器缓存住"
    assert all(v == app_config.APP_VERSION for v in versions), (
        f"?v= 与 APP_VERSION({app_config.APP_VERSION}) 不一致：{sorted(set(versions))}"
    )


def test_css_hidden_rule_beats_flex_display():
    """`[hidden]` 会被 .tabs{display:flex} 盖掉——这条 !important 是修过的坑"""
    from web_server.app import STATIC_DIR

    with open(os.path.join(STATIC_DIR, "style.css"), encoding="utf-8") as handle:
        css = handle.read()
    assert "[hidden]" in css
    assert "display: none !important" in css


def test_player_bar_is_opaque():
    """播放条半透明时下面的曲目行会透出来，观感很脏——必须是实色"""
    from web_server.app import STATIC_DIR

    with open(os.path.join(STATIC_DIR, "style.css"), encoding="utf-8") as handle:
        css = handle.read()
    start = css.index("#player {")
    block = css[start:css.index("}", start)]
    assert "rgba(" not in block

# ==================== 桌面端控制窗口守卫 ====================

def _close_event_node():
    """取出 ServerWindow.closeEvent 的 AST 节点

    为什么不用子串匹配：`if False:` 甚至一行注释都能让「包含某段文字」成立，
    注入式验证时这种守卫根本不会变红。走 AST 才能钉住「条件里真的用了回调」。
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "ui" / "server_window.py").read_text(
        encoding="utf-8"
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "ServerWindow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "closeEvent":
                    return item
    raise AssertionError("ServerWindow.closeEvent 不见了")


def test_server_window_close_returns_to_launcher():
    """点 ✕ 关窗必须把启动器叫回前台：漏了这步启动器还藏着，用户以为程序没了"""
    node = _close_event_node()
    guards = [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.If) and "_on_back_to_launcher" in ast.unparse(item.test)
    ]
    assert guards, "closeEvent 不再按 _on_back_to_launcher 是否存在来决定回调"
    calls = [ast.unparse(item) for item in ast.walk(guards[0]) if isinstance(item, ast.Call)]
    assert any("callback()" in call for call in calls), "closeEvent 里没有真的调用回调"


def test_server_window_close_keeps_server_running():
    """窗口只是控制台，关窗不能顺手把服务停了，否则手机上正播的歌会断"""
    node = _close_event_node()
    stops = [
        ast.unparse(item.func)
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and ast.unparse(item.func).endswith(".stop")
    ]
    assert stops == ["self._timer.stop"], f"closeEvent 里多停了东西：{stops}"


# ==================== 老浏览器兼容守卫 ====================
#
# 背景：用户的老平板是 Android 7，自带 WebView 是 Chrome 51~59 那一代。
# 当时 app.js 里有两处把它整死的写法：
#   `...(options || {})` 对象展开是 ES2018，直接语法错误，整份脚本不执行；
#   `replaceChildren` 是 Chrome 86+，26 处渲染全都调它。
# 结果就是「只显示名字、按钮全不出来」。下面几条守卫防它再犯。

def _static_text(name):
    from web_server.app import STATIC_DIR

    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments_and_strings(js):
    """把注释和字符串内容抹成空白（换行保留，行号才对得上）

    不剥的话，注释里随手写的示例 `...(options)` 也会被当成对象展开误报。
    """
    out = []
    index = 0
    length = len(js)
    while index < length:
        char = js[index]
        ahead = js[index + 1] if index + 1 < length else ""
        if char == "/" and ahead == "/":
            while index < length and js[index] != "\n":
                index += 1
        elif char == "/" and ahead == "*":
            index += 2
            while index < length and not (js[index] == "*" and index + 1 < length
                                          and js[index + 1] == "/"):
                index += 1
            index += 2
        elif char in "'\"`":
            quote = char
            index += 1
            while index < length and js[index] != quote:
                if js[index] == "\\":
                    index += 2
                    continue
                if js[index] == "\n":
                    out.append("\n")
                index += 1
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _object_spread_lines(js):
    """找出位于 `{...}` 里的 `...`（对象展开，ES2018）

    只做括号配对：最近的未闭合括号是 `{` 才算。
    `[...]` 是数组展开、`(...)` 是形参/实参摊开，都是 ES2015，老内核认。
    """
    code = _strip_comments_and_strings(js)
    lines = []
    stack = []
    index = 0
    while index < len(code):
        char = code[index]
        if char in "{[(":
            stack.append(char)
        elif char in "}])":
            if stack:
                stack.pop()
        elif code.startswith("...", index):
            if stack and stack[-1] == "{":
                lines.append(code.count("\n", 0, index) + 1)
            index += 2
        index += 1
    return lines


def test_app_js_has_no_object_spread():
    """对象展开会让老 WebView 整份脚本解析失败（不是某一行出错，是全都不执行）"""
    js = _strip_comments_and_strings(_static_text("app.js"))
    assert _object_spread_lines(js) == [], "app.js 里出现了对象展开 {...x}，改用 Object.assign"
    for token in ("?.", "??"):
        assert token not in js, f"app.js 用了 {token}（ES2020 语法），老浏览器解析失败"


def test_compat_js_loads_before_app_js():
    """垫片必须在 app.js 之前执行，顺序反了等于没垫"""
    html = _static_text("index.html")
    compat_at = html.index('src="/static/compat.js')
    app_at = html.index('src="/static/app.js')
    assert compat_at < app_at, "compat.js 必须排在 app.js 前面"


def test_compat_defines_the_apis_old_webviews_lack():
    """这几个函数老内核一个都没有，缺一个页面就停在半截"""
    js = _static_text("compat.js")
    for name in ("replaceChildren", "flat", "padStart", "entries", "append"):
        assert name in js, f"compat.js 没有为 {name} 提供垫片"
    assert "window.onerror" in js, "缺少 onerror 兜底，出问题时用户只能看到白屏"


def test_cover_does_not_use_aspect_ratio():
    """aspect-ratio 要 Chrome 88+；老平板上它失效后封面高度塌成 0，卡片只剩标题"""
    css = _static_text("style.css")
    offenders = [
        line for line in css.splitlines()
        if line.strip().startswith("aspect-ratio:")
    ]
    assert not offenders, "封面的比例请用 padding-bottom 百分比撑，别用 aspect-ratio"
    assert "padding-bottom: 133.3333%" in css


def test_static_js_is_served_with_a_script_mime(tmp_path):
    """老浏览器对未知 MIME 的 <script> 会拒绝执行"""
    from web_server import http_util

    assert http_util.guess_content_type("app.js").startswith("application/javascript")
    assert http_util.guess_content_type("style.css").startswith("text/css")


def _css_rule(css, selector):
    """取出某个顶层选择器的声明块（够用：只找第一个 `selector {`）"""
    marker = selector + " {"
    start = css.index(marker) + len(marker)
    end = css.index("}", start)
    return css[start:end]


def test_video_overlay_is_viewport_fixed():
    """Android 7 的 WebView 用独立图层渲染 <video>，按文档流算位置

    只写 top/right/bottom/left 的 fixed 在个别机型上会被算成「页面最后一块」，
    现象就是用户说的「视频在列表最下面播放」。浮层和 video 各自要 width/height 100%。
    """
    css = _static_text("style.css")
    overlay = _css_rule(css, "#video-overlay")
    assert "position: fixed" in overlay
    assert "width: 100%" in overlay, "浮层必须有显式宽度，别指望 left/right 拉出来"
    assert "height: 100%" in overlay, "浮层必须有显式高度，别指望 top/bottom 拉出来"

    video = _css_rule(css, "#video")
    assert "position: fixed" in video, "video 自己也要 fixed，不能靠父容器定位"
    assert "width: 100%" in video and "height: 100%" in video
    assert "flex:" not in video, "用 flex 撑高度在老 WebView 上算不出尺寸"


def test_body_video_open_hides_page_behind_player():
    """兜底：万一内核不认 fixed，屏上也只剩播放器，而不是「视频躲在列表下面」"""
    css = _static_text("style.css")
    for selector in (
        "body.video-open #topbar",
        "body.video-open #view",
        "body.video-open #player",
    ):
        assert selector in css, f"缺少 {selector}，视频浮层盖不住背后的页面"

    js = _static_text("app.js")
    assert "classList.add('video-open')" in js, "打开视频时没给 body 加 video-open"
    assert "classList.remove('video-open')" in js, "关闭视频时没摘掉 video-open"


def test_app_js_forces_video_fullscreen_as_fallback():
    """几何不对就地改内联样式——CSS 万一失效也能把画面拉回全屏"""
    js = _static_text("app.js")
    assert "function ensureVideoFullscreen(" in js
    assert "getBoundingClientRect" in js
    assert js.count("ensureVideoFullscreen();") >= 3, "打开、切集、转屏三处都要校正"


def test_compat_ignores_injected_third_party_errors():
    """小米/输入法会往页面注入脚本，它们自己报错不该弹红条吓用户"""
    js = _static_text("compat.js")
    assert "function isOurScript(" in js
    assert "/static/" in js, "要靠 /static/ 判断报错是不是自家脚本发的"


# ==================== 多根目录配置（v1.10.0） ====================

def test_normalize_roots_accepts_single_string_and_dedupes():
    """旧版单字符串、尾斜杠重复、空白项都要收敛成一个干净列表"""
    assert config._normalize_roots(r"E:\写真", "PHOTO_ROOTS") == [r"E:\写真"]
    assert config._normalize_roots(
        [r"E:\写真", r"E:\写真" + "\\", "   "], "PHOTO_ROOTS"
    ) == [r"E:\写真"]
    assert config._normalize_roots([], "PHOTO_ROOTS") == config._DEFAULT_USER_CONFIG["PHOTO_ROOTS"]


def test_old_single_root_config_is_migrated(tmp_path, monkeypatch):
    """老 config.json 里只有 PHOTO_ROOT / VIDEO_ROOT，升级后不能把用户路径丢了"""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"PHOTO_ROOT": "D:/旧写真", "VIDEO_ROOT": "D:/旧视频"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(config, "_user_config", dict(config._user_config))
    config._load_user_config()

    assert config._user_config["PHOTO_ROOTS"] == ["D:/旧写真"]
    assert config._user_config["VIDEO_ROOTS"] == ["D:/旧视频"]


def test_set_user_config_accepts_legacy_single_root(monkeypatch):
    """旧的 set_user_config("PHOTO_ROOT", ...) 调用要等价于「只有一个根」"""
    monkeypatch.setattr(config, "_user_config", dict(config._user_config))
    monkeypatch.setattr(config, "PHOTO_ROOTS", list(config.PHOTO_ROOTS))
    monkeypatch.setattr(config, "PHOTO_ROOT", config.PHOTO_ROOT)

    config.set_user_config("PHOTO_ROOT", "D:/新写真")

    assert config.PHOTO_ROOTS == ["D:/新写真"]
    assert config.PHOTO_ROOT == "D:/新写真"


# ==================== 网页大图：先缩图再原图（v1.10.0） ====================

def test_view_widths_stay_inside_thumbnail_whitelist(seeded):
    """看图用的宽度必须在白名单里：漏了会静默回落原图（几十 MB，正是卡顿根因）"""
    data = library.photo_work_images(library.photo_works()[0]["id"])
    image = data["images"][0]
    for key in ("thumb", "full"):
        width = int(image[key].split("w=")[1])
        assert width in config.WEB_THUMB_WIDTHS, f"{key} 用了未经缩图的白名单外宽度 {width}"
    assert image["orig"] and "?" not in image["orig"], "原图地址不应带缩放参数"


def test_lightbox_prefers_scaled_image_and_loads_original_on_demand():
    """看图默认 1600 宽主图，原图只在点「原图」时拉——直接给原图就是卡顿的来源"""
    js = _static_text("app.js")
    assert "lbImg.src = image.full" in js
    assert "image.orig" in js
    assert "lb-img" in _static_text("index.html")
    assert 'id="lb-orig"' in _static_text("index.html"), "看图浮层缺少「原图」按钮"


# ==================== 列表分页（v1.10.1） ====================

def test_paged_slices_and_pages():
    """切片、翻页、末页不满、越界页空集"""
    items = list(range(25))
    data = library._paged(items, 1, 10)
    assert data["items"] == list(range(10))
    assert (data["total"], data["pages"], data["page"]) == (25, 3, 1)
    assert library._paged(items, 3, 10)["items"] == list(range(20, 25))
    over = library._paged(items, 4, 10)
    assert over["items"] == [] and over["pages"] == 3


def test_paged_clamps_page_size_and_empty():
    """page_size 超上限钳到 200、缺省回 60，空列表 pages 也要是 1"""
    data = library._paged(list(range(10)), 1, 99999)
    assert data["page_size"] == library.PAGE_MAX
    data = library._paged(list(range(10)), 1, 0)
    assert data["page_size"] == library.PAGE_DEFAULT
    data = library._paged([], 1, 10)
    assert data["items"] == [] and data["total"] == 0 and data["pages"] == 1


def test_paged_bad_params_fall_back_to_defaults():
    """前端传非法 page/page_size 不能 500，按默认值处理"""
    data = library._paged([1, 2], "abc", "xyz")
    assert data["page"] == 1
    assert data["page_size"] == library.PAGE_DEFAULT
    assert data["items"] == [1, 2]


def test_api_photo_works_paginates_across_pages(server, env):
    """真翻页：6 个作品按 page_size=2 拆 3 页，两页 id 不重复"""
    _instance, port = server
    for i in range(2, 7):
        image_path = _make_image(str(env["photo_root"] / "作者A" / f"作品{i}" / "01.jpg"))
        photo_db.add_author_with_works(
            "作者A",
            [(f"作品{i}", str(env["photo_root"] / "作者A" / f"作品{i}"), "", [(image_path, 0)])],
        )

    first = json.loads(_get(port, "/api/photos/works?page=1&page_size=2", cookie=TOKEN)[2])
    second = json.loads(_get(port, "/api/photos/works?page=2&page_size=2", cookie=TOKEN)[2])
    assert first["total"] == 6 and first["pages"] == 3 and len(first["works"]) == 2
    assert len(second["works"]) == 2
    assert not {w["id"] for w in first["works"]} & {w["id"] for w in second["works"]}


def test_api_video_works_reports_pagination(server):
    _instance, port = server
    data = json.loads(_get(port, "/api/video/works?page=1&page_size=60", cookie=TOKEN)[2])
    assert data["total"] == 1 and data["pages"] == 1 and len(data["works"]) == 1


def test_api_audio_playlists_reports_pagination(server):
    _instance, port = server
    data = json.loads(_get(port, "/api/audio/playlists", cookie=TOKEN)[2])
    assert data["total"] == 1 and data["pages"] == 1 and len(data["playlists"]) == 1


def test_web_list_views_use_paged_grid():
    """切标签卡顿的根因是全量渲染：前端必须分页请求 + 提供加载更多 + 重绘回第一页"""
    js = _static_text("app.js")
    assert "function pagedGrid(" in js
    assert "params.set('page'" in js, "列表请求必须带 page 分页参数"
    assert "加载更多" in js, "分页后必须有「加载更多」入口，否则第二页永远看不到"
    assert js.count("state.repaint = wall.reload") == 3, "写真/视频/音频三个列表都要走分页重绘"
    assert "pagedGrid({" in js
    assert "'/api/photos/works'" in js and "'/api/video/works'" in js and "'/api/audio/playlists'" in js
