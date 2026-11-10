import json
import os
import sys

# ==================== 项目根路径（兼容 PyInstaller 打包） ====================

def get_project_root():
    """返回项目根目录，兼容开发模式和 PyInstaller frozen 模式"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：data/ 目录放在 exe 同级
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


# 项目根目录
_PROJECT_ROOT = get_project_root()

# 数据目录
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# 本地数据库路径
DB_PATH = os.path.join(_DATA_DIR, "resource_manager.db")

# DLsite 作品信息：独立数据库 + 封面缓存目录
DLSITE_DB_PATH = os.path.join(_DATA_DIR, "dlsite.db")
DLSITE_COVER_DIR = os.path.join(_DATA_DIR, "dlsite_covers")

# 缩略图缓存目录（在 data 下，与其他数据文件统一）
THUMBNAIL_DIR = os.path.join(_DATA_DIR, "thumbnails")

# ==================== 视频模块 ====================

# 视频库数据库（与写真、音频各自独立，互不影响彼此的迁移）
VIDEO_DB_PATH = os.path.join(_DATA_DIR, "video_manager.db")

# 视频封面（ffmpeg 抽帧）缓存目录
VIDEO_THUMB_DIR = os.path.join(_DATA_DIR, "video_thumbs")

# 网页端（手机/平板）缩图缓存：按 md5 + 宽度命名，同一张图只缩一次
WEB_CACHE_DIR = os.path.join(_DATA_DIR, "web_cache")

# 网页端允许的缩图宽度（前端只用这几档，防止被随意拉大当图床用）
# 1600 供「看图」用（手机/平板屏幕像素密度的合理上限），2048 供大屏平板
WEB_THUMB_WIDTHS = (320, 640, 960, 1280, 1600, 2048)

# 网页面板默认端口与上限
WEB_DEFAULT_PORT = 8735

# 支持的视频扩展名（扫描白名单）
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m2ts",
    ".m4v", ".webm", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp", ".vob",
}

# 作品级缩略图尺寸（用于作品卡片）
THUMBNAIL_SIZE = 240

# 单张图片缩略图持久化目录
IMAGE_THUMBNAIL_DIR = os.path.join(THUMBNAIL_DIR, "img")

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 网页端转码缓存：浏览器不认的编码（HEVC 等）转成 H.264 后存这里，
# 下次直接当普通 mp4 发（支持 Range，可拖动、可续播）
TRANSCODE_DIR = os.path.join(_DATA_DIR, "transcode")

# 转码缓存上限：超过就按最后访问时间淘汰最旧的。0 = 不限制
TRANSCODE_MAX_BYTES = 20 * 1024 ** 3

# 应用版本号
APP_VERSION = "1.17.3"

# ==================== 用户可变配置（config.json） ====================

# 用户配置文件路径
USER_CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")

# 默认配置（仅在 config.json 不存在时使用）
_DEFAULT_USER_CONFIG = {
    # 写真库根目录（可多个）：每个根目录下是 作者/作品/图片
    "PHOTO_ROOTS": [r"E:\hhh\写真"],
    "AUDIO_ROOTS": [r"E:\音频"],
    # 视频库根目录（可多个）：作者/作品/视频文件 三级结构（与写真同构）
    "VIDEO_ROOTS": [r"E:\hhh\视频"],
    "USE_GPU": True,
    "GPU_FRIENDLY_FORMAT": True,
    "UI_SCALE": 125,
    # UI 状态记忆
    "AUDIO_SORT_INDEX": 0,       # 音频排序: 0=名称A-Z, 1=名称Z-A, 2=时间新→旧, 3=时间旧→新
    "AUDIO_VIEW_MODE": 0,        # 音频视图: 0=网格, 1=列表
    # DLsite 抓取代理：空 = 自动（系统代理 → 直连 → 环境变量）
    "DL_PROXY": "",
    # 播放器行为
    "PLAYER_AUTO_QUEUE": False,      # 打开播放器时自动展开播放队列
    "PLAYER_ON_TOP": False,          # 播放器窗口始终置顶
    "PLAYER_EMBEDDED_COVER": True,   # 文件夹没封面时，用音频文件自带的内嵌封面
    # 视频播放器行为
    "VIDEO_RESUME": True,            # 记住每个视频看到哪，下次接着播
    "VIDEO_VOLUME": 80,              # 视频音量（0-100）
    "VIDEO_AUTOPLAY_NEXT": True,     # 一集播完自动播下一个
    "VIDEO_SPEED": 1.0,              # 上次使用的倍速
    "VIDEO_SKIP_OP": True,           # 自动跳过片头（按作品记一次，全剧通用）
    "VIDEO_SKIP_ED": True,           # 自动跳过片尾（跳完直接进下一集）
    # 手机/平板网页访问（内置 HTTP 服务）
    "WEB_PORT": WEB_DEFAULT_PORT,    # 监听端口
    "WEB_TOKEN": "",                 # 访问令牌，首次启动自动生成
    "WEB_ALLOW_LAN": True,           # True=局域网可访问；False=仅本机
    "WEB_AUTO_START": False,         # 程序启动时自动开服
    "WEB_REQUIRE_TOKEN": True,       # True=访问需要令牌；局域网自用可关闭
}

# 当前生效的用户配置（运行时可改，通过 save_user_config() 写盘）
_user_config = dict(_DEFAULT_USER_CONFIG)


def _normalize_roots(value, default_key):
    """把「多根目录」配置归一化为去重后的非空字符串列表

    兼容三种历史写法：单字符串（旧版 PHOTO_ROOT）、单元素列表、多元素列表。
    去重时比较 normpath，避免同一个目录被大小写/尾斜杠写出两份。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return list(_DEFAULT_USER_CONFIG[default_key])
    cleaned = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        key = os.path.normcase(os.path.normpath(text))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned or list(_DEFAULT_USER_CONFIG[default_key])


def _normalize_audio_roots(value):
    """把 AUDIO_ROOTS 配置归一化为非空字符串列表（兼容旧版单字符串）"""
    return _normalize_roots(value, "AUDIO_ROOTS")


def _normalize_video_volume(value):
    """视频音量归一化到 0-100 的整数（配置文件可能被手改成 "80"/800）"""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return _DEFAULT_USER_CONFIG["VIDEO_VOLUME"]


def _normalize_video_speed(value):
    """倍速归一化到 0.5-2.0（越界或非数字回落到 1.0）"""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return 1.0
    if speed <= 0:
        return 1.0
    return max(0.5, min(2.0, speed))


def _normalize_web_port(value):
    """网页端口归一化：非数字、越界或落在特权段（<1024）都回落到默认端口"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return WEB_DEFAULT_PORT
    if port < 1024 or port > 65535:
        return WEB_DEFAULT_PORT
    return port


def _normalize_web_token(value):
    """访问令牌只保留 URL 安全字符，去掉用户粘贴时带进来的空格/引号"""
    return "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in "-_")


def _load_user_config():
    """启动时从 config.json 加载用户配置，缺失字段用默认值"""
    global _user_config
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            # 仅接受已知字段，忽略未知键
            merged = dict(_DEFAULT_USER_CONFIG)
            for k, v in data.items():
                if k not in _DEFAULT_USER_CONFIG:
                    continue
                if k == "PHOTO_ROOTS":
                    v = _normalize_roots(v, "PHOTO_ROOTS")
                elif k == "AUDIO_ROOTS":
                    v = _normalize_audio_roots(v)
                elif k == "VIDEO_ROOTS":
                    v = _normalize_roots(v, "VIDEO_ROOTS")
                elif k == "VIDEO_VOLUME":
                    v = _normalize_video_volume(v)
                elif k == "VIDEO_SPEED":
                    v = _normalize_video_speed(v)
                elif k == "WEB_PORT":
                    v = _normalize_web_port(v)
                elif k == "WEB_TOKEN":
                    v = _normalize_web_token(v)
                elif k in ("WEB_ALLOW_LAN", "WEB_REQUIRE_TOKEN",
                           "VIDEO_SKIP_OP", "VIDEO_SKIP_ED"):
                    v = bool(v)
                elif k == "DL_PROXY":
                    v = str(v or "").strip()
                merged[k] = v
            # 兼容旧版单路径配置 → 迁移为多根列表
            if "PHOTO_ROOTS" not in data and "PHOTO_ROOT" in data:
                merged["PHOTO_ROOTS"] = _normalize_roots(data["PHOTO_ROOT"], "PHOTO_ROOTS")
            if "VIDEO_ROOTS" not in data and "VIDEO_ROOT" in data:
                merged["VIDEO_ROOTS"] = _normalize_roots(data["VIDEO_ROOT"], "VIDEO_ROOTS")
            if "AUDIO_ROOTS" not in data and "AUDIO_ROOT" in data:
                merged["AUDIO_ROOTS"] = _normalize_audio_roots(data["AUDIO_ROOT"])
            _user_config = merged
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] 加载用户配置失败，使用默认值: {e}")


def save_user_config():
    """把当前 _user_config 写回 config.json"""
    try:
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_user_config, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[config] 保存用户配置失败: {e}")


# 旧键 → 多根键：历史代码/旧配置文件里还有按单路径写的调用
_LEGACY_ROOT_KEYS = {"PHOTO_ROOT": "PHOTO_ROOTS", "VIDEO_ROOT": "VIDEO_ROOTS"}
_ROOT_KEYS = ("PHOTO_ROOTS", "AUDIO_ROOTS", "VIDEO_ROOTS")


def set_user_config(key: str, value):
    """更新用户配置项并立即生效（运行时改，需调用 save_user_config() 持久化）"""
    if key in _LEGACY_ROOT_KEYS:
        # 单路径写法按「只有一个根」处理，等价于把列表整个换掉
        value = _normalize_roots(value, _LEGACY_ROOT_KEYS[key])
        key = _LEGACY_ROOT_KEYS[key]
    if key not in _DEFAULT_USER_CONFIG:
        raise KeyError(f"未知配置项: {key}")
    if key in _ROOT_KEYS:
        value = _normalize_roots(value, key)
    elif key == "VIDEO_VOLUME":
        value = _normalize_video_volume(value)
    elif key == "VIDEO_SPEED":
        value = _normalize_video_speed(value)
    elif key == "WEB_PORT":
        value = _normalize_web_port(value)
    elif key == "WEB_TOKEN":
        value = _normalize_web_token(value)
    elif key in ("WEB_ALLOW_LAN", "WEB_REQUIRE_TOKEN",
                 "VIDEO_SKIP_OP", "VIDEO_SKIP_ED"):
        value = bool(value)
    elif key == "DL_PROXY":
        value = str(value or "").strip()
    _user_config[key] = value
    globals()[key] = value  # 同步模块级变量，方便 config.XXX 直接访问
    # 单路径别名恒等于第一个根目录（旧代码直接读 config.PHOTO_ROOT）
    if key == "PHOTO_ROOTS":
        globals()["PHOTO_ROOT"] = value[0]
    elif key == "AUDIO_ROOTS":
        globals()["AUDIO_ROOT"] = value[0]
    elif key == "VIDEO_ROOTS":
        globals()["VIDEO_ROOT"] = value[0]


# 启动时加载用户配置
_load_user_config()

# 暴露为模块级属性（兼容旧的 config.PHOTO_ROOT 访问方式：取第一个根目录）
PHOTO_ROOTS = list(_user_config["PHOTO_ROOTS"])
PHOTO_ROOT = PHOTO_ROOTS[0]
VIDEO_ROOTS = list(_user_config["VIDEO_ROOTS"])
VIDEO_ROOT = VIDEO_ROOTS[0]
AUDIO_ROOTS = list(_user_config["AUDIO_ROOTS"])
AUDIO_ROOT = AUDIO_ROOTS[0]  # 兼容旧代码：第一个音频根目录
USE_GPU = _user_config["USE_GPU"]
GPU_FRIENDLY_FORMAT = _user_config["GPU_FRIENDLY_FORMAT"]
UI_SCALE = _user_config["UI_SCALE"]
AUDIO_SORT_INDEX = _user_config["AUDIO_SORT_INDEX"]
AUDIO_VIEW_MODE = _user_config["AUDIO_VIEW_MODE"]
DL_PROXY = _user_config["DL_PROXY"]
PLAYER_AUTO_QUEUE = _user_config["PLAYER_AUTO_QUEUE"]
PLAYER_ON_TOP = _user_config["PLAYER_ON_TOP"]
PLAYER_EMBEDDED_COVER = _user_config["PLAYER_EMBEDDED_COVER"]
VIDEO_RESUME = _user_config["VIDEO_RESUME"]
VIDEO_VOLUME = _user_config["VIDEO_VOLUME"]
VIDEO_AUTOPLAY_NEXT = _user_config["VIDEO_AUTOPLAY_NEXT"]
VIDEO_SPEED = _user_config["VIDEO_SPEED"]
VIDEO_SKIP_OP = _user_config["VIDEO_SKIP_OP"]
VIDEO_SKIP_ED = _user_config["VIDEO_SKIP_ED"]
WEB_PORT = _user_config["WEB_PORT"]
WEB_TOKEN = _user_config["WEB_TOKEN"]
WEB_ALLOW_LAN = _user_config["WEB_ALLOW_LAN"]
WEB_AUTO_START = _user_config["WEB_AUTO_START"]
WEB_REQUIRE_TOKEN = _user_config["WEB_REQUIRE_TOKEN"]
