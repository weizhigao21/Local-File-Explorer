# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

# 项目根目录（spec 文件所在目录）
ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'src' / 'main.py')],
    pathex=[str(ROOT / 'src')],
    binaries=[],
    datas=[
        # 应用图标
        (str(ROOT / 'data' / 'ui.ico'), 'data'),
        # 网页端静态资源（手机/平板访问的页面），必须随包带上，
        # 否则打包后「手机访问」一打开就是 404
        (str(ROOT / 'src' / 'web_server' / 'static'), 'web_server/static'),
    ],
    hiddenimports=[
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'pygame',
        'PIL._tkinter_finder',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='本地资源管理器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / 'data' / 'ui.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='本地资源管理器',
)