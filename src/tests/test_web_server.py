"""网页服务（web_server）守卫：Range、鉴权、路径安全、数据视图、真实 HTTP 链路

这里刻意分成两层：
- 纯函数（Range / MIME / 静态路径 / VTT）用参数化单测钉死，跑得飞快；
- 端到端用标准库 urllib 打真实端口，覆盖「手机到底能不能取到字节」这件事——
  只测函数不测链路的话，`Content-Range` 拼错、HEAD 多发一个包这类问题照样漏。

不依赖 ffmpeg：封面抽帧用桩替换，测试机没装 ffmpeg 也能全绿。
"""
import ast
import gzip
import json
import os
import re
import urllib.error
import urllib.request
import wave
from urllib.parse import quote

import pytest

from audio_manager import database as audio_db
from resource_manager import config
from resource_manager import database as photo_db
from video_manager import database as video_db
from web_server import http_util, library
from web_server.app import WebServer, build_url, ensure_token

TOKEN = "testtoken9x"

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_video_resume_lists_unfinished_by_recency(seeded):
    """网页「继续观看」与桌面侧栏共用 list_resume：倒序、封面按 id、不带路径"""
    work_id = library.video_works()[0]["id"]
    videos = library.video_work_videos(work_id)["videos"]
    library.save_video_progress(videos[0]["id"], 120000, 600000)
    library.save_video_progress(videos[1]["id"], 300000, 600000)

    # 同一秒内写入 updated_at 会打平，手工拉开顺序才能验倒序
    rows = {v["name"]: v["path"] for v in video_db.get_work_videos(work_id)}
    with video_db.get_conn() as conn:
        conn.execute("UPDATE progress SET updated_at = 1000 WHERE path = ?", (rows["01.mp4"],))
        conn.execute("UPDATE progress SET updated_at = 2000 WHERE path = ?", (rows["02.mkv"],))

    data = library.video_resume()
    assert data["total"] == 2
    top = data["items"][0]
    assert top["video_name"] == "02.mkv"
    assert top["work_id"] == work_id and top["work_name"] == "作品V"
    assert top["position_ms"] == 300000 and top["duration_ms"] == 600000
    assert top["cover"] == f"/media/video-cover/{work_id}"
    assert top["author"] == "作者V"


def test_video_resume_keyword_and_paging(seeded):
    work_id = library.video_works()[0]["id"]
    videos = library.video_work_videos(work_id)["videos"]
    for video in videos:
        library.save_video_progress(video["id"], 60000, 600000)

    assert library.video_resume("作品V")["total"] == 2
    assert library.video_resume("02.mkv")["total"] == 1
    assert library.video_resume("不存在的关键词")["total"] == 0

    first = library.video_resume("", 1, 1)
    assert first["total"] == 2 and first["pages"] == 2 and len(first["items"]) == 1


def test_video_resume_skips_finished_and_orphans(seeded):
    work_id = library.video_works()[0]["id"]
    video = library.video_work_videos(work_id)["videos"][0]
    library.save_video_progress(video["id"], 120000, 600000)
    # 进度表里存在、videos 表里没有（视频被删/改名）→ INNER JOIN 自然滤掉
    video_db.set_progress(str(seeded["video_root"] / "没了.mp4"), 50000, 600000)
    assert library.video_resume()["total"] == 1


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


# ==================== 视频拖动进度条（v1.10.3） ====================
#
# 背景：原生 <video controls> 的进度条是「拖一格 seek 一次」。1080p（GOP 250）
# 实测单次 seek 198ms、p90 357ms（同片 GOP 30 只要 34ms）——拖 30 格就是 6 秒
# 连续卡顿，用户感受就是「拖动进度条非常卡」。改成自绘条：拖动全程只动滑块和
# 数字，松手才写一次 currentTime。下面几条守卫防它退回原生控件或退回逐格 seek。

def _js_block(js, header):
    """从 header 起截出配对的 `{...}`（够用于静态守卫：这几个函数里没有花括号字符串）"""
    start = js.index(header)
    index = js.index("{", start)
    depth = 0
    for pos in range(index, len(js)):
        if js[pos] == "{":
            depth += 1
        elif js[pos] == "}":
            depth -= 1
            if depth == 0:
                return js[start:pos + 1]
    raise AssertionError(f"{header} 的花括号没配对")


def test_video_uses_own_progress_bar():
    """<video> 不能再带 controls：原生条和自绘条会叠在一起，且原生条逐格 seek"""
    html = _static_text("index.html")
    tag = re.search(r"<video[^>]*>", html)
    assert tag, "index.html 里找不到 <video>"
    assert "controls" not in tag.group(0), "原生 controls 又回来了：拖动会变成逐格 seek"

    for element in ('id="v-seek"', 'id="v-play"', 'id="v-pos"', 'id="v-dur"', 'id="v-full"'):
        assert element in html, f"缺少 {element}，自绘控件不完整"

    overlay = html[html.index('<div id="video-overlay"'):]
    assert 'id="v-seek"' in overlay, "进度条必须在视频浮层内部"


def test_video_seek_only_commits_on_release():
    """拖动时只许改显示，松手才 seek —— 这条是「拖一格 seek 一次」的止血带"""
    js = _static_text("app.js")

    # 注释里写着「绝不能写 currentTime」，先抹掉注释再判断真的代码
    on_input = _strip_comments_and_strings(_js_block(js, "vSeek.addEventListener('input'"))
    assert "currentTime" not in on_input, (
        "input 回调里又写了 currentTime：每一格拖动都会触发一次解码级 seek"
    )

    commit = _strip_comments_and_strings(_js_block(js, "function commitVideoSeek("))
    assert "currentTime" in commit, "松手时必须真的 seek"
    assert "dataset.dragging" in commit, "缺少拖动态判断，pointerup 会重复 seek"

    assert "vSeek.addEventListener('change', commitVideoSeek)" in js
    assert "vSeek.addEventListener('pointerup', commitVideoSeek)" in js, (
        "有的内核只在 pointerup 上报松手，两个都要接"
    )
    # 触摸屏上松手还可能只报 pointercancel / touchend：都要接，
    # 而且重复触发必须是空操作（靠 dataset.dragging 清标记），不能连 seek 三次
    for event in ("pointercancel", "touchend"):
        assert f"vSeek.addEventListener('{event}', commitVideoSeek)" in js, (
            f"没有接 {event}：触摸屏上松手可能收不到 change，拖动会不生效"
        )


def test_video_controls_do_not_fade_while_paused():
    """暂停时控件必须留着：淡出后用户找不到播放按钮，会以为播放器坏了"""
    js = _static_text("app.js")
    show = _js_block(js, "function showVideoControls(")
    assert "!videoEl.paused" in show, "淡出没有区分是否在播放"
    assert "v-idle" in show


def test_video_seek_restarts_stream_instead_of_seeking():
    """fMP4 流没有总时长，浏览器自己 seek 不了：拖动必须让服务端从目标点重新起转

    早先的做法是「流模式下把进度条置灰」，但用户真正想要的是能拖——
    `?start=` 重连比「完全不能拖」好用得多，代价只是重新缓冲几秒。
    """
    js = _static_text("app.js")
    commit = _js_block(js, "function commitVideoSeek(")
    assert "state.video.streaming" in commit, "拖动没有区分直连与流模式"
    assert "startStream(target)" in commit, "流模式拖动没有重新起流"
    assert "videoEl.currentTime = target" in commit, "直连模式还得能本地 seek"

    stream = _js_block(js, "function startStream(")
    assert "setVideoSeekable(false)" not in stream, "流模式不该再把进度条置灰"


def test_stream_url_encodes_start_offset():
    """拖动重连靠 ?start=<秒>，拼错就退回从头转——白等一次"""
    js = _static_text("app.js")
    url = _js_block(js, "function streamUrl(")
    assert "start=" in url
    assert "Math.round(start)" in url, "start 得取整，小数进了 query 会变 400"
    # 地址上已经挂着 `?caps=` 了，第二个参数必须用 & 接
    assert "indexOf('?') >= 0 ? '&' : '?'" in url


def test_video_controls_bar_is_self_positioned():
    """控件条也得自己 fixed：老 WebView 里 <video> 是独立图层，靠父容器定位会跑偏"""
    css = _static_text("style.css")
    bar = _css_rule(css, ".v-controls")
    assert "position: fixed" in bar
    assert "bottom: 0" in bar
    assert "z-index" in bar, "没有层级会被 <video> 盖住"

    idle_at = css.index("#video-overlay.v-idle")
    idle = css[idle_at:css.index("}", idle_at)]
    assert "opacity: 0" in idle
    assert "pointer-events: none" in idle, "淡出后残留的透明层会吃掉点击"


def test_video_buttons_are_big_enough_for_fingers():
    """7.9 寸 1920×1200（CSS 1px≈0.13mm）上，34px 的按钮只有 4mm 宽，按不准

    触摸目标按 36px 起算（≈4.7mm），进度条再高一点才好拖。
    """
    css = _static_text("style.css")
    button = _css_rule(css, ".v-btn")
    for prop in ("width", "height"):
        value = re.search(rf"{prop}:\s*(\d+)px", button)
        assert value, f".v-btn 没写死 {prop}"
        assert int(value.group(1)) >= 36, f".v-btn 的 {prop} 太小，手指按不准"

    seek = _css_rule(css, "#v-seek")
    height = re.search(r"height:\s*(\d+)px", seek)
    assert int(height.group(1)) >= 28, "进度条的触摸高度太小，平板上很难拖"


def test_video_time_updates_do_not_touch_dom_pointlessly():
    """timeupdate 每秒来 4 次：内容没变还写 textContent/value 会白烧一次重排

    Tegra K1 + 2GB 那级设备上，这类「小便宜」很实在。
    """
    js = _static_text("app.js")
    assert "function setText(" in js

    sync = _js_block(js, "function syncVideoTime(")
    assert "setText(" in sync, "时间文字没走 setText，每次都会重排"
    assert "vSeek.value !== value" in sync, "滑块位置没比较就赋值，会反复重排进度条"


def test_flex_rows_do_not_rely_on_gap():
    """flex 容器的 `gap` 是 Chrome 84 才支持的

    用户的平板是 Android 7 + Chrome 78 内核（小米平板 1 / MI PAD），
    那一条会被整条忽略，表现就是「控件全挤成一团」。原来用 gap 的 flex 行/列
    统一改成相邻兄弟外边距；grid 的 gap 是 Chrome 66 就有的，不用动。
    """
    css = _static_text("style.css")
    flex_rows = [
        ".bar-row", ".brand", ".tabs", ".section-title", ".home-card", ".chips",
        ".list", ".row", ".p-controls", ".p-seek-row", ".v-top", ".v-controls",
    ]
    for selector in flex_rows:
        body = _css_rule(css, selector)
        assert "gap:" not in body, f"{selector} 又用 flex gap 了，老内核上会挤在一起"
        assert f"{selector} > * + *" in css, f"{selector} 缺少相邻兄弟外边距兜底"

    # chip 的首个子节点是文本节点（`* + *` 匹配不到匿名文本项），只能给计数单独补
    assert "margin-left: 6px" in _css_rule(css, ".chip .count")


def test_video_fullscreen_supports_native_player_for_old_browsers():
    """完全没有 requestFullscreen 的老内核（Android 4.x）只剩 <video> 自己的全屏通道

    注意：这条退路只给「连元素全屏 API 都没有」的浏览器用；有 API 但静默失败的
    WebView 不许走它，否则页面会被留在全屏媒体态（见
    test_silent_fullscreen_failure_never_enters_native_player）。
    """
    js = _static_text("app.js")
    assert "function enterNativeVideoFullscreen(" in js
    assert "webkitEnterFullscreen" in js, "没走原生视频全屏这条退路"

    handler = _js_block(js, "$('v-full').addEventListener(")
    assert "if (!asked)" in handler, "只有拿不到元素全屏 API 时才该退原生"


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
    assert "imgEl.src = image.full" in js
    assert "lbImgNow().src = image.orig" in js
    assert "lb-img" in _static_text("index.html")
    assert 'id="lb-orig"' in _static_text("index.html"), "看图浮层缺少「原图」按钮"


def test_full_image_goes_back_to_its_own_layer():
    """1600 主图必须回填到「画它的那一层」：写到 lbImgNow() 会贴到正在推走的那层；
    而且不能拿全局 index 认人——动画还没提交索引时主图就已经解码完了，会被丢掉，
    新的一张就永远停在 640 缩图"""
    js = _static_text("app.js")
    assert "function paintLayer(imgEl, image)" in js
    assert "imgEl.__image = image" in js
    assert "if (imgEl.__image !== image) return;" in js
    assert "function ensureFull(" in js
    assert "ensureFull();" in js, "翻页落定后要补一次主图"
    assert "target.src = image.full" not in js


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


def test_api_video_resume_serves_items_and_is_not_cached(server):
    _instance, port = server
    work_id = library.video_works()[0]["id"]
    video_id = library.video_work_videos(work_id)["videos"][0]["id"]
    library.save_video_progress(video_id, 120000, 600000)

    status, headers, body = _get(port, "/api/video/resume?page=1&page_size=60", cookie=TOKEN)
    assert status == 200
    assert "no-store" in headers.get("Cache-Control", ""), "看完一集必须立刻从列表消失"
    data = json.loads(body)
    assert data["total"] == 1
    item = data["items"][0]
    assert item["video_id"] == video_id and item["work_id"] == work_id
    assert item["position_ms"] == 120000 and item["duration_ms"] == 600000
    assert item["cover"] == f"/media/video-cover/{work_id}"


def test_web_video_resume_entry_and_deep_link():
    """续播入口：分页网格 + 深链到具体某一集（否则点进去还得自己找第几集）"""
    js = _static_text("app.js")
    assert "'/api/video/resume'" in js
    assert "继续观看" in js
    assert "`#/video/w/${item.work_id}/${item.video_id}`" in js
    assert "'#/video?resume=1'" in js
    assert "playVideoId" in js and "if (playVideoId)" in js, "深链要真的自动开那一集"
    assert "'progress-line'" in js, "续播卡必须有进度条"


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


# ==================== 访问令牌开关（v1.10.2） ====================

def test_server_without_token_allows_anonymous():
    """局域网自用关掉令牌：不带任何凭证也能打开页面和 API，链接里也不再拼 t="""
    instance = WebServer()
    port = instance.start(port=0, lan=False, token=TOKEN, require_token=False)
    try:
        status, _, body = _get(port, "/api/summary")
        assert status == 200
        assert json.loads(body)["version"] == config.APP_VERSION
        status, _, _ = _get(port, "/")
        assert status == 200
        assert "t=" not in instance.url("127.0.0.1"), "关掉令牌后链接不应再带 t="
    finally:
        instance.stop()


def test_server_with_token_still_rejects_anonymous():
    """开关默认开着：无凭证仍是 401，链接带令牌"""
    instance = WebServer()
    port = instance.start(port=0, lan=False, token=TOKEN, require_token=True)
    try:
        status, _, _ = _get(port, "/api/summary")
        assert status == 401
        assert instance.url("127.0.0.1").endswith(f"?t={TOKEN}")
    finally:
        instance.stop()


def test_web_require_token_config_defaults_and_normalize():
    """默认启用令牌；开关值一律归一成 bool"""
    assert config._DEFAULT_USER_CONFIG["WEB_REQUIRE_TOKEN"] is True
    assert config._DEFAULT_USER_CONFIG["WEB_REQUIRE_TOKEN"] is bool(
        config._DEFAULT_USER_CONFIG["WEB_REQUIRE_TOKEN"]
    )


def test_server_window_has_token_toggle():
    """设置窗口必须有令牌开关，且所有 start() 调用都把开关状态传给服务"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "ui" / "server_window.py"
    ).read_text(encoding="utf-8")
    assert "WEB_REQUIRE_TOKEN" in source
    assert "_on_require_token_changed" in source

    tree = ast.parse(source)
    starts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        # self._server.start(...)；排除 self._timer.start() 这类同名调用
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_server"
    ]
    # _toggle_server / _reset_token / _on_settings_changed / _on_require_token_changed
    assert len(starts) == 4, f"server_window 里 start() 调用数变了（{len(starts)}），补齐守卫"
    for call in starts:
        keyword_names = [kw.arg for kw in call.keywords]
        assert "require_token" in keyword_names, (
            f"start() 调用没传 require_token，关令牌开关会不生效: {ast.unparse(call)[:80]}"
        )


# ==================== 分页与传输（v1.11.0） ====================

def _insert_images(work_id, names, base):
    with photo_db.get_conn() as conn:
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO images (work_id, path) VALUES (?, ?)",
                (work_id, str(base / name)),
            )


def _app_source():
    """读 web_server/app.py 源码：传输层行为光靠函数单测钉不住"""
    from web_server.app import STATIC_DIR

    path = os.path.join(os.path.dirname(STATIC_DIR), "app.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _image_names(page):
    return [os.path.basename(library.photo_image_path(item["id"])) for item in page["images"]]


def test_photo_images_are_paged_in_natural_order(seeded):
    """一册两千多张不能一次全吐：接口必须分页，且跨页顺序与桌面一致（1,2,10）"""
    base = seeded["root"] / "册子"
    photo_db.add_author_with_works("作者C", [("作品C", str(base), "", [])])
    work_id = max(work["id"] for work in library.photo_works())
    _insert_images(work_id, ["2.jpg", "10.jpg", "1.jpg"], base)

    first = library.photo_work_images(work_id, 1, 2)
    second = library.photo_work_images(work_id, 2, 2)
    assert first["total"] == 3 and first["pages"] == 2
    assert _image_names(first) == ["1.jpg", "2.jpg"]
    assert _image_names(second) == ["10.jpg"]
    assert library.photo_work_images(work_id, 3, 2)["images"] == []


def test_photo_image_order_cache_sees_new_images(seeded):
    """顺序缓存按「行数 + mtime」失效：新扫进来的图不能因为缓存而消失"""
    base = seeded["root"] / "册子"
    photo_db.add_author_with_works("作者C", [("作品C", str(base), "", [])])
    work_id = max(work["id"] for work in library.photo_works())
    _insert_images(work_id, ["1.jpg"], base)
    assert library.photo_work_images(work_id, 1, 10)["total"] == 1

    _insert_images(work_id, ["2.jpg"], base)
    assert library.photo_work_images(work_id, 1, 10)["total"] == 2


def test_photo_image_payload_has_no_unused_fields(seeded):
    """前端只用 thumb/full/orig 和序号：文件名白占 40 字节/张，两千张就是 80KB"""
    work_id = library.photo_works()[0]["id"]
    image = library.photo_work_images(work_id)["images"][0]
    assert set(image) == {"id", "thumb", "full", "orig"}


def test_json_response_is_gzipped_when_client_accepts(server, seeded):
    """列表 JSON 字段名大量重复，gzip 后只剩十分之一"""
    _instance, port = server
    work_id = library.photo_works()[0]["id"]
    _insert_images(work_id, [f"extra{i:03d}.jpg" for i in range(40)], seeded["root"] / "extra")

    status, headers, body = _get(
        port, f"/api/photos/works/{work_id}/images?page_size=200", cookie=TOKEN,
        headers={"Accept-Encoding": "gzip"},
    )
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    assert headers.get("Vary") == "Accept-Encoding"
    data = json.loads(gzip.decompress(body).decode("utf-8"))
    assert data["total"] >= 41


@pytest.mark.parametrize("header,expected", [
    ("gzip", True),
    ("gzip, deflate, br", True),
    ("deflate", False),
    ("gzip;q=0", False),
    ("", False),
    (None, False),
])
def test_accepts_gzip(header, expected):
    from web_server.app import _accepts_gzip

    assert _accepts_gzip(header) is expected


def test_token_cookie_is_httponly(server):
    """前端从不读这个 cookie，加 HttpOnly 少一条被 XSS 拿走令牌的路"""
    _instance, port = server
    _status, headers, _body = _get(port, f"/?t={TOKEN}")
    cookie = headers.get("Set-Cookie") or ""
    assert "lrm_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_audio_playlist_reports_parent_id(seeded):
    """子歌单要能退回父歌单：只给 parent_path 前端换不出上级链接"""
    parent_id = audio_db.add_playlist("父歌单", str(seeded["root"] / "audio"))
    children = {"name": "子歌单", "path": str(seeded["root"] / "audio" / "子歌单")}
    child_id = audio_db.add_playlist(
        children["name"], children["path"], parent_path=str(seeded["root"] / "audio")
    )

    detail = library.audio_playlist(child_id)
    assert detail["playlist"]["parent_id"] == parent_id
    assert library.audio_playlist(parent_id)["playlist"]["parent_id"] == 0


def test_audio_playlists_page_does_not_scan_whole_table(seeded):
    """按父路径直接取一页：老实现每次都把整库读进内存（2265 个歌单要 58 ms）"""
    import inspect

    source = inspect.getsource(library.audio_playlists_page)
    assert "LIMIT ? OFFSET ?" in source
    assert "list_playlists()" not in source, "不许再全表 list_playlists()"


def test_media_supports_sendfile_with_fallback():
    """大文件走零拷贝；平台不支持时必须有分块循环兜底，否则老系统上直接不发数据"""
    from web_server.app import STATIC_DIR  # noqa: F401 - 触发模块导入

    source = _app_source()
    assert "sendfile" in source
    assert "file_obj.read(min(http_util.CHUNK_SIZE, remaining))" in source


def test_ffmpeg_streams_are_single_flight():
    """快速切集会同时起多个 ffmpeg，全都更慢——转码与转封装各限一路

    两个信号量必须分开：共用一个的话「一边转码一边翻看另一集」会互相卡死。
    """
    source = _app_source()
    assert "_REMUX_SLOTS = threading.Semaphore(1)" in source
    assert "_TRANSCODE_SLOTS = threading.Semaphore(1)" in source
    assert "slots = _TRANSCODE_SLOTS if transcoding else _REMUX_SLOTS" in source, \
        "转码没有走自己的信号量"
    assert "slots.acquire(timeout=REMUX_WAIT_SECONDS)" in source
    assert "slots.release()" in source, "拿不到槽位时要放回去"


# ==================== 前端守卫：分页 / 缩放 / 返回上级 ====================

def test_photo_work_page_uses_paged_grid():
    """作品内图片也必须分页：最大一册 2025 张，全量渲染要建一万多个节点"""
    js = _static_text("app.js")
    assert "pageSize: 120" in js
    assert "onclick: () => openLightbox(wall, index)" in js, "浮层要拿到整册序号，不能只给本页序号"
    assert "source.loadMore" in js, "翻到已加载末尾要能续页，否则最后一张翻不过去"


def test_grid_auto_loads_on_scroll():
    """滚到底自动续页：只剩「加载更多」按钮的话，用户会以为到底了"""
    js = _static_text("app.js")
    assert "function maybeAutoLoad(" in js
    assert "window.addEventListener('scroll'" in js
    assert "state.autoLoad" in js


def test_lightbox_supports_pinch_and_double_tap_zoom():
    """写真没有缩放等于没有细节：捏合与双击都要落在自己的 transform 上"""
    js = _static_text("app.js")
    css = _static_text("style.css")
    assert "function zoomAt(" in js and "function resetZoom(" in js
    assert "'pinch'" in js and "touchDistance(" in js
    assert "touch-action: none" in css, "交给浏览器就会变成整页放大，顶栏跟着变大"
    assert ".lb-stage img.zooming" in css, "切档位要动画，跟手拖动不能有延迟"


def test_detail_pages_expose_back_button():
    """iOS 加到主屏后没有返回键，详情页必须自带退路"""
    html = _static_text("index.html")
    js = _static_text("app.js")
    assert 'id="back-btn"' in html
    assert "backBtn.dataset.target" in js
    assert "data.playlist.parent_id" in js, "子歌单要退回父歌单，不是直接回模块首页"


def test_audio_cover_inherits_through_multiple_levels(seeded):
    """三层继承：祖父有封面时，孙子也要拿到（按层批量查很容易在这里漏）"""
    from PIL import Image

    root = seeded["root"] / "audio" / "top"
    cover = root / "cover.jpg"
    os.makedirs(root, exist_ok=True)
    Image.new("RGB", (60, 60), (30, 60, 90)).save(cover, "JPEG")

    top = audio_db.add_playlist("顶层", str(root), cover=str(cover))
    mid = audio_db.add_playlist("中层", str(root / "mid"), parent_path=str(root))
    leaf = audio_db.add_playlist("底层", str(root / "mid" / "leaf"), parent_path=str(root / "mid"))

    page = library.audio_playlists_page("", page=1, page_size=60)
    by_id = {item["id"]: item for item in page["items"]}
    assert by_id[top]["has_cover"] is True

    leaf_brief = library.audio_playlist(mid)["children"][0]
    assert leaf_brief["id"] == leaf
    assert leaf_brief["has_cover"] is True, "封面只继承了一层，说明父链断了"


# ==================== 看图翻页（v1.11.1） ====================

def test_lightbox_has_two_layers_for_transition():
    """一张推走、一张推进来：单张 <img> 换 src 做不出位移，必须是两层轮换"""
    html = _static_text("index.html")
    js = _static_text("app.js")
    assert 'id="lb-layer-a"' in html and 'id="lb-layer-b"' in html
    assert 'id="lb-img"' in html and 'id="lb-img-b"' in html
    assert "lbLayers = [$('lb-layer-a'), $('lb-layer-b')]" in js
    assert "function flipTo(" in js and "lbFront ^= 1" in js


def test_flip_translates_layer_while_zoom_translates_image():
    """翻页位移写在层上、缩放写在图上：两套 transform 挤在同一个元素上会互相覆盖"""
    js = _static_text("app.js")
    assert "lbLayers[lbFront].style.transform" in js
    assert "lbImgNow();\n    img.style.transform" in js
    assert "translate3d(${dir * shift}px, 0, 0)" in js


def test_swipe_follows_finger_then_snaps_or_commits():
    """跟手拖动：手指停在哪图就停在哪，松手按距离决定翻过去还是弹回"""
    js = _static_text("app.js")
    assert "function beginSwipe(" in js
    assert "function moveSwipe(" in js
    assert "function endSwipe(" in js
    assert "Math.min(width * 0.2, 96)" in js, "翻过去的阈值改了要同步手感预期"
    # 跟手阶段必须关掉 transition，否则图会落后手指半拍
    assert "inLayer.style.transition = 'none'" in js


def test_flip_respects_reduced_motion():
    """系统开了「减弱动态效果」就只换图、不做位移动画"""
    js = _static_text("app.js")
    assert "prefers-reduced-motion: reduce" in js
    assert "if (dir && !LB_REDUCED)" in js


def test_lightbox_supports_keyboard_paging():
    """桌面端 ← → 翻页、Esc 关闭"""
    js = _static_text("app.js")
    assert "event.key === 'ArrowLeft'" in js
    assert "event.key === 'ArrowRight'" in js
    assert "event.key === 'Escape'" in js


# ==================== 片头 / 片尾跳过（网页端） ====================
#
# 网页端不另存一份数据：标记写的是 video_manager.db 的 skip_segments（作品级），
# 逐集对齐结果写 skip_overrides。所以这里的重点不是「接口能通」，而是
# **同一作品下每一集都拿到同一份计划**，以及坏输入不能把计划弄坏。

def _first_work_videos():
    work_id = library.video_works()[0]["id"]
    return library.video_work_videos(work_id)["videos"]


def _post_skip(port, video_id, payload):
    return _get(
        port,
        f"/api/video/videos/{video_id}/skip",
        cookie=TOKEN,
        method="POST",
        body=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


def test_video_play_info_starts_without_skip(seeded):
    video = _first_work_videos()[0]
    info = library.video_play_info(video["id"])
    assert info["skip"]["plan"] == []
    assert info["skip"]["segments"] == []
    assert info["skip"]["work_id"] == library.video_works()[0]["id"]


def test_post_skip_op_applies_to_whole_work(server):
    _instance, port = server
    videos = _first_work_videos()
    status, _headers, payload = _post_skip(
        port, videos[0]["id"], {"kind": "op", "start_ms": 12000, "end_ms": 102000}
    )
    assert status == 200
    assert json.loads(payload)["plan"] == [
        {"kind": "op", "start_ms": 12000, "end_ms": 102000}
    ]

    # 同一作品的**另一集**也必须拿到同一段（这就是「标一次全剧通用」）
    other = library.video_play_info(videos[1]["id"])
    assert other["skip"]["plan"] == [{"kind": "op", "start_ms": 12000, "end_ms": 102000}]


def test_post_skip_ed_open_ended_uses_duration(server):
    """片尾只标起点（end=0）：终点由这一集的时长兜底"""
    _instance, port = server
    video = _first_work_videos()[0]
    status, _headers, payload = _post_skip(
        port, video["id"], {"kind": "ed", "start_ms": 540000, "end_ms": 0}
    )
    assert status == 200
    plan = json.loads(payload)["plan"]
    assert plan == [{"kind": "ed", "start_ms": 540000, "end_ms": 600000}]


def test_post_skip_rejects_short_segment(server):
    _instance, port = server
    video = _first_work_videos()[0]
    status, _headers, payload = _post_skip(
        port, video["id"], {"kind": "op", "start_ms": 12000, "end_ms": 14000}
    )
    assert status == 400
    assert "太短" in json.loads(payload)["error"]
    assert library.video_play_info(video["id"])["skip"]["segments"] == []


def test_post_skip_rejects_unknown_kind(server):
    _instance, port = server
    video = _first_work_videos()[0]
    status, _headers, _payload = _post_skip(
        port, video["id"], {"kind": "preview", "start_ms": 0, "end_ms": 90000}
    )
    assert status == 400


def test_post_skip_clear_removes_both(server):
    _instance, port = server
    video = _first_work_videos()[0]
    _post_skip(port, video["id"], {"kind": "op", "start_ms": 12000, "end_ms": 102000})
    _post_skip(port, video["id"], {"kind": "ed", "start_ms": 540000, "end_ms": 0})
    status, _headers, payload = _post_skip(port, video["id"], {"clear": True})
    assert status == 200
    assert json.loads(payload)["segments"] == []
    assert library.video_play_info(video["id"])["skip"]["plan"] == []


def test_aligned_absent_drops_segment_for_that_episode(seeded):
    """对齐判定「这集没有片头」时，只影响这一集，别的集照跳"""
    work_id = library.video_works()[0]["id"]
    videos = _first_work_videos()
    paths = [row["path"] for row in video_db.get_work_videos(work_id)]
    video_db.set_skip_segment(work_id, "op", 12000, 102000, "")
    video_db.set_skip_override(paths[0], "op", 0, 0, "absent")

    assert library.video_play_info(videos[0]["id"])["skip"]["plan"] == []
    assert library.video_play_info(videos[1]["id"])["skip"]["plan"] == [
        {"kind": "op", "start_ms": 12000, "end_ms": 102000}
    ]


def test_skip_endpoint_requires_token(server):
    _instance, port = server
    video = _first_work_videos()[0]
    assert _get(port, f"/api/video/videos/{video['id']}/skip")[0] == 401


def test_video_players_share_one_skip_table():
    """网页端与桌面播放器必须用同一套规则/同一张表，不能各记一份"""
    for name in ("ui/video_player.py", "web_server/library.py"):
        with open(os.path.join(SRC_DIR, name), encoding="utf-8") as handle:
            source = handle.read()
        assert "video_manager" in source
        assert "skip_logic" in source or "skip as" in source
        assert "build_plan" in source or "set_skip_segment" in source


def test_overlay_has_skip_controls():
    """播放器里的标记/提示元素必须真的存在——少一个 id，JS 当场 TypeError"""
    html = _static_text("index.html")
    js = _static_text("app.js")
    for name in ("v-mark", "v-mark-op", "v-mark-ed", "v-mark-clear", "v-mark-info",
                 "v-tag", "v-hint"):
        assert f'id="{name}"' in html, f"index.html 缺 #{name}"
        assert f"$('{name}')" in js, f"app.js 没有用到 #{name}"


def test_video_toast_is_never_used_inside_player():
    """播放时 #toast 是 display:none，播放器里的反馈必须走 #v-hint"""
    js = _strip_comments_and_strings(_static_text("app.js"))
    player_start = js.index("function openVideo(")
    head, player = js[:player_start], js[player_start:]
    # 播放器段里允许出现 toast( 吗？不允许：那是「用户看不见的提示」
    assert "toast(" not in player.replace("showVideoHint(", ""), \
        "播放器里用了 toast()，body.video-open 时它被隐藏，提示看不到"
    assert "showVideoHint(" in player
    assert head  # 只是为了让断言失败时能看出比较的是什么


def test_auto_skip_does_not_repeat_after_manual_seek_back():
    """用户手动拖回片头里看，这一集不能再被弹走（跳过一次就记下来）"""
    js = _static_text("app.js")
    assert "function markSkippedAt(" in js
    assert "markSkippedAt(Math.round(target * 1000))" in js
    assert "state.video.skipped[seg.kind] = true" in js


# ==================== 下一集 ====================
#
# 网页端原本只有「片尾跳过 → 进下一集」和「播完 → 进下一集」两条隐式路径，
# 没有「我现在就想看下一集」的按钮。加按钮的风险全在边界上：最后一集越界、
# 切集前丢进度。所以这里断言的是**顺序**，不是「字符串存在」。

def _js_function(name):
    js = _static_text("app.js")
    start = js.index(f"function {name}(")
    return js[start:js.index("\n  }", start)]


def test_player_has_next_episode_button():
    html = _static_text("index.html")
    js = _static_text("app.js")
    assert 'id="v-next"' in html, "index.html 里没有下一集按钮"
    assert "$('v-next')" in js, "app.js 没有绑定 #v-next"
    # 按钮是空的 <button>，图标靠 JS 注入；漏了这行按钮在界面上是个透明圆圈
    assert "$('v-next').innerHTML = ICON.next" in js


def test_next_episode_stops_at_last_one():
    body = _js_function("goNextEpisode")
    assert "state.video.videos.length" in body
    # 越界判断必须在 loadVideo 之前：反过来会拿 undefined 去读 .name
    assert "return false" in body
    assert body.index("return false") < body.index("loadVideo")


def test_next_button_saves_progress_before_switching():
    """timeupdate 是每 10 秒存一次，切集不先落盘会丢掉刚看的那一段"""
    js = _static_text("app.js")
    handler = js[js.index("$('v-next').addEventListener"):]
    handler = handler[:handler.index("});")]
    assert "saveVideoProgress(false)" in handler
    assert handler.index("saveVideoProgress") < handler.index("goNextEpisode")


def test_ed_skip_marks_watched_even_on_last_episode():
    """最后一集的片尾跳也要上报「已看完」，否则续播点留在片尾，下次一打开又被跳"""
    body = _js_function("finishEpisode")
    assert "postProgress" in body
    assert body.index("postProgress") < body.index("goNextEpisode")


def test_next_button_is_dimmed_not_removed_on_last_episode():
    """最后一集压暗而不是 display:none——按钮消失会让进度条左右跳一下"""
    css = _static_text("style.css")
    js = _static_text("app.js")
    assert ".v-btn.dim" in css
    assert "classList.toggle('dim'" in js


# ==================== 全屏退出后的收尾 ====================
#
# 真机踩到的：全屏看视频 → 点关闭回列表 → 页面其实还在全屏态（overlay 藏了，
# document.fullscreenElement 还是它）。Android 把这当成「全屏媒体」，上下滑
# 被系统吃掉去调音量，列表就滑不动。所以关闭必须先退全屏，再隐藏。

def test_close_video_exits_fullscreen_before_hiding():
    body = _js_function("closeVideo")
    assert "exitPlayerFullscreenWithRetry()" in body
    # 顺序不能反：先 hidden 的话，exitFullscreen 面对的是已经不可见的全屏元素
    assert body.index("exitPlayerFullscreenWithRetry") < body.index("$('video-overlay').hidden = true")


def test_exit_fullscreen_retries_until_it_sticks():
    """退出全屏在 WebView 上是异步的，一次调用可能被 reject 或静默失败

    真机现象：点返回回列表了，页面却仍是全屏态 → 上下滑被系统当「全屏媒体手势」
    （右侧音量、左侧亮度），列表滑不动。所以退完要复查，还在就再补两刀。
    """
    body = _js_function("exitPlayerFullscreenWithRetry")
    assert "exitPlayerFullscreen()" in body
    assert "stillInFullscreen()" in body, "退出后没有复查，失败了也不知道"
    assert "setTimeout" in body, "没有重试"
    still = _js_function("stillInFullscreen")
    assert "fullscreenElement" in still and "webkitDisplayingFullscreen" in still


def test_silent_fullscreen_failure_never_enters_native_player():
    """元素全屏静默失败时不能退到 <video> 原生全屏

    原生全屏进去容易出来难：退出由原生播放器控制，宿主壳不一定回调
    onHideCustomView，页面就卡在「全屏媒体」状态。播放器本身铺满视口，
    少一条状态栏不值得冒这个险。
    """
    handler = _js_block(_static_text("app.js"), "$('v-full').addEventListener(")
    assert "enterNativeVideoFullscreen()" in handler, "不支持 requestFullscreen 的老浏览器仍要能全屏"
    # 800ms 那条兜底分支只许提示，不许进原生全屏
    tail = handler[handler.index("800"):]
    assert "enterNativeVideoFullscreen()" not in tail, "静默失败又退到原生视频全屏了"


def test_webview_never_asks_for_system_fullscreen():
    """UA 带 wv 的 WebView 一律不开系统全屏

    播放器本身就是铺满视口的固定层，系统全屏只多藏一条状态栏；可宿主壳对
    onShowCustomView/onHideCustomView 的实现参差不齐，进去容易退不出来——
    退不出来时整个页面都被系统当「全屏媒体」，列表再也滑不动。
    """
    js = _static_text("app.js")
    assert "IS_WEBVIEW" in js
    assert "wv[);]" in js, "UA 判定不见了"
    handler = _js_block(js, "$('v-full').addEventListener(")
    assert "systemFullscreenBroken" in handler
    # 短路必须排在真正 requestFullscreen 之前
    assert handler.index("systemFullscreenBroken") < handler.index("requestFullscreen")


def test_hash_change_closes_player_cleanly():
    """系统返回键直接改 hash，不经过关闭按钮，也得退全屏+解锁滚动"""
    js = _static_text("app.js")
    block = _js_block(js, "window.addEventListener('hashchange'")
    assert "closeVideo()" in block
    # 切集不改 hash，所以这里只看「hash 指向的视频」是否就是当前这集
    assert "sameVideo" in block


def test_exit_fullscreen_handles_native_video_fullscreen():
    """Android WebView 的原生视频全屏不进 document.fullscreenElement，得单独退出"""
    body = _js_function("exitPlayerFullscreen")
    assert "fullscreenElement" in body
    assert "webkitDisplayingFullscreen" in body
    assert "webkitExitFullscreen" in body


def test_fullscreen_end_is_cleaned_up_whatever_ends_it():
    """系统返回键退出全屏（不经过我们的按钮）也要收尾，否则几何/滚动锁留在旧视口"""
    js = _static_text("app.js")
    assert "videoEl.addEventListener('webkitendfullscreen'" in js
    assert "document.addEventListener('fullscreenchange'" in js
    body = _js_function("onFullscreenSettled")
    assert "body.style.overflow = ''" in body
    assert "classList.remove('video-open')" in body


def test_video_element_is_detached_on_close_and_reattached_on_load():
    """Android 7 的 <video> 是独立 SurfaceView：只靠父级 display:none 不一定拆干净"""
    assert "videoEl.style.display = 'none'" in _js_function("closeVideo")
    assert "videoEl.style.display = ''" in _js_function("loadVideo")


# ==================== 转码视频拖动不能从头播 ====================
#
# 真机：需要转码的片子点/拖进度条，有几率回到 00:00。两处成因：
# 1. 整集转码缓存刚好在播放中转完 → 服务端把整文件从头端过来（fMP4 没时长，
#    浏览器只能从 0 播），而前端还按「流从 start 开始」算 offset；
# 2. 时长没探到时滑块换算恒为 0，提交一次就等于让服务端从 0 重新起转。

def test_cached_transcode_honours_start():
    path = os.path.join(SRC_DIR, "web_server", "app.py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    body = text[text.index("def _play_cached"):]
    body = body[:body.index("def _stream_pipe")]
    assert "start > 0" in body, "缓存命中时要区分有没有 start"
    # 带 start 必须先切片；整文件只能发给 start <= 0 那支
    assert body.index("_stream_pipe") < body.index("_serve_file")


def test_stream_pipe_can_read_from_cache_file():
    path = os.path.join(SRC_DIR, "web_server", "app.py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "def _stream_pipe(self, video, mode, start, encoder, source=None)" in text
    assert "source or video[\"path\"]" in text


def test_transcode_uses_short_gop():
    """`-c:v copy` 切段只能从关键帧起：GOP 太大，拖动会落到十秒之前"""
    from web_server import transcode

    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf", None):
        args = transcode._video_args(encoder)
        assert "-g" in args, encoder
        gop = int(args[args.index("-g") + 1])
        assert 8 <= gop <= 120, f"{encoder}: GOP {gop} 帧太大，seek 落点会飘"


def test_seek_does_not_restart_when_duration_unknown():
    body = _js_function("commitVideoSeek")
    assert "if (!videoDuration())" in body
    # 这个拦截必须在 startStream 之前：否则 target 恒为 0，一拖就重连到 0
    assert body.index("if (!videoDuration())") < body.index("startStream(")


def test_cached_transcode_is_streamed_not_played_as_file():
    """缓存好了也不能当普通 mp4 播

    转码输出是 fMP4 分片，没有可 seek 的索引（实测 duration 只报 4.8s、
    seekable.end(0)=0，`currentTime = 60` 被忽略、画面退回 1.2s）。把它直接
    塞给 `<video>`，续播点和拖动都会变成「从头开始」——正是用户报的现象。
    """
    body = _js_function("loadVideo")
    assert "startStream(" in body, "非 direct 的集要走流"
    assert "mode === 'direct' || info.cached" not in body, \
        "缓存命中不能走直连分支（fMP4 seek 不了）"
    # info.cached 只留着决定要不要弹「开头等几秒」
    assert "Boolean(info.cached)" in body


# ==================== 客户端解码能力（v1.18.0） ====================
#
# 服务端的编码白名单只能是「所有浏览器都认」的保守集合（不含 HEVC），可手机、
# 平板大多带 HEVC 硬解。不在前端探一次、把结果带上去，一集 HEVC 的 mkv 就得
# 重编码十几分钟；带上之后只需要换封装（`-c:v copy`，几秒）。

def test_caps_are_probed_once_and_cached():
    js = _static_text("app.js")
    body = _js_block(js, "function detectVideoCaps(")
    assert "canPlayType" in body, "没有探测就是瞎猜"
    assert "localStorage" in body, "每次开播放器都重新探测，太浪费"
    assert "hevc" in body and "av1" in body
    # 探测结果按版本号缓存：以后探测逻辑改了，旧结果要能作废
    assert "lrm.videoCaps.v" in js


def test_caps_ride_along_on_every_video_request():
    js = _static_text("app.js")
    assert "function withCaps(" in js
    body = _js_function("loadVideo")
    assert "withCaps(" in body, "取播放信息时没带 caps，服务端只能按保守白名单判"
    url = _js_block(js, "function streamUrl(")
    assert "withCaps(" in url, "媒体流没带 caps，前面白探测了"


def test_forced_transcode_survives_seek_and_reconnect():
    """设备自称能解 HEVC、实际放不出来时，「转码播放」得真的转

    不带 `mode=transcode` 的话服务端仍按 caps 判成换封装，点了还是同一份
    放不出的流；而且拖动、跳片头重连也要沿用——否则拖一下又退回换封装。
    """
    js = _static_text("app.js")
    assert "state.video.forceMode = forceMode" in js
    url = _js_block(js, "function streamUrl(")
    assert "mode=" in url
    error_block = js[js.index("videoEl.addEventListener('error'"):]
    assert "startStream(videoTime(), false, 'transcode')" in error_block


def test_landscape_phones_get_compact_layout():
    """手机横过来只有 300~420px 高，顶栏 56px 就占了六分之一

    收一档之后卡片从 148px 缩到 104px、顶栏 56→46，一屏能多看一行。
    平板横屏高度够（≥560），不该被这条命中，否则大屏上内容反而变小。
    """
    css = _static_text("style.css")
    assert "orientation: landscape" in css
    assert "max-height: 560px" in css
    block = css[css.index("@media (orientation: landscape)"):]
    assert "--top-h: 46px" in block
    assert "minmax(104px, 1fr)" in block
    # 按钮缩小后圆角是 50%，不写死 px，所以不会退化成直角
    assert ".v-btn { width: 32px; height: 32px; }" in block


def test_wide_video_on_portrait_screen_hints_landscape():
    """竖屏拿手机看 16:9，上下黑边能占掉将近一半，值得提示一句"""
    js = _static_text("app.js")
    body = _js_block(js, "function hintLandscapeForWideVideo(")
    assert "videoWidth" in body, "要按视频真实宽高比判断，不能看容器尺寸"
    assert "1.4" in body, "方形/竖屏片子不该提示"
    assert "innerHeight" in body or "innerWidth > window.innerHeight" in body
    assert "landscapeHinted" in body, "每切一集都弹一次会很烦"


def test_api_video_info_uses_caps_query(server):
    """接口层：同一个 HEVC 文件，报不报 caps 拿到的播放方式不一样"""
    from video_manager import database as video_db

    port = server[1]
    work_id = library.video_works()[0]["id"]
    mkv = next(v for v in video_db.get_work_videos(work_id) if v["path"].endswith(".mkv"))
    video_db.update_video_meta(mkv["path"], codec="hevc")

    plain = json.loads(_get(port, f"/api/video/videos/{mkv['id']}", cookie=TOKEN)[2])
    assert plain["mode"] == http_util.MODE_TRANSCODE

    smart = json.loads(
        _get(port, f"/api/video/videos/{mkv['id']}?caps=h264,hevc", cookie=TOKEN)[2]
    )
    assert smart["mode"] == http_util.MODE_REMUX


# ==================== 列表搜索 / 排序（v1.18.1） ====================
#
# 排序值来自 query，会被拼进 ORDER BY —— 只能查表映射，绝不能原样拼 SQL。
# 这三个模块的四个选项与桌面端音频模块的下拉一致：
# 名称 A-Z / 名称 Z-A / 时间 新→旧 / 时间 旧→新。

def test_resolve_sort_only_accepts_whitelist():
    assert http_util.resolve_sort("time_desc") == "time_desc"
    assert http_util.resolve_sort(" NAME_DESC ") == "name_desc"
    assert http_util.resolve_sort("") == "name_asc"
    assert http_util.resolve_sort(None) == "name_asc"
    # 重点：任何不在白名单里的串（包括想混进 ORDER BY 的 SQL）都得被吃掉
    for hostile in ("name; DROP TABLE works", "mtime DESC", "1", "name_asc, (select 1)"):
        assert http_util.resolve_sort(hostile) == "name_asc"


def test_photo_works_orders(seeded):
    tmp = seeded["root"]
    for author, work, stamp in (("作者B", "作品B", 5000), ("作者C", "作品C", 1000)):
        image = _make_image(str(tmp / "写真" / author / work / "01.jpg"))
        photo_db.add_author_with_works(
            author, [(work, str(tmp / "写真" / author / work), "", [(image, stamp)])]
        )

    names = lambda page: [w["name"] for w in page["items"]]  # noqa: E731

    asc = names(library.photo_works_page(None, "", 1, 60, "name_asc"))
    assert asc == sorted(asc), "名称升序和桌面端作品墙不一致"

    desc = names(library.photo_works_page(None, "", 1, 60, "name_desc"))
    assert desc == list(reversed(asc)), "Z-A 不是 A-Z 的倒序"

    newest = names(library.photo_works_page(None, "", 1, 60, "time_desc"))
    assert newest[:2] == ["作品B", "作品C"], f"按时间新→旧排错：{newest}"
    oldest = names(library.photo_works_page(None, "", 1, 60, "time_asc"))
    assert oldest[:2] == ["作品1", "作品C"], f"按时间旧→新排错：{oldest}"


def test_video_works_orders(seeded):
    tmp = seeded["root"]
    for author, work, stamp in (("作者W", "作品W", 9000), ("作者X", "作品X", 100)):
        folder = tmp / "视频" / author / work
        folder.mkdir(parents=True)
        clip = folder / "01.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
        video_db.add_author_with_works(
            author, [(work, str(folder), "", [(str(clip), "01.mp4", stamp, clip.stat().st_size)])]
        )

    newest = [w["name"] for w in library.video_works_page(None, "", 1, 60, "time_desc")["items"]]
    assert newest[0] == "作品W", f"视频按时间新→旧排错：{newest}"
    name_desc = [w["name"] for w in library.video_works_page(None, "", 1, 60, "name_desc")["items"]]
    assert name_desc == sorted(name_desc, reverse=True)


def test_audio_playlists_search_and_sort(seeded):
    tmp = seeded["root"]
    audio_db.add_playlist("白噪音", str(tmp / "白噪音"))
    audio_db.add_playlist("抒情合集", str(tmp / "抒情合集"))
    audio_db.add_playlist("嵌套白噪音", str(tmp / "白噪音" / "嵌套"), parent_path=str(tmp / "白噪音"))

    hit = library.audio_playlists_page("", "白噪音", 1, 60)
    assert [p["name"] for p in hit["items"]] == ["白噪音"], "搜索应该只命中名字含关键词的"
    assert hit["total"] == 1

    # 子歌单不在顶层，搜顶层时不该冒出来；进到那一层才搜得到
    assert "嵌套白噪音" not in [p["name"] for p in library.audio_playlists_page("", "白噪音", 1, 60)["items"]]
    child = library.audio_playlists_page(str(tmp / "白噪音"), "白噪音", 1, 60)
    assert [p["name"] for p in child["items"]] == ["嵌套白噪音"]

    # LIKE 通配符要按字面匹配：搜 % 不能把全库捞出来
    assert library.audio_playlists_page("", "%", 1, 60)["total"] == 0

    desc = [p["name"] for p in library.audio_playlists_page("", "", 1, 60, "name_desc")["items"]]
    assert desc == sorted(desc, key=str.lower, reverse=True)


def test_list_endpoints_accept_sort_query(server):
    """接口层：sort 得真的透传到 SQL，而不是被路由丢掉"""
    port = server[1]
    for path in ("/api/photos/works", "/api/video/works", "/api/audio/playlists"):
        status, _, body = _get(port, f"{path}?sort=name_desc", cookie=TOKEN)
        assert status == 200, path
        data = json.loads(body)
        items = data.get("works") or data.get("playlists")
        assert items, f"{path} 没返回数据"
        names = [item["name"] for item in items]
        assert names == sorted(names, reverse=True), f"{path} 没按 Z-A 排"

    status, _, body = _get(port, "/api/audio/playlists?sort=%E4%B9%B1%E5%86%99", cookie=TOKEN)
    assert status == 200, "非法排序值应该被吃掉而不是 500"


def test_paged_grid_sends_sort_and_keeps_select():
    js = _static_text("app.js")
    fetch = _js_block(js, "function fetchPage(")
    assert "state.sort" in fetch and "params.set('sort'" in fetch, "列表请求没带排序"

    grid = _js_block(js, "function pagedGrid(")
    # 标题文字和排序下拉在同一行：applyTitle 直接 replaceChildren 会把下拉一起清掉
    assert "titleText.textContent" in grid
    assert "titleEl.replaceChildren" not in grid, "replaceChildren 会把排序下拉清掉"
    assert "options.sortable" in grid and "buildSortSelect()" in grid

    # 切排序不能整页重建（会重拉作者列表、闪一下）
    apply = _js_block(js, "function applySort(")
    assert "replaceHashQuery" in apply
    assert "replaceState" in _js_block(js, "function replaceHashQuery(")
    assert "location.hash =" not in apply, "改 hash 会触发 hashchange → render 整页重建"


def test_all_three_lists_are_searchable():
    js = _static_text("app.js")
    render_block = js[js.index("async function render()"):js.index("// ---------------- 首页")]
    for tab in ("photo", "audio", "video"):
        marker = f"tab: '{tab}', crumb: '', searchable: true"
        assert marker in render_block, f"{tab} 列表没开搜索"
    assert "searchPlaceholder" in render_block
    assert "搜索歌单" in render_block


# ==================== 音频标签筛选（v1.18.2） ====================
#
# 标签是 `tags` 字段里一段「空格/逗号/换行」混排的文本。桌面端把它拆成集合再比，
# 网页端必须用同一套拆分规则 + 同一种「都要有」的语义，否则同一个标签在手机和
# 电脑上点出来的歌单对不上。

def test_parse_tags_matches_desktop_rule():
    """分隔符：中英文逗号、换行、空白（桌面端 tag_widgets.parse_tags 同款）"""
    assert audio_db.parse_tags("舔耳,ASMR") == ["舔耳", "ASMR"]
    assert audio_db.parse_tags("舔耳，ASMR\n巨乳/爆乳") == ["舔耳", "ASMR", "巨乳/爆乳"]
    assert audio_db.parse_tags("  舔耳   舔耳  ") == ["舔耳"]
    assert audio_db.parse_tags("") == []
    assert audio_db.parse_tags(None) == []
    # 标签内部带斜杠/括号是常态，不能被拆开
    assert audio_db.parse_tags("双声道立体声/人头麦") == ["双声道立体声/人头麦"]


def test_audio_tag_counts_only_covers_current_level(seeded):
    """候选标签只统计这一层：子歌单的标签在列表里根本不出现，列出来只会点出空结果"""
    tmp = seeded["root"]
    audio_db.add_playlist("甲", str(tmp / "甲"), tags="舔耳 ASMR")
    audio_db.add_playlist("乙", str(tmp / "乙"), tags="舔耳")
    audio_db.add_playlist("甲子集", str(tmp / "甲" / "子"), tags="只属于子集", parent_path=str(tmp / "甲"))

    top = {tag["name"]: tag["count"] for tag in library.audio_tag_counts("")}
    assert top["舔耳"] == 2
    assert top["ASMR"] == 1
    assert "只属于子集" not in top

    child = {tag["name"]: tag["count"] for tag in library.audio_tag_counts(str(tmp / "甲"))}
    assert child["只属于子集"] == 1

    # 命中多的排前面：298 个标签按码点排等于没排，常用的得先露出来
    counts = [tag["count"] for tag in library.audio_tag_counts("")]
    assert counts == sorted(counts, reverse=True)


def test_audio_tags_filter_is_and(seeded):
    """多选标签 = 都要有（AND），与桌面端 `_playlist_matches_tags` 一致"""
    tmp = seeded["root"]
    audio_db.add_playlist("甲", str(tmp / "甲"), tags="舔耳 ASMR")
    audio_db.add_playlist("乙", str(tmp / "乙"), tags="舔耳")
    audio_db.add_playlist("丙", str(tmp / "丙"), tags="ASMR")

    def names(tags):
        page = library.audio_playlists_page("", "", 1, 60, "", tags)
        return [item["name"] for item in page["items"]]

    assert names(["舔耳"]) == ["乙", "甲"]
    assert names(["舔耳", "ASMR"]) == ["甲"]
    assert names(["ASMR", "舔耳"]) == ["甲"], "标签顺序不该影响结果"
    assert names(["舔耳", "不存在的标签"]) == []


def test_tag_match_is_exact_not_substring(seeded):
    """「治愈」不能把「治愈系」也捞出来——LIKE '%治愈%' 就会

    这就是为什么标签过滤不走 SQL LIKE，而是在 Python 里拆成集合取交集。
    """
    tmp = seeded["root"]
    audio_db.add_playlist("治愈系作品", str(tmp / "x1"), tags="治愈系")
    audio_db.add_playlist("治愈作品", str(tmp / "x2"), tags="治愈")

    page = library.audio_playlists_page("", "", 1, 60, "", ["治愈"])
    assert [item["name"] for item in page["items"]] == ["治愈作品"]


def test_tags_combine_with_search_and_sort(seeded):
    """标签与搜索、排序是叠加关系，不是互相顶掉"""
    tmp = seeded["root"]
    audio_db.add_playlist("甲作品", str(tmp / "甲"), tags="舔耳", mtime=100)
    audio_db.add_playlist("乙作品", str(tmp / "乙"), tags="舔耳", mtime=900)
    audio_db.add_playlist("丙作品", str(tmp / "丙"), tags="巨乳/爆乳", mtime=500)

    hit = library.audio_playlists_page("", "甲", 1, 60, "", ["舔耳"])
    assert [item["name"] for item in hit["items"]] == ["甲作品"]

    newest = library.audio_playlists_page("", "", 1, 60, "time_desc", ["舔耳"])
    assert [item["name"] for item in newest["items"]] == ["乙作品", "甲作品"]

    # 筛出来的总数要跟着变，否则「加载更多」会一直翻空页
    assert newest["total"] == 2


def test_api_audio_tags_and_filter(server, seeded):
    """接口层：/api/audio/tags 给候选，/api/audio/playlists?tags= 真的筛"""
    port = server[1]
    audio_db.add_playlist("舔耳合集", str(seeded["root"] / "舔耳合集"), tags="舔耳 ASMR")
    audio_db.add_playlist("只有巨乳", str(seeded["root"] / "只有巨乳"), tags="巨乳/爆乳")

    status, _, body = _get(port, "/api/audio/tags", cookie=TOKEN)
    assert status == 200
    payload = json.loads(body)
    names = {item["name"]: item["count"] for item in payload["tags"]}
    assert names == {"舔耳": 1, "ASMR": 1, "巨乳/爆乳": 1}

    def query(tags):
        status_, _, raw = _get(port, f"/api/audio/playlists?tags={quote(tags)}", cookie=TOKEN)
        assert status_ == 200
        return json.loads(raw)

    assert [item["name"] for item in query("舔耳")["playlists"]] == ["舔耳合集"]
    assert [item["name"] for item in query("舔耳,ASMR")["playlists"]] == ["舔耳合集"]
    assert [item["name"] for item in query("舔耳,巨乳/爆乳")["playlists"]] == []
    # 非法/超长输入不该 500：多传几个逗号而已
    assert query(",,,,,,")["total"] >= 1


def test_tag_picker_is_a_staged_sheet():
    """标签选择做成底部抽屉 + 暂存：点一下就发一次请求会让列表反复闪

    结构守卫而不是行为测试——老 WebView 上没有可跑的无头环境。
    """
    js = _static_text("app.js")
    sheet = _js_block(js, "function ensureTagSheet(")
    assert "sheet-mask" in sheet and "sheet-list" in sheet
    assert "open(" in sheet and "close(" in sheet
    # 勾选只改本地集合，确定才回调
    toggle = _js_block(sheet, "function toggle(")
    assert "onApply" not in toggle, "点一下标签就直接筛了，确定按钮就没意义了"
    assert "sheet.selected" in toggle

    ok = sheet[sheet.index("okBtn.addEventListener"):]
    assert "sheet.onApply(picked)" in ok

    # 列表项只改自己那一个 class，不重画 298 个按钮
    assert "sheet.nodes[name].classList.toggle" in toggle


def test_audio_list_wires_tags_into_request_and_hash():
    js = _static_text("app.js")
    grid = _js_block(js, "function pagedGrid(")
    assert "params.set('tags'" in grid, "列表请求没带标签"
    assert "replaceHashQuery('tags'" in grid, "标签没写进地址栏，从详情退回来就没了"
    assert "sort-wrap + .tag-btn" in _static_text("style.css"), "标签按钮得跟排序下拉并排"

    view = _js_block(js, "async function viewAudioRoot(")
    assert "taggable: true" in view and "/api/audio/tags" in view

    # 详情页的标签是能点的：点一下回列表按它筛（桌面端详情页也是这么用的）
    assert "function tagLinkChip(" in js
    assert "tagLinkChip" in _js_function("viewAudioPlaylist")
    assert "[,，\\n\\r\\s]+" in _js_function("splitTagText")


# ==================== 音视频下载（v1.10.3） ====================

def test_audio_track_download(server, seeded):
    """单曲下载：原文件直传 + attachment 头 + RFC 5987 中文文件名"""
    _instance, port = server
    status, headers, body = _get(port, "/api/audio/tracks/1/download", cookie=TOKEN)
    assert status == 200
    disp = headers.get("Content-Disposition", "")
    assert disp.startswith("attachment")
    assert "filename*=UTF-8''" in disp
    assert body.startswith(b"RIFF")


def test_audio_playlist_download_zip(server, seeded):
    """歌单打包下载：ZIP 内按「歌单名/曲目名」组织，内容是原文件"""
    import io
    import zipfile

    _instance, port = server
    status, headers, body = _get(port, "/api/audio/playlists/1/download", cookie=TOKEN)
    assert status == 200
    assert headers.get("Content-Type") == "application/zip"
    assert "attachment" in headers.get("Content-Disposition", "")
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()
    assert len(names) == 1
    assert names[0].startswith("歌单A/")
    assert zf.read(names[0]).startswith(b"RIFF")


def test_audio_playlist_download_missing(server, seeded):
    _instance, port = server
    status, _, _body = _get(port, "/api/audio/playlists/999/download", cookie=TOKEN)
    assert status == 404


def test_video_download_streams_original(server, seeded):
    """视频下载：原文件（非转码流）+ attachment，Content-Length 与响应体一致"""
    _instance, port = server
    status, headers, body = _get(port, "/api/video/videos/1/download", cookie=TOKEN)
    assert status == 200
    assert "attachment" in headers.get("Content-Disposition", "")
    assert body.startswith(b"\x00\x00\x00\x18ftypmp42")
    assert int(headers.get("Content-Length")) == len(body)


def test_media_audio_playback_not_attachment(server, seeded):
    """播放接口不带 attachment：不能因为加了下载就把 <audio> 播放变成另存为"""
    _instance, port = server
    _status, headers, _body = _get(port, "/media/audio/1", cookie=TOKEN)
    assert "Content-Disposition" not in headers


def test_web_download_ui_guard():
    """前端必须有单曲/歌单/视频三个下载入口"""
    js = _static_text("app.js")
    assert "/api/audio/tracks/${track.id}/download" in js
    assert "/api/audio/playlists/${playlistId}/download" in js
    assert "/api/video/videos/${video.id}/download" in js
    assert "row-dl" in js and "dl-btn" in js
    css = _static_text("style.css")
    assert ".dl-btn" in css and ".row-dl" in css
