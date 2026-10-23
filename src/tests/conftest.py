import os

import pytest

os.environ.setdefault("PYTEST_QT_API", "pyqt6")

# 离屏平台默认没有任何可用字体：此时 Qt 会退回一个「每字符 1em」的假度量，
# 任何依赖 QFontMetrics 的自适应逻辑（图标字号收缩、文本宽度布局）都会算出
# 与真机不符的结果。把系统字体目录指进去，测试环境才和真实运行一致。
if "QT_QPA_FONTDIR" not in os.environ:
    candidates = [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
        "/usr/share/fonts",
        "/System/Library/Fonts",
    ]
    for folder in candidates:
        if os.path.isdir(folder):
            os.environ["QT_QPA_FONTDIR"] = folder
            break


def _stub_marker(func):
    """给替换用的假函数打标记，守卫测试靠它确认拦截真的生效"""
    func._is_test_stub = True  # noqa: SLF001 - 测试基建的内部约定
    return func


@pytest.fixture(autouse=True)
def block_shell_notifications(monkeypatch):
    """全局安全网：任何测试都不许真的向 Windows 外壳发通知。

    `system_integration.notify_shell()` 用 ctypes 调
    `SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_FLUSH)` —— 这是一次**全局广播**，
    shell 会重新解析所有文件类型的图标并重建图标缓存，桌面上每个图标都重绘一遍。

    事故（2026-10-24 用户反馈「你改代码的时候我桌面图标疯狂闪烁」）：
    `test_system_integration.py` 的安全网只拦了 `subprocess`（ie4uinit / taskkill），
    ctypes 这一路漏了，于是一轮测试跑下来广播 6~8 次，用户桌面连续闪断。
    那一层现在也补上了，这里再加一层，未来新增的测试也不可能误伤真实外壳。
    """
    try:
        import system_integration as si
    except ImportError:  # 非 Windows / 模块缺失时无需拦截
        yield []
        return

    calls = []

    def fake_notify_shell():
        calls.append("notify_shell")
        return True

    # 守卫测试要验证"生产路径确实会广播"与"pytest 下自动空转"，
    # 得拿得到被替换掉的那份原始实现。
    fake_notify_shell._original = si.notify_shell  # noqa: SLF001
    monkeypatch.setattr(si, "notify_shell", _stub_marker(fake_notify_shell))
    yield calls
