"""网页端播放方式判定与转码命令拼装

核心回归点：**HEVC 不能靠转封装救**。`-c:v copy` 只换容器，HEVC 原样进 fMP4
后手机 Chrome / WebView 依然解不了，用户点了「转封装」还是黑屏。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from tests import test_web_server as web_tests
from web_server import app as web_app
from web_server import http_util, library, transcode

# 复用 Web 服务那套临时库 fixture（同模块内定义，pytest 按名字就能找到）
env = web_tests.env
seeded = web_tests.seeded


# ==================== 播放方式判定 ====================

@pytest.mark.parametrize(
    "ext,codec,expected",
    [
        (".mp4", "h264", http_util.MODE_DIRECT),
        (".mp4", "", http_util.MODE_DIRECT),
        (".webm", "vp9", http_util.MODE_DIRECT),
        (".mkv", "h264", http_util.MODE_REMUX),
        (".avi", "mpeg4", http_util.MODE_TRANSCODE),
        (".ts", "h264", http_util.MODE_REMUX),
        # 关键的一档：容器是 mp4（浏览器认），编码是 HEVC（手机不认）
        (".mp4", "hevc", http_util.MODE_TRANSCODE),
        (".mp4", "HEVC", http_util.MODE_TRANSCODE),
        (".mkv", "hevc", http_util.MODE_TRANSCODE),
        (".wmv", "vc1", http_util.MODE_TRANSCODE),
        (".rmvb", "rv40", http_util.MODE_TRANSCODE),
    ],
)
def test_video_mode_classifies_container_and_codec_separately(ext, codec, expected):
    assert http_util.video_mode(ext, codec) == expected


def test_browser_playable_is_only_direct():
    """`playable` 的语义收窄成「原文件能直接交给 <video>」"""
    assert http_util.browser_playable(".mp4", "h264") is True
    assert http_util.browser_playable(".mp4", "hevc") is False
    assert http_util.browser_playable(".mkv", "h264") is False


# ==================== ffmpeg 命令 ====================

def test_remux_copies_video_stream():
    cmd = transcode.build_cmd("ffmpeg", "in.mkv", http_util.MODE_REMUX)
    assert "copy" in cmd
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd, "转封装不该重编码"
    assert cmd[-1] == "pipe:1"


def test_transcode_uses_chosen_encoder_with_quality_flag():
    cmd = transcode.build_cmd("ffmpeg", "in.mp4", http_util.MODE_TRANSCODE, encoder="h264_nvenc")
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert "-cq" in cmd, "NVENC 要有恒质量参数，否则码率乱飘"
    assert "copy" not in cmd
    # 10bit 片源必须降到 8bit，否则老浏览器还是不认
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_transcode_audio_is_always_aac():
    """DTS/AC3/FLAC 塞进 mp4 后浏览器大概率没声音"""
    for mode in (http_util.MODE_REMUX, http_util.MODE_TRANSCODE):
        cmd = transcode.build_cmd("ffmpeg", "in.mkv", mode)
        assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_seek_flag_goes_before_input():
    """`-ss` 放 `-i` 之后要先解码再丢弃，转码大文件会慢到不可用"""
    cmd = transcode.build_cmd("ffmpeg", "in.mp4", http_util.MODE_TRANSCODE, start=125.5)
    assert "-ss" in cmd
    assert cmd.index("-ss") < cmd.index("-i"), "-ss 必须在 -i 之前（快速 seek）"
    assert cmd[cmd.index("-ss") + 1] == "125.500"


def test_start_zero_adds_no_seek_flag():
    cmd = transcode.build_cmd("ffmpeg", "in.mp4", http_util.MODE_TRANSCODE, start=0)
    assert "-ss" not in cmd


def test_subtitle_and_data_streams_are_dropped():
    """mkv 里的 ASS 字幕塞进 mp4 会让封装直接报错"""
    cmd = transcode.build_cmd("ffmpeg", "in.mkv", http_util.MODE_REMUX)
    assert "-sn" in cmd and "-dn" in cmd


# ==================== 缓存 ====================

def test_cache_path_changes_when_source_changes(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    video = {"id": 7, "path": str(src)}
    first = transcode.cache_path(video, str(tmp_path))

    assert first.endswith(".mp4")
    assert "7-" in os.path.basename(first)

    src.write_bytes(b"x" * 20)  # 换了文件就得换缓存名，否则播到旧片
    second = transcode.cache_path(video, str(tmp_path))
    assert second != first


def test_cache_path_missing_source_is_empty(tmp_path):
    assert transcode.cache_path({"id": 1, "path": str(tmp_path / "nope.mp4")}, str(tmp_path)) == ""


def test_sweep_evicts_oldest_and_never_touches_part_files(tmp_path):
    for index in range(3):
        path = tmp_path / f"{index}.mp4"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + index, 1000 + index))
    part = tmp_path / "busy.mp4.part"
    part.write_bytes(b"x" * 100)

    removed = transcode.sweep(str(tmp_path), max_bytes=250)

    assert removed == 1
    assert not (tmp_path / "0.mp4").exists(), "最旧的应该先被删"
    assert (tmp_path / "2.mp4").exists()
    assert part.exists(), "正在写的 .part 不能删，会打断当前播放"


def test_sweep_noop_when_under_limit(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x" * 10)
    assert transcode.sweep(str(tmp_path), max_bytes=10**6) == 0


# ==================== 后台整集转码任务 ====================
#
# 转码不能挂在某一次 HTTP 连接上：用户切集/退出会 kill ffmpeg，缓存永远建不起来；
# 从续播点看时 `-ss` 只转后半段，也拼不成完整剧集。

@pytest.fixture(autouse=True)
def _clean_jobs():
    transcode.reset_jobs()
    yield
    transcode.reset_jobs()


def _stub_ffmpeg(payload=b"y" * 4096):
    """用一个马上退出的 python 进程冒充 ffmpeg，验证任务生命周期而非转码本身"""
    code = f"import sys; sys.stdout.buffer.write({payload!r})"
    return [sys.executable, "-c", code]


def test_ensure_job_lands_cache_file(tmp_path, monkeypatch):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    video = {"id": 1, "path": str(src)}
    monkeypatch.setattr(transcode, "build_cmd", lambda *a, **k: _stub_ffmpeg())

    job, has_cache = transcode.ensure_job(video, "ffmpeg", "libx264", str(tmp_path))
    assert job is not None and has_cache is False
    assert job.done.wait(30), "后台任务没在 30 秒内结束"

    final = transcode.cache_path(video, str(tmp_path))
    assert job.ok is True
    assert os.path.isfile(final), "转完的结果没有 rename 成正式缓存"
    assert not os.path.exists(final + ".part"), "临时文件没清掉"
    with open(final, "rb") as handle:
        assert handle.read() == b"y" * 4096


def test_ensure_job_skips_when_cache_exists(tmp_path, monkeypatch):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    video = {"id": 2, "path": str(src)}
    final = transcode.cache_path(video, str(tmp_path))
    with open(final, "wb") as handle:
        handle.write(b"done")

    job, has_cache = transcode.ensure_job(video, "ffmpeg", "libx264", str(tmp_path))
    assert job is None and has_cache is True


def test_ensure_job_reuses_running_job(tmp_path, monkeypatch):
    """同一集连点两次播放不能起两个 ffmpeg，否则 GPU 直接被打满"""
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    video = {"id": 3, "path": str(src)}
    monkeypatch.setattr(
        transcode, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(1)"],
    )

    first, _ = transcode.ensure_job(video, "ffmpeg", "libx264", str(tmp_path))
    second, _ = transcode.ensure_job(video, "ffmpeg", "libx264", str(tmp_path))
    assert first is second, "重复请求起了第二个转码任务"
    first.done.wait(30)


def test_ensure_job_failure_removes_part_file(tmp_path, monkeypatch):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    video = {"id": 4, "path": str(src)}
    monkeypatch.setattr(
        transcode, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import sys; sys.exit(3)"],
    )

    job, _ = transcode.ensure_job(video, "ffmpeg", "libx264", str(tmp_path))
    assert job.done.wait(30)
    assert job.ok is False
    final = transcode.cache_path(video, str(tmp_path))
    assert not os.path.exists(final), "失败的转码不该留下缓存文件"
    assert not os.path.exists(final + ".part"), "失败的 .part 没清掉"


# ==================== 路由与分流 ====================

def test_stream_route_is_registered():
    patterns = [pattern for _method, pattern, _name in web_app._ROUTE_DEFS]
    assert any("video-stream" in p for p in patterns)
    # 老地址保留兼容，前端缓存里的旧页面还能用
    assert any("video-remux" in p for p in patterns)


def _fake_stream(record):
    def handler(self, video, mode, start, encoder=None):
        record.update(mode=mode, start=start, video=video["id"], encoder=encoder)
        return self._json({"mode": mode, "start": start})

    return handler


def test_stream_direct_for_h264_mp4(seeded):
    """直连要真发文件：206 + Accept-Ranges，拖动靠它"""
    instance = web_app.WebServer()
    port = instance.start(port=0, lan=False, token="tok")
    try:
        data = json.loads(web_tests._get(port, "/api/video/works", cookie="tok")[2])
        work_id = data["works"][0]["id"]
        videos = json.loads(web_tests._get(port, f"/api/video/works/{work_id}/videos", cookie="tok")[2])
        mp4 = [v for v in videos["videos"] if v["ext"] == ".mp4"][0]

        status, headers, body = web_tests._get(
            port, f"{mp4['stream_url']}?start=30", cookie="tok", headers={"Range": "bytes=0-3"}
        )
        assert status == 206, "直连模式带不带 start 都该走 Range 分支"
        assert headers["Accept-Ranges"] == "bytes"
        assert body == b"\x00\x00\x00\x18"
    finally:
        instance.stop()


def test_stream_remux_for_mkv(seeded, monkeypatch):
    record = {}
    monkeypatch.setattr(web_app.RequestHandler, "_stream_pipe", _fake_stream(record))
    # 后台整集转码与编码器探测都不真跑：这里只验分流，不验 ffmpeg
    monkeypatch.setattr(web_app.transcode, "ensure_job", lambda *a, **k: (None, False))
    monkeypatch.setattr(web_app.transcode, "pick_encoder", lambda *a, **k: "libx264")

    instance = web_app.WebServer()
    port = instance.start(port=0, lan=False, token="tok")
    try:
        data = json.loads(web_tests._get(port, "/api/video/works", cookie="tok")[2])
        work_id = data["works"][0]["id"]
        videos = json.loads(web_tests._get(port, f"/api/video/works/{work_id}/videos", cookie="tok")[2])
        mkv = [v for v in videos["videos"] if v["ext"] == ".mkv"][0]

        status, _headers, body = web_tests._get(port, f"{mkv['stream_url']}?start=42", cookie="tok")
        assert status == 200
        assert record["mode"] == http_util.MODE_REMUX
        assert record["start"] == 42.0
        assert json.loads(body)["mode"] == "remux"
    finally:
        instance.stop()


def test_stream_transcode_for_hevc(seeded, monkeypatch):
    """容器是 mp4 但编码是 HEVC：必须走重编码，不能只换封装"""
    record = {}
    monkeypatch.setattr(web_app.RequestHandler, "_stream_pipe", _fake_stream(record))
    # 后台整集转码与编码器探测都不真跑：这里只验分流，不验 ffmpeg
    monkeypatch.setattr(web_app.transcode, "ensure_job", lambda *a, **k: (None, False))
    monkeypatch.setattr(web_app.transcode, "pick_encoder", lambda *a, **k: "libx264")

    loaded = library.video_by_id(library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"])
    from video_manager import database as video_db

    video_db.update_video_meta(loaded["path"], codec="hevc")

    instance = web_app.WebServer()
    port = instance.start(port=0, lan=False, token="tok")
    try:
        status, _headers, body = web_tests._get(port, f"/media/video-stream/{loaded['id']}", cookie="tok")
        assert status == 200
        assert record["mode"] == http_util.MODE_TRANSCODE
        assert record["start"] == 0.0
    finally:
        instance.stop()


def test_stream_serves_cache_when_present(seeded, monkeypatch, tmp_path):
    """转好了就直接当普通文件发：有 Content-Length，能拖能续播"""
    record = {}
    monkeypatch.setattr(web_app.RequestHandler, "_stream_pipe", _fake_stream(record))
    # 后台整集转码与编码器探测都不真跑：这里只验分流，不验 ffmpeg
    monkeypatch.setattr(web_app.transcode, "ensure_job", lambda *a, **k: (None, False))
    monkeypatch.setattr(web_app.transcode, "pick_encoder", lambda *a, **k: "libx264")
    monkeypatch.setattr(web_app.config, "TRANSCODE_DIR", str(tmp_path))

    from video_manager import database as video_db

    loaded = library.video_by_id(library.video_work_videos(library.video_works()[0]["id"])["videos"][0]["id"])
    video_db.update_video_meta(loaded["path"], codec="hevc")
    cached = transcode.cache_path(loaded, str(tmp_path))
    with open(cached, "wb") as handle:
        handle.write(b"converted")

    instance = web_app.WebServer()
    port = instance.start(port=0, lan=False, token="tok")
    try:
        status, headers, body = web_tests._get(port, f"/media/video-stream/{loaded['id']}", cookie="tok")
        assert status == 200
        assert body == b"converted"
        assert headers["Content-Length"] == "9"
        assert record == {}, "命中缓存不该再起 ffmpeg"
    finally:
        instance.stop()


def test_api_reports_mode_for_each_video(seeded):
    """/api/video/... 要给前端 mode，前端才知道该走哪条路"""
    instance = web_app.WebServer()
    port = instance.start(port=0, lan=False, token="tok")
    try:
        data = json.loads(web_tests._get(port, "/api/video/works", cookie="tok")[2])
        work_id = data["works"][0]["id"]
        videos = json.loads(web_tests._get(port, f"/api/video/works/{work_id}/videos", cookie="tok")[2])
        modes = {v["ext"]: v["mode"] for v in videos["videos"]}
        assert modes[".mp4"] == http_util.MODE_DIRECT
        assert modes[".mkv"] == http_util.MODE_REMUX
        for video in videos["videos"]:
            assert video["stream_url"].startswith("/media/video-stream/")
    finally:
        instance.stop()
