"""字幕解析与配对测试（纯 Python，无 Qt）"""
import os

from audio_manager import subtitles as subs


def _write(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def test_parse_srt_with_crlf_tags_and_multiline(tmp_path):
    path = tmp_path / "a.srt"
    _write(path, (
        "1\r\n00:00:01,000 --> 00:00:03,500\r\n<i>第一句</i>\r\n第二行\r\n\r\n"
        "2\r\n00:00:04,000 --> 00:00:06,000\r\n下一句\r\n"
    ))
    track = subs.load_subtitle(str(path))
    assert len(track) == 2
    assert track.cues[0].start_ms == 1000
    assert track.cues[0].end_ms == 3500
    assert track.cues[0].text == "第一句\n第二行"
    assert track.format == "SRT"


def test_parse_vtt_skips_header_and_settings(tmp_path):
    path = tmp_path / "b.vtt"
    _write(path, (
        "WEBVTT\n\nNOTE 注释\n\n"
        "00:01.000 --> 00:02.500 align:start position:0%\n你好\n\n"
        "00:00:03.000 --> 00:00:04.000\n世界\n"
    ))
    track = subs.load_subtitle(str(path))
    assert [cue.text for cue in track.cues] == ["你好", "世界"]
    assert track.cues[0].start_ms == 1000
    assert track.cues[1].start_ms == 3000


def test_parse_lrc_multiple_tags_and_metadata(tmp_path):
    path = tmp_path / "c.lrc"
    _write(path, (
        "[ti:测试]\n[ar:某人]\n"
        "[00:01.50]第一句\n"
        "[00:05.00][00:10.00]重复句\n"
    ))
    track = subs.load_subtitle(str(path))
    assert len(track.cues) == 3
    assert track.cues[0].start_ms == 1500
    assert track.cues[0].end_ms == 5000
    assert track.cues[1].text == "重复句"
    assert track.cues[1].start_ms == 5000
    assert track.cues[2].start_ms == 10000
    assert track.cues[2].end_ms == 15000  # 最后一句默认 5 秒


def test_parse_ass_dialogue_with_commas_and_overrides(tmp_path):
    path = tmp_path / "d.ass"
    _write(path, (
        "[Script Info]\nTitle: x\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}第一句，带逗号\n"
        "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,上句\\N下一句\n"
    ))
    track = subs.load_subtitle(str(path))
    assert len(track) == 2
    assert track.cues[0].start_ms == 1000
    assert track.cues[0].text == "第一句，带逗号"
    assert track.cues[1].text == "上句\n下一句"


def test_gb18030_subtitle_is_decoded(tmp_path):
    path = tmp_path / "e.srt"
    _write(path, "1\n00:00:01,000 --> 00:00:02,000\n中文台词\n", encoding="gb18030")
    track = subs.load_subtitle(str(path))
    assert track.encoding == "gb18030"
    assert track.cues[0].text == "中文台词"


def test_timestamp_variants():
    assert subs.parse_timestamp("00:00:01,500") == 1500
    assert subs.parse_timestamp("0:00:01.05") == 1050
    assert subs.parse_timestamp("00:01.250") == 1250
    assert subs.parse_timestamp("00:01:02") is None  # 缺毫秒的 SRT 不合法
    assert subs.parse_timestamp("") is None


def test_index_at_handles_gaps_and_boundaries(tmp_path):
    path = tmp_path / "f.srt"
    _write(path, (
        "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nB\n"
    ))
    track = subs.load_subtitle(str(path))
    assert track.index_at(500) == -1
    assert track.index_at(1000) == 0
    assert track.index_at(2000) == 0
    assert track.index_at(2500) == -1  # 句间空隙不显示上一句
    assert track.index_at(5000) == 1
    assert track.index_at(99999) == -1
    assert track.cue_at(5000).text == "B"


def test_find_subtitle_prefers_exact_then_chinese_variant(tmp_path):
    audio = tmp_path / "03.mp3"
    audio.write_bytes(b"")

    exact = tmp_path / "03.srt"
    exact.write_text("", encoding="utf-8")
    assert subs.find_subtitle(str(audio)) == str(exact)

    exact.unlink()
    ja = tmp_path / "03.ja.srt"
    zh = tmp_path / "03.zh.srt"
    ja.write_text("", encoding="utf-8")
    zh.write_text("", encoding="utf-8")
    assert subs.find_subtitle(str(audio)) == str(zh)


def test_find_subtitle_single_file_in_folder(tmp_path):
    audio = tmp_path / "track01.mp3"
    audio.write_bytes(b"")
    only = tmp_path / "字幕.srt"
    only.write_text("", encoding="utf-8")
    assert subs.find_subtitle(str(audio)) == str(only)

    # 多个不同名字幕时不做猜测
    (tmp_path / "另一份.srt").write_text("", encoding="utf-8")
    assert subs.find_subtitle(str(audio)) is None


def test_find_subtitle_missing_folder_is_safe():
    assert subs.find_subtitle(os.path.join("Z:", "nope", "a.mp3")) is None
    assert subs.find_subtitle("") is None


def test_plain_text_needs_duration(tmp_path):
    path = tmp_path / "g.txt"
    _write(path, "第一行\n第二行\n第三行\n第四行\n")
    assert len(subs.load_subtitle(str(path))) == 0
    track = subs.load_subtitle(str(path), duration_ms=8000)
    assert len(track) == 4
    assert track.cues[1].start_ms == 2000
    assert track.cues[3].end_ms == 8000


def test_subtitles_module_has_no_qt_dependency():
    """静态守卫：字幕解析属于 core 层，不得引入 Qt（保证可脱离界面单测）"""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "audio_manager", "subtitles.py",
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "PyQt" not in text
