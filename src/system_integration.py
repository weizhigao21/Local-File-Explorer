"""
Windows 系统集成：可选播放器注册 + 音频文件格式图标

两件事都只写 HKCU（当前用户），不需要管理员权限，且都可完整撤销。

一、注册为可选播放器
    Windows 10/11 的默认应用由 `UserChoice` 保护（带哈希），第三方程序**无法静默**
    把自己设成默认——强行写入会被系统判定为篡改并重置。因此这里做的是正规注册：

        1. 每种格式一个 ProgID + shell\\open\\command  →「打开方式」里可用
        2. 每种音频扩展名的 OpenWithProgids             → 右键「打开方式」出现本程序
        3. Capabilities + RegisteredApplications        → 出现在「设置 → 默认应用」
        4. Applications\\<exe> + SupportedTypes          →「选择其他应用」列表里可见

    最后一步「设为默认」必须由用户在系统设置里点一次，程序只负责一键跳转。

二、让资源管理器显示本程序的格式图标
    实测（SHGetFileInfo + AssocQueryString，见 tests 与开发记录）确认了 shell 的
    图标解析规则：

        · 图标来自 ProgID 的 `DefaultIcon`，**不是**扩展名的 `DefaultIcon`；
        · `HKCR\\.ext\\DefaultIcon` 只在 ProgID 完全没有图标时才被采用；
        · `SystemFileAssociations` 里的图标不参与这条链路；
        · 最终用哪个 ProgID，由该格式当前的默认程序决定（UserChoice 优先）。

    所以想换掉图标，只有两条路：
        A. 把该格式的 ProgID 换成我们的（顺带接管打开方式）→ 图标和打开方式一起变；
        B. 保留原来的默认程序，去覆盖那个 ProgID 的 `DefaultIcon`
           （仅当那个 ProgID 只服务这一种格式时才安全，比如 `PotPlayerMini64.mp3`）。
    两种都按需实现，写入前先把原值记进备份，`remove_format_icons()` 可原样还原。
"""
import json
import os
import subprocess
import sys
from collections import Counter
from contextlib import suppress

APP_NAME = "本地资源管理器"
APP_DESCRIPTION = "本地音频/写真资源管理器与播放器"
PROG_ID = "LocalResourceManager.Audio"
REGISTERED_APPS_PATH = r"Software\RegisteredApplications"
CLASSES_PATH = r"Software\Classes"
# 图标安装的备份与状态（删除本键即可视为"没装过"）
ICON_STATE_PATH = r"Software\LocalResourceManager.FormatIcons"
# 每个扩展名的"用户已选定默认程序"（带哈希，只能由系统写；删掉即回落到默认 ProgID）
_USER_CHOICE_PATH = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                     r"\FileExts\.{}\UserChoice")


def supported_extensions():
    """与扫描器保持一致，避免注册了却扫不到"""
    from audio_manager.scanner import AUDIO_EXTENSIONS

    return sorted(AUDIO_EXTENSIONS)


def normalize_ext(ext: str) -> str:
    """'.MP3' / 'mp3' → 'mp3'"""
    return str(ext or "").strip().lower().lstrip(".")


def prog_id_for(ext: str, base: str = PROG_ID) -> str:
    """每种格式一个 ProgID：Windows 的图标是按 ProgID 挂的，共用就无法分格式"""
    return f"{base}.{normalize_ext(ext)}"


def is_supported():
    return os.name == "nt"


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir():
    """打包后是 exe 同级的 data/，与数据库、缩略图缓存保持同一套约定"""
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else _project_root())
    return os.path.join(base, "data")


def format_icon_dir():
    return os.path.join(_data_dir(), "format_icons")


def icon_path(ext: str, icon_dir=None) -> str:
    directory = icon_dir or format_icon_dir()
    return os.path.join(directory, f"{normalize_ext(ext)}.ico")


def generic_icon_path(icon_dir=None) -> str:
    directory = icon_dir or format_icon_dir()
    return os.path.join(directory, "audio.ico")


def executable_path():
    """打包后是 exe；源码模式返回 main.py（注册仅供开发调试）"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def application_icon():
    icon = os.path.join(_project_root(), "data", "ui.ico")
    return icon if os.path.isfile(icon) else executable_path()


def launcher_command():
    """文件关联的打开命令（`"%1"` 由系统替换为被双击的文件）"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --play "%1"'
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{python}" "{script}" --play "%1"'


def _capabilities_path(prog_id):
    return rf"Software\{prog_id}\Capabilities"


def _winreg():
    import winreg

    return winreg


def _set_value(root, path, name, value, kind):
    wr = _winreg()
    with wr.CreateKeyEx(root, path, 0, wr.KEY_WRITE) as key:
        wr.SetValueEx(key, name, 0, kind, value)


def _read_value(root, path, name=""):
    """读不到（键或值缺失）时返回 None"""
    wr = _winreg()
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_READ) as key:
            return wr.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _delete_value(root, path, name):
    wr = _winreg()
    with suppress(FileNotFoundError, OSError), \
            wr.OpenKey(root, path, 0, wr.KEY_SET_VALUE) as key:
        wr.DeleteValue(key, name)


def _delete_key_if_empty(root, path):
    """只在键确实为空时删除（DeleteKey 遇到子键/值会失败，正好当保护）"""
    wr = _winreg()
    with suppress(FileNotFoundError, OSError):
        wr.DeleteKey(root, path)


def _delete_tree(root, path):
    """递归删除整个键（键不存在时静默返回）"""
    wr = _winreg()
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_READ | wr.KEY_WRITE) as key:
            while True:
                try:
                    child = wr.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + child)
    except FileNotFoundError:
        return
    except OSError:
        return
    with suppress(FileNotFoundError, OSError):
        wr.DeleteKey(root, path)


# ==================== 关联来源：UserChoice / 扩展名默认值 ====================


def user_choice_prog_id(ext: str):
    """Windows 记录在 FileExts 下的"用户选定"默认程序（有哈希保护，不能伪造）"""
    if not is_supported():
        return None
    ext = normalize_ext(ext)
    return _read_value(
        _winreg().HKEY_CURRENT_USER,
        _USER_CHOICE_PATH.format(ext),
        "ProgId",
    )


def effective_prog_id(ext: str):
    """该扩展名实际会用的 ProgID：UserChoice 优先，其次 HKCR\\.ext 的默认值"""
    choice = user_choice_prog_id(ext)
    if choice:
        return choice
    if not is_supported():
        return None
    return _read_value(_winreg().HKEY_CLASSES_ROOT, "." + normalize_ext(ext)) or None


def _progid_icon(prog_id: str):
    return _read_value(_winreg().HKEY_CLASSES_ROOT, prog_id + r"\DefaultIcon")


# ==================== 写 ProgID ====================


def _write_ext_progid(ext: str, command: str, icon: str, base: str = PROG_ID):
    """写「本程序专用于该格式」的 ProgID：类型名 + 图标 + 打开命令"""
    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    prog_id = prog_id_for(ext, base)
    label = f"{normalize_ext(ext).upper()} 音频文件"
    with wr.CreateKeyEx(root, CLASSES_PATH + "\\" + prog_id, 0, wr.KEY_WRITE) as key:
        wr.SetValueEx(key, "", 0, wr.REG_SZ, label)
        wr.SetValueEx(key, "FriendlyTypeName", 0, wr.REG_SZ, label)
    _set_value(root, f"{CLASSES_PATH}\\{prog_id}\\DefaultIcon", "", f"{icon},0", wr.REG_SZ)
    _set_value(root, f"{CLASSES_PATH}\\{prog_id}\\shell\\open\\command", "", command, wr.REG_SZ)
    _set_value(root, f"{CLASSES_PATH}\\.{normalize_ext(ext)}\\OpenWithProgids",
               prog_id, b"", wr.REG_NONE)
    return prog_id


def _write_legacy_icon(icon: str):
    """旧版共享 ProgID 的图标：早期把 .mp3/.wav 设成默认的用户仍指向它"""
    wr = _winreg()
    _set_value(wr.HKEY_CURRENT_USER, f"{CLASSES_PATH}\\{PROG_ID}\\DefaultIcon", "",
               f"{icon},0", wr.REG_SZ)


# ==================== 注册为可选播放器 ====================


def ensure_format_icons(icon_dir=None):
    """确保每种格式各有自己的 .ico 落盘

    这一步不能省：注册时若 `<ext>.ico` 不存在，图标会退回程序主图标，
    22 种格式在资源管理器里就全长得一模一样（只显示"本程序"的图标）。
    返回写出的图标数量（导入 UI 依赖失败时返回 0，不影响注册本身）。
    """
    directory = icon_dir or format_icon_dir()
    try:
        from PyQt6.QtGui import QGuiApplication

        # 没有 GUI 实例时 QPixmap 会让进程直接 abort，先探一下
        if QGuiApplication.instance() is None:
            return 0
        from ui.format_icon_export import export_icons
    except Exception:  # noqa: BLE001 - 无 Qt 环境（如 CLI）时静默跳过
        return 0
    try:
        return len(export_icons(directory))
    except Exception:  # noqa: BLE001 - 绘制/写盘异常不该拖垮注册
        return 0


def register(app_name=APP_NAME, prog_id=PROG_ID):
    """注册为可选播放器；返回 (是否成功, 给用户看的说明)"""
    if not is_supported():
        return False, "系统集成仅支持 Windows。"
    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    command = launcher_command()
    fallback_icon = application_icon()
    extensions = supported_extensions()
    icon_dir = format_icon_dir()

    # 0) 先把格式图标导出：有没有它决定了"22 种各显示各的"还是"全显示程序图标"
    ensure_format_icons(icon_dir)

    # 1) 每种格式一个 ProgID：图标、打开命令、类型名都挂在它下面
    for ext in extensions:
        icon = icon_path(ext, icon_dir)
        if not os.path.isfile(icon):
            icon = fallback_icon
        _write_ext_progid(ext, command, icon, base=prog_id)
    generic = generic_icon_path(icon_dir)
    if prog_id == PROG_ID:
        _write_legacy_icon(generic if os.path.isfile(generic) else fallback_icon)

    # 2) Capabilities：让应用出现在「设置 → 默认应用」
    cap_path = _capabilities_path(prog_id)
    with wr.CreateKeyEx(root, cap_path, 0, wr.KEY_WRITE) as key:
        wr.SetValueEx(key, "ApplicationName", 0, wr.REG_SZ, app_name)
        wr.SetValueEx(key, "ApplicationDescription", 0, wr.REG_SZ, APP_DESCRIPTION)
        wr.SetValueEx(key, "ApplicationIcon", 0, wr.REG_SZ, f"{fallback_icon},0")
    for ext in extensions:
        _set_value(root, cap_path + r"\FileAssociations", ext,
                   prog_id_for(ext, prog_id), wr.REG_SZ)
    _set_value(root, REGISTERED_APPS_PATH, app_name, cap_path, wr.REG_SZ)

    # 3) 打包后的 exe 额外登记到「选择其他应用」列表
    if getattr(sys, "frozen", False):
        exe_name = os.path.basename(sys.executable)
        app_path = CLASSES_PATH + r"\Applications" + "\\" + exe_name
        _set_value(root, app_path + r"\shell\open\command", "", command, wr.REG_SZ)
        for ext in extensions:
            _set_value(root, app_path + r"\SupportedTypes", ext, "", wr.REG_SZ)

    notify_shell()
    return True, (
        f"已注册为可选音乐播放器（{len(extensions)} 种格式）。\n\n"
        "接下来请在弹出的 Windows「默认应用」里：\n"
        "  1. 找「本地资源管理器」→ 设为默认；或\n"
        "  2. 按文件类型（如 .mp3）→ 选择「本地资源管理器」。\n\n"
        "Windows 不允许程序直接抢占默认，这一步必须由你确认一次。\n"
        "设成本程序默认的那些格式，资源管理器里就会显示本程序的格式图标。"
    )


def unregister(app_name=APP_NAME, prog_id=PROG_ID):
    """撤销本程序写入的注册项（不会触碰系统已有的默认关联）"""
    if not is_supported():
        return False, "系统集成仅支持 Windows。"
    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    extensions = supported_extensions()

    _delete_value(root, REGISTERED_APPS_PATH, app_name)
    _delete_tree(root, rf"Software\{prog_id}")
    _delete_tree(root, CLASSES_PATH + "\\" + prog_id)
    for ext in extensions:
        _delete_tree(root, f"{CLASSES_PATH}\\{prog_id_for(ext, prog_id)}")
        _delete_value(root, f"{CLASSES_PATH}\\.{ext}\\OpenWithProgids",
                      prog_id_for(ext, prog_id))
    if getattr(sys, "frozen", False):
        exe_name = os.path.basename(sys.executable)
        _delete_tree(root, CLASSES_PATH + r"\Applications" + "\\" + exe_name)
    notify_shell()

    return True, "已取消注册。\n如果之前已把本程序设为默认，请在系统设置里改回其他播放器。"


def is_registered(app_name=APP_NAME, prog_id=PROG_ID):
    if not is_supported():
        return False
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, REGISTERED_APPS_PATH, 0, wr.KEY_READ) as key:
            value, _kind = wr.QueryValueEx(key, app_name)
        if not value:
            return False
        # Capabilities 与任一 ProgID 缺失都视为未完整注册
        wr.OpenKey(wr.HKEY_CURRENT_USER, value).Close()
        first = prog_id_for(supported_extensions()[0], prog_id)
        wr.OpenKey(wr.HKEY_CURRENT_USER, CLASSES_PATH + "\\" + first).Close()
        return True
    except OSError:
        return False


# ==================== 格式图标 ====================


def _load_state():
    """读取图标安装状态（备份表 + 图标目录 + 模式）"""
    raw = _read_value(_winreg().HKEY_CURRENT_USER, ICON_STATE_PATH, "Backup")
    try:
        backup = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        backup = {}
    return {
        "backup": backup if isinstance(backup, dict) else {},
        "icon_dir": _read_value(_winreg().HKEY_CURRENT_USER, ICON_STATE_PATH, "IconDir") or "",
        "mode": _read_value(_winreg().HKEY_CURRENT_USER, ICON_STATE_PATH, "Mode") or "",
    }


def _save_state(state):
    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    _set_value(root, ICON_STATE_PATH, "Backup",
               json.dumps(state.get("backup", {}), ensure_ascii=False), wr.REG_SZ)
    _set_value(root, ICON_STATE_PATH, "IconDir", state.get("icon_dir", ""), wr.REG_SZ)
    _set_value(root, ICON_STATE_PATH, "Mode", state.get("mode", ""), wr.REG_SZ)


def _remember(backup, root, path, name=""):
    """写入前先记下原值；同一个键只记第一次，卸载时原样还回去

    键里带 `|` 分隔路径与值名：注册表的默认值名为空串，若只存路径，
    还原时分不清"整个键"和"某个值"。
    """
    key = f"{path}|{name}"
    if key in backup:
        return
    backup[key] = _read_value(root, path, name)


def _write_with_backup(backup, path, name, value, kind):
    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    _remember(backup, root, path, name)
    _set_value(root, path, name, value, kind)


def format_icons_installed():
    """图标是否已经挂到各格式的 ProgID 上"""
    if not is_supported():
        return False
    state = _load_state()
    if not state["icon_dir"]:
        return False
    exts = supported_extensions()
    hits = 0
    for ext in exts:
        icon = _read_value(_winreg().HKEY_CLASSES_ROOT,
                           prog_id_for(ext) + r"\DefaultIcon")
        if icon and normalize_ext(state["icon_dir"]) in str(icon).lower():
            hits += 1
    return hits == len(exts)


def apply_format_icons(icon_dir=None, take_over=True, extensions=None, base=PROG_ID,
                       command=None):
    """把导出的 .ico 挂到各格式上。

    command：双击时的打开命令，默认按当前运行形态生成（打包版指向 exe 本身）。
             手工为另一份安装目录修图标时传入该 exe 的命令即可。
    take_over=True：换成我们的 ProgID（图标一定生效，但双击也用本程序打开；
                    早期版本留下的"公用 ProgID 锁定"会被自动拆成按格式的 ProgID）。
    take_over=False：保留当前默认程序，只覆盖它那个 ProgID 的图标；
                    如果该 ProgID 被多种格式共用，就无法区分图标，会被跳过。

    返回报告字典（给界面直接展示）：
        applied / pending / skipped 均为 [(ext, 说明)]，taken_over 为已接管的扩展名。
    """
    if not is_supported():
        return {"ok": False, "error": "仅支持 Windows", "applied": [], "pending": [],
                "skipped": [], "taken_over": []}

    wr = _winreg()
    icon_dir = icon_dir or format_icon_dir()
    exts = [normalize_ext(e) for e in (extensions or supported_extensions())]
    state = _load_state()
    backup = state["backup"]
    command = command or launcher_command()

    report = {"ok": True, "error": "", "applied": [], "pending": [], "skipped": [],
              "taken_over": [], "icon_dir": icon_dir, "mode": "takeover" if take_over else "icon"}

    for ext in exts:
        icon = icon_path(ext, icon_dir)
        if not os.path.isfile(icon):
            report["skipped"].append((ext, "缺少图标文件，请先导出图标"))
            continue
        _write_ext_progid(ext, command, icon, base=base)

    generic = generic_icon_path(icon_dir)
    if os.path.isfile(generic):
        _write_legacy_icon(generic)

    if take_over:
        for ext in exts:
            if any(ext == item for item, _ in report["skipped"]):
                continue
            icon = icon_path(ext, icon_dir)
            prog = prog_id_for(ext, base)
            choice = user_choice_prog_id(ext)
            if choice:
                if choice == prog:
                    report["applied"].append((ext, "已由本程序接管"))
                elif choice == base:
                    # 这是早期版本留下的"公用 ProgID"锁定：用户当初选的确实是本程序，
                    # 只是那时所有格式共用一个 ProgID，没法按格式分图标。
                    # 这里把扩展名改指向本格式专属 ProgID，并解除旧的 UserChoice 锁定。
                    # （只处理指向自己的锁，绝不动其他播放器的默认程序）
                    _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}", "", prog, wr.REG_SZ)
                    _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}\\DefaultIcon", "",
                                       f"{icon},0", wr.REG_SZ)
                    _delete_tree(wr.HKEY_CURRENT_USER,
                                 _USER_CHOICE_PATH.format(normalize_ext(ext)))
                    if user_choice_prog_id(ext) is None:
                        report["taken_over"].append(ext)
                        report["applied"].append((ext, "已从旧版公用 ProgID 拆成专属 ProgID"))
                    else:
                        # 键被系统守着删不掉：只能让用户重选一次，不能谎报成功
                        report["pending"].append(
                            (ext, "仍锁在旧版公用 ProgID 上，请在「默认应用」里重选一次本程序")
                        )
                else:
                    report["pending"].append(
                        (ext, f"Windows 已把默认程序固定为 {choice}，需在设置里重新选一次")
                    )
                continue
            # 没有 UserChoice：把扩展名默认值指向我们，图标立刻生效
            _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}", "", prog, wr.REG_SZ)
            _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}\\DefaultIcon", "",
                               f"{icon},0", wr.REG_SZ)
            report["taken_over"].append(ext)
            report["applied"].append((ext, "已换成格式图标并接管打开方式"))
    else:
        # 保留默认程序：只能去改那个 ProgID 的图标，且必须是一对一
        progs = {ext: effective_prog_id(ext) for ext in exts}
        shared = Counter(
            p for p in progs.values() if p and not p.startswith(PROG_ID)
        )
        for ext in exts:
            if any(ext == item for item, _ in report["skipped"]):
                continue
            icon = icon_path(ext, icon_dir)
            prog = progs.get(ext)
            if not prog:
                _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}", "",
                                   prog_id_for(ext, base), wr.REG_SZ)
                _write_with_backup(backup, f"{CLASSES_PATH}\\.{ext}\\DefaultIcon", "",
                                   f"{icon},0", wr.REG_SZ)
                report["applied"].append((ext, "原本没有关联，已直接使用格式图标"))
            elif prog == prog_id_for(ext, base):
                report["applied"].append((ext, "已由本程序接管，图标已生效"))
            elif prog.startswith(PROG_ID):
                report["pending"].append(
                    (ext, f"仍由旧的共享 ProgID {prog} 接管，请在设置里重新选一次本程序")
                )
            elif shared[prog] > 1:
                report["skipped"].append(
                    (ext, f"{prog} 同时服务多种格式，无法按格式区分图标")
                )
            else:
                _write_with_backup(backup, f"{CLASSES_PATH}\\{prog}\\DefaultIcon", "",
                                   f"{icon},0", wr.REG_SZ)
                report["applied"].append(
                    (ext, f"已换掉 {prog} 的图标，默认程序仍是 {prog}")
                )

    state.update({"backup": backup, "icon_dir": icon_dir, "mode": report["mode"]})
    _save_state(state)
    notify_shell()
    refresh_icon_cache()
    return report


def remove_format_icons(extensions=None, base=PROG_ID):
    """还原图标与关联：恢复备份的原值，删除我们建的 ProgID"""
    if not is_supported():
        return {"ok": False, "error": "仅支持 Windows", "restored": 0, "removed": 0}

    wr = _winreg()
    root = wr.HKEY_CURRENT_USER
    state = _load_state()
    restored = 0
    for key, value in state["backup"].items():
        path, _, name = key.partition("|")
        if value is None:
            _delete_value(root, path, name)
        else:
            _set_value(root, path, name, value, wr.REG_SZ)
        restored += 1

    exts = [normalize_ext(e) for e in (extensions or supported_extensions())]
    removed = 0
    for ext in exts:
        for path in (f"{CLASSES_PATH}\\{prog_id_for(ext, base)}",
                     f"{CLASSES_PATH}\\.{ext}\\DefaultIcon"):
            try:
                _delete_tree(root, path)
                removed += 1
            except OSError:
                pass
        _delete_value(root, f"{CLASSES_PATH}\\.{ext}\\OpenWithProgids",
                      prog_id_for(ext, base))

    for ext in exts:
        _delete_key_if_empty(root, rf"{CLASSES_PATH}\.{ext}")

    _delete_tree(root, ICON_STATE_PATH)
    notify_shell()
    refresh_icon_cache()
    return {"ok": True, "error": "", "restored": restored, "removed": removed}


# ==================== 让 shell 立刻刷新 ====================

# 结束 explorer.exe 会让桌面和任务栏整体闪断一次，属于"打扰用户"的操作。
# 教训：曾经为了试探图标缓存是否跟上，在几十秒内反复重启资源管理器，
# 用户看到的是桌面连续 3 次闪断。所以这里加两道闸——
#   1. 默认只走静默刷新（refresh_icon_cache），完全不结束进程；
#   2. 真要重启时，同一台机器 120 秒内只允许一次，重复调用直接拒绝。
RESTART_COOLDOWN_SEC = 120


def notify_shell():
    """通知资源管理器：关联变了（等价于改了默认应用后系统自动做的刷新）

    SHCNE_ASSOCCHANGED 是**全局广播**：shell 会重新读取所有文件类型的图标并
    重建图标缓存，桌面上每个图标都跟着重绘一遍。手动点一次没问题，
    但一秒内来几次，用户看到的就是"桌面图标疯狂闪烁"。

    所以测试环境（pytest 会设 PYTEST_CURRENT_TEST）直接空转：测试要验证的是
    注册表结构，没有任何理由去打扰真实外壳。测试侧另有一层 stub 兜着。
    """
    if not is_supported():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        # SHCNE_ASSOCCHANGED | SHCNF_FLUSH
        shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
        return True
    except Exception:
        return False


def refresh_icon_cache():
    """静默刷新图标缓存：让 explorer 重读图标，**不结束任何进程**

    `ie4uinit.exe -show` 是 Win10 1803+ 的系统自带入口，作用等价于用户手动
    F5/重开窗口时那次重读。绝大多数"注册表改了但图标没变"靠它就够。
    """
    if not is_supported():
        return False
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, timeout=15)
        return True
    return False


def clear_icon_cache():
    """重建图标缓存（静默刷新的加强版，仍然不结束进程）"""
    if not is_supported():
        return False
    ok = False
    for args in (["-ClearIconCache"], ["-show"]):
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["ie4uinit.exe", *args], capture_output=True, timeout=15)
            ok = True
    return ok


def _last_restart_ts():
    try:
        return float(_read_value(_winreg().HKEY_CURRENT_USER, ICON_STATE_PATH,
                                 "LastRestart") or 0)
    except (TypeError, ValueError):
        return 0.0


def restart_explorer(force: bool = False):
    """重启资源管理器：图标缓存最彻底的刷新方式（桌面与任务栏会闪断一次）

    返回 (ok, message)。冷却期内不执行，`force=True` 可跳过冷却（仅供人工确认后使用）。
    """
    if not is_supported():
        return False, "仅支持 Windows"
    if sys.platform != "win32":  # pragma: no cover - 仅 Windows 可达
        return False, "仅支持 Windows"

    import time as _time

    now = _time.time()
    last = _last_restart_ts()
    wait = RESTART_COOLDOWN_SEC - (now - last)
    if not force and last and wait > 0:
        return False, f"刚重启过，请等 {int(wait) + 1} 秒再试（频繁重启会让桌面反复闪断）"

    with suppress(OSError, subprocess.SubprocessError):
        _set_value(_winreg().HKEY_CURRENT_USER, ICON_STATE_PATH, "LastRestart",
                   str(now), _winreg().REG_SZ)
        subprocess.run(["taskkill", "/f", "/im", "explorer.exe"],
                       capture_output=True, timeout=15)
        subprocess.Popen(["explorer.exe"])  # noqa: S606 - 系统外壳，非用户输入
        return True, "已重启资源管理器"
    return False, "重启失败"


def open_default_apps_settings():
    """打开 Windows 默认应用设置页"""
    if not is_supported():
        return False
    try:
        os.startfile("ms-settings:defaultapps")
        return True
    except OSError:
        return False


__all__ = [
    "APP_NAME", "PROG_ID", "supported_extensions", "is_supported",
    "normalize_ext", "prog_id_for", "executable_path", "application_icon",
    "launcher_command", "format_icon_dir", "icon_path", "generic_icon_path",
    "register", "unregister", "is_registered", "ensure_format_icons",
    "user_choice_prog_id", "effective_prog_id",
    "apply_format_icons", "remove_format_icons", "format_icons_installed",
    "notify_shell", "refresh_icon_cache", "clear_icon_cache", "restart_explorer",
    "RESTART_COOLDOWN_SEC",
    "open_default_apps_settings",
]
