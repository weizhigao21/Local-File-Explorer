"""守卫：测试环境绝不允许向 Windows 外壳广播通知。

事故（2026-10-24 用户反馈「你改代码的时候我桌面图标还是疯狂闪烁」）：
`system_integration.notify_shell()` 用 ctypes 调
`SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_FLUSH)` —— 这是**全局广播**，
shell 收到后重新解析所有文件类型的图标、重建图标缓存，桌面上每个图标重绘一遍。

`test_system_integration.py` 当年的安全网只拦了 `subprocess`（ie4uinit / taskkill），
ctypes 这一路完全没拦。而该文件里有 6 个用例会走 register / unregister /
apply_format_icons / remove_format_icons，每个末尾都调 notify_shell()，
于是一轮 pytest 广播 6~8 次 —— 用户看到的就是桌面连续闪断。

现场证据（排查方法，下次复现照此办理）：
- explorer 进程启动时间没变、Application 日志无 1002 事件 → **不是 explorer 重启**
- 但 `%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer\\iconcache_*.db` 的 mtime
  在跑测试的时间点被反复刷新 → 是图标缓存被重建，即收到关联变更广播

三道防线，缺一不可：
1. `notify_shell()` 在 pytest 下直接空转（生产代码自身兜底）
2. `tests/conftest.py` 全局 autouse fixture 把 notify_shell 换成 stub
3. `test_system_integration.py::_no_shell_touch` 局部再拦一层
"""
import ctypes
import inspect
import pathlib

import pytest

import system_integration as si


def _original_notify_shell():
    """拿到被 conftest stub 替换掉的原始实现"""
    stub = si.notify_shell
    original = getattr(stub, "_original", None)
    assert original is not None, "conftest 的 stub 必须把原函数挂在 _original 上"
    return original


class _FakeShell32:
    def __init__(self):
        self.calls = []

    def SHChangeNotify(self, *args):
        self.calls.append(args)


@pytest.fixture
def fake_windll(monkeypatch):
    """把 ctypes.windll 换成假的，观察是否会真的广播（不触发任何真实刷新）"""
    fake = _FakeShell32()
    holder = type("FakeWindll", (), {"shell32": fake})()
    monkeypatch.setattr(ctypes, "windll", holder, raising=False)
    return fake


# ==================== 防线 1：生产代码在 pytest 下空转 ====================

def test_notify_shell_skips_under_pytest(monkeypatch, fake_windll):
    """带 PYTEST_CURRENT_TEST 时必须直接返回，一次 API 都不调"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::t (call)")
    assert _original_notify_shell()() is False
    assert fake_windll.calls == []


def test_notify_shell_really_broadcasts_when_enabled(monkeypatch, fake_windll):
    """去掉 pytest 标记后确实会广播 SHCNE_ASSOCCHANGED（0x08000000）

    反向证明防线 1 不是"这段代码根本不干活"，否则上面的测试毫无意义。
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert _original_notify_shell()() is True
    assert len(fake_windll.calls) == 1
    event_id, flags = fake_windll.calls[0][0], fake_windll.calls[0][1]
    assert event_id == 0x08000000  # SHCNE_ASSOCCHANGED
    assert flags == 0x1000         # SHCNF_FLUSH


# ==================== 防线 2：conftest 全局 stub ====================

def test_conftest_stubs_notify_shell():
    """全局安全网必须生效：任何测试里 notify_shell 都是假函数"""
    assert getattr(si.notify_shell, "_is_test_stub", False), (
        "测试环境里 notify_shell 必须是 stub：",
        "ctypes 广播会让 shell 重建全部图标缓存、桌面图标全量重绘",
    )


def test_stub_reports_success_so_callers_are_unaffected():
    """stub 要返回 True：调用方（register/apply）不该因为测试而走进失败分支"""
    assert si.notify_shell() is True


# ==================== 防线 3：模块内 fixture 也要拦 ====================

def test_no_shell_touch_fixture_also_blocks_ctypes():
    """只拦 subprocess 不够——ctypes 广播照样刷用户桌面"""
    source = pathlib.Path(__file__).with_name("test_system_integration.py").read_text(
        encoding="utf-8")
    marker = "def _no_shell_touch"
    assert marker in source
    body = source.split(marker, 1)[1].split("\ndef ", 1)[0]
    assert "notify_shell" in body, "_no_shell_touch 必须同时拦截 notify_shell"


def test_system_integration_module_keeps_the_pytest_guard():
    """生产代码里的 PYTEST_CURRENT_TEST 判断是最后一道保险，不能被删掉"""
    source = pathlib.Path(si.__file__).read_text(encoding="utf-8")
    body = source.split("def notify_shell", 1)[1].split("\ndef ", 1)[0]
    assert "PYTEST_CURRENT_TEST" in body


def test_notify_shell_is_never_called_via_subprocess():
    """通知走的是 ctypes API，不是命令行——确保没人改成 shell 调用"""
    body = inspect.getsource(_original_notify_shell())
    assert "SHChangeNotify" in body
    assert "subprocess" not in body
