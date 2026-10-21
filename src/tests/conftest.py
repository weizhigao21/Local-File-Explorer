import os

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
