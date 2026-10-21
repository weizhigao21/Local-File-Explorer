"""Windows 注册表集成：ProgID 结构、图标挂载与还原（临时项，测完清理）"""
import os
import sys

import pytest

import system_integration as si


@pytest.fixture(autouse=True)
def _no_shell_touch(monkeypatch):
    """测试绝不碰真实外壳：所有子进程调用（ie4uinit / taskkill / explorer）一律拦掉"""
    started = []

    def _fake_run(cmd, *args, **kwargs):
        started.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
        return

    def _fake_popen(cmd, *args, **kwargs):
        started.append(["Popen", *cmd] if isinstance(cmd, (list, tuple)) else ["Popen", cmd])
        return

    monkeypatch.setattr(si.subprocess, "run", _fake_run)
    monkeypatch.setattr(si.subprocess, "Popen", _fake_popen)
    return started


def test_supported_extensions_cover_common_formats():
    extensions = si.supported_extensions()
    assert ".mp3" in extensions
    assert ".flac" in extensions
    assert extensions == sorted(extensions)


def test_prog_id_is_per_extension():
    """图标是按 ProgID 挂的：共用同一个 ProgID 就没法给不同格式不同图标"""
    assert si.prog_id_for(".MP3") == "LocalResourceManager.Audio.mp3"
    assert si.prog_id_for("flac") == "LocalResourceManager.Audio.flac"
    assert si.prog_id_for(".mp3") != si.prog_id_for(".flac")
    assert si.prog_id_for(".mp3", base="Base") == "Base.mp3"


def test_launcher_command_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\DIY\本地资源管理器\本地资源管理器.exe")
    assert si.launcher_command() == '"C:\\DIY\\本地资源管理器\\本地资源管理器.exe" --play "%1"'


def test_launcher_command_source(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    command = si.launcher_command()
    assert "--play" in command
    assert "%1" in command
    assert "main.py" in command


def test_icon_paths_are_per_extension():
    assert si.icon_path(".FLAC").endswith("flac.ico")
    assert si.generic_icon_path().endswith("audio.ico")
    assert si.icon_path(".mp3", r"D:\icons") == os.path.join(r"D:\icons", "mp3.ico")


@pytest.mark.skipif(os.name != "nt", reason="Windows 注册表专有")
def test_register_and_unregister_round_trip():
    """真实写入 HKCU 的临时项：验证结构正确，最后完整清理"""
    import winreg

    app_name = "本地资源管理器-测试"
    prog_id = "LocalResourceManager.Audio.Pytest"
    root = winreg.HKEY_CURRENT_USER

    si.unregister(app_name, prog_id)
    try:
        ok, message = si.register(app_name, prog_id)
        assert ok, message
        assert si.is_registered(app_name, prog_id)

        # Capabilities 指向"每种格式自己的" ProgID
        with winreg.OpenKey(root, si.REGISTERED_APPS_PATH) as key:
            cap_path, _ = winreg.QueryValueEx(key, app_name)
        with winreg.OpenKey(root, cap_path + r"\FileAssociations") as key:
            assert winreg.QueryValueEx(key, ".mp3")[0] == f"{prog_id}.mp3"
            assert winreg.QueryValueEx(key, ".flac")[0] == f"{prog_id}.flac"

        # ProgID 打开命令
        with winreg.OpenKey(
            root, si.CLASSES_PATH + "\\" + f"{prog_id}.mp3" + r"\shell\open\command"
        ) as key:
            command = winreg.QueryValue(key, "")
        assert "--play" in command and "%1" in command

        # 扩展名的「打开方式」候选
        with winreg.OpenKey(
            root, si.CLASSES_PATH + r"\.mp3\OpenWithProgids"
        ) as key:
            winreg.QueryValueEx(key, f"{prog_id}.mp3")
    finally:
        ok, _message = si.unregister(app_name, prog_id)
        assert ok
        assert not si.is_registered(app_name, prog_id)

    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(root, si.CLASSES_PATH + "\\" + prog_id)


# ==================== 格式图标：安装 / 还原 ====================
_PROBE_BASE = "LocalResourceManager.IconProbe"
_PROBE_EXT = ".lrmiconprobe"
_STATE = r"Software\LocalResourceManager.FormatIcons.Pytest"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """隔离环境：独立 ProgID 前缀 + 独立状态键 + 临时图标目录"""
    monkeypatch.setattr(si, "ICON_STATE_PATH", _STATE)
    icon = tmp_path / "lrmiconprobe.ico"
    icon.write_bytes(b"fake-ico")
    yield {"dir": str(tmp_path), "icon": str(icon), "ext": _PROBE_EXT}
    si.remove_format_icons([_PROBE_EXT], base=_PROBE_BASE)
    si._delete_tree(si._winreg().HKEY_CURRENT_USER, _STATE)


@pytest.mark.skipif(os.name != "nt", reason="Windows 注册表专有")
def test_apply_icons_writes_progid_and_association(probe):
    import winreg

    report = si.apply_format_icons(probe["dir"], take_over=True,
                                   extensions=[probe["ext"]], base=_PROBE_BASE)
    assert report["ok"] is True
    assert [ext for ext, _ in report["applied"]] == [probe["ext"].lstrip(".")]
    assert report["taken_over"] == [probe["ext"].lstrip(".")]

    root = winreg.HKEY_CURRENT_USER
    prog = si.prog_id_for(probe["ext"], _PROBE_BASE)
    with winreg.OpenKey(root, rf"{si.CLASSES_PATH}\{prog}\DefaultIcon") as key:
        assert winreg.QueryValue(key, "") == f"{probe['icon']},0"
    with winreg.OpenKey(root, si.CLASSES_PATH + "\\" + probe["ext"]) as key:
        assert winreg.QueryValue(key, "") == prog
    with winreg.OpenKey(root, rf"{si.CLASSES_PATH}\{probe['ext']}\OpenWithProgids") as key:
        winreg.QueryValueEx(key, prog)


@pytest.mark.skipif(os.name != "nt", reason="Windows 注册表专有")
def test_remove_icons_restores_previous_state(probe):
    import winreg

    root = winreg.HKEY_CURRENT_USER
    # 先制造一个"原来就有的关联"，还原后必须一模一样
    with winreg.CreateKeyEx(root, si.CLASSES_PATH + "\\" + probe["ext"], 0,
                            winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "SomeOther.ProgId")

    si.apply_format_icons(probe["dir"], take_over=True,
                          extensions=[probe["ext"]], base=_PROBE_BASE)
    result = si.remove_format_icons([probe["ext"]], base=_PROBE_BASE)
    assert result["ok"] is True
    assert result["restored"] >= 1

    with winreg.OpenKey(root, si.CLASSES_PATH + "\\" + probe["ext"]) as key:
        assert winreg.QueryValue(key, "") == "SomeOther.ProgId", "原关联必须还原"
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(root, rf"{si.CLASSES_PATH}\{si.prog_id_for(probe['ext'], _PROBE_BASE)}")
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(root, _STATE)

    # 收尾：删掉测试自己造的 ProgID 引用
    si._delete_tree(root, si.CLASSES_PATH + "\\" + probe["ext"])


@pytest.mark.skipif(os.name != "nt", reason="Windows 注册表专有")
def test_icon_only_mode_skips_missing_icon_file(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "ICON_STATE_PATH", _STATE)
    report = si.apply_format_icons(str(tmp_path), take_over=False,
                                   extensions=[".lrmmissing"], base=_PROBE_BASE)
    assert report["skipped"], "没有 .ico 时必须如实报告，而不是静默成功"
    assert report["applied"] == []
    si._delete_tree(si._winreg().HKEY_CURRENT_USER, _STATE)


def test_format_icons_installed_false_when_never_applied(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "ICON_STATE_PATH", _STATE)
    assert si.format_icons_installed() is False


# ==================== 刷新链路：不再靠重启资源管理器 ====================
# 事故背景：为了试探"注册表改了图标却没变"，曾在几十秒内反复 taskkill explorer，
# 用户看到桌面连续闪断 3 次。下面四项守卫锁死这条路径。


def _module_source():
    import pathlib

    return pathlib.Path(si.__file__).read_text(encoding="utf-8")


def _function_body(source, name):
    marker = f"def {name}"
    assert marker in source, f"{name} 不见了"
    return source.split(marker, 1)[1].split("\ndef ", 1)[0]


def test_silent_refresh_never_kills_explorer(monkeypatch):
    """静默刷新只允许调 ie4uinit，绝不允许出现 taskkill / Popen"""
    calls = []
    monkeypatch.setattr(si.subprocess, "run",
                        lambda *a, **k: calls.append(" ".join(map(str, a[0]))))
    assert si.refresh_icon_cache() is True
    assert calls, "应当调用了刷新入口"
    assert all("taskkill" not in c for c in calls)
    assert any("ie4uinit" in c for c in calls)


def test_apply_and_remove_end_with_silent_refresh():
    """安装/还原都要顺手刷一次缓存，省得用户还得自己去重启"""
    source = _module_source()
    for name in ("apply_format_icons", "remove_format_icons"):
        assert "refresh_icon_cache()" in _function_body(source, name), \
            f"{name} 结束时应调用 refresh_icon_cache()"


def test_restart_explorer_refuses_during_cooldown(monkeypatch):
    """冷却期内必须直接拒绝，且不执行任何子进程"""
    import time

    monkeypatch.setattr(si, "_last_restart_ts", lambda: time.time())
    monkeypatch.setattr(si, "_set_value", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: calls.append(a))
    ok, message = si.restart_explorer()
    assert ok is False
    assert "刚重启过" in message
    assert calls == []


def test_restart_cooldown_is_at_least_a_minute():
    assert si.RESTART_COOLDOWN_SEC >= 60


def test_restart_explorer_returns_message_pair(monkeypatch):
    """返回值必须是 (bool, str)：界面要能把"为什么没执行"告诉用户"""
    monkeypatch.setattr(si, "_last_restart_ts", lambda: 0.0)
    monkeypatch.setattr(si, "_set_value", lambda *a, **k: None)
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(si.subprocess, "Popen", lambda *a, **k: None)
    result = si.restart_explorer()
    assert isinstance(result, tuple) and len(result) == 2
    assert result[0] is True and isinstance(result[1], str)


def test_dialog_prefers_silent_refresh():
    """界面上"刷新图标"按钮走静默刷新，重启只作兜底"""
    import pathlib

    source = (pathlib.Path(si.__file__).parent / "ui" / "system_dialog.py").read_text(
        encoding="utf-8")
    body = _function_body(source, "_clear_cache")
    assert "refresh_icon_cache()" in body
    assert "clear_icon_cache()" not in body
