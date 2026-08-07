import os
import sys
import json


# ==================== 项目根路径（兼容 PyInstaller 打包） ====================

def get_project_root():
    """返回项目根目录，兼容开发模式和 PyInstaller frozen 模式"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：data/ 目录放在 exe 同级
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


# 项目根目录
_PROJECT_ROOT = get_project_root()

# 数据目录
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# 本地数据库路径
DB_PATH = os.path.join(_DATA_DIR, "resource_manager.db")

# 缩略图缓存目录（在 data 下，与其他数据文件统一）
THUMBNAIL_DIR = os.path.join(_DATA_DIR, "thumbnails")

# 作品级缩略图尺寸（用于作品卡片）
THUMBNAIL_SIZE = 240

# 单张图片缩略图持久化目录
IMAGE_THUMBNAIL_DIR = os.path.join(THUMBNAIL_DIR, "img")

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 应用版本号
APP_VERSION = "1.1.8"

# ==================== 用户可变配置（config.json） ====================

# 用户配置文件路径
USER_CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")

# 默认配置（仅在 config.json 不存在时使用）
_DEFAULT_USER_CONFIG = {
    "PHOTO_ROOT": r"E:\hhh\写真",
    "AUDIO_ROOTS": [r"E:\音频"],
    "USE_GPU": True,
    "GPU_FRIENDLY_FORMAT": True,
    "UI_SCALE": 125,
    # UI 状态记忆
    "AUDIO_SORT_INDEX": 0,       # 音频排序: 0=名称A-Z, 1=名称Z-A, 2=时间新→旧, 3=时间旧→新
    "AUDIO_VIEW_MODE": 0,        # 音频视图: 0=网格, 1=列表
}

# 当前生效的用户配置（运行时可改，通过 save_user_config() 写盘）
_user_config = dict(_DEFAULT_USER_CONFIG)


def _normalize_audio_roots(value):
    """把 AUDIO_ROOTS 配置归一化为非空字符串列表（兼容旧版单字符串）"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return list(_DEFAULT_USER_CONFIG["AUDIO_ROOTS"])
    cleaned = [p.strip() for p in value if isinstance(p, str) and p.strip()]
    return cleaned or list(_DEFAULT_USER_CONFIG["AUDIO_ROOTS"])


def _load_user_config():
    """启动时从 config.json 加载用户配置，缺失字段用默认值"""
    global _user_config
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 仅接受已知字段，忽略未知键
            merged = dict(_DEFAULT_USER_CONFIG)
            for k, v in data.items():
                if k not in _DEFAULT_USER_CONFIG:
                    continue
                if k == "AUDIO_ROOTS":
                    v = _normalize_audio_roots(v)
                merged[k] = v
            # 兼容旧版单路径配置 AUDIO_ROOT → 迁移为 AUDIO_ROOTS
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


def set_user_config(key: str, value):
    """更新用户配置项并立即生效（运行时改，需调用 save_user_config() 持久化）"""
    if key not in _DEFAULT_USER_CONFIG:
        raise KeyError(f"未知配置项: {key}")
    if key == "AUDIO_ROOTS":
        value = _normalize_audio_roots(value)
    _user_config[key] = value
    globals()[key] = value  # 同步模块级变量，方便 config.XXX 直接访问
    if key == "AUDIO_ROOTS":
        # 保持 AUDIO_ROOT = 第一个根目录（兼容旧代码）
        globals()["AUDIO_ROOT"] = value[0]


# 启动时加载用户配置
_load_user_config()

# 暴露为模块级属性（兼容旧的 config.PHOTO_ROOT 访问方式）
PHOTO_ROOT = _user_config["PHOTO_ROOT"]
AUDIO_ROOTS = list(_user_config["AUDIO_ROOTS"])
AUDIO_ROOT = AUDIO_ROOTS[0]  # 兼容旧代码：第一个音频根目录
USE_GPU = _user_config["USE_GPU"]
GPU_FRIENDLY_FORMAT = _user_config["GPU_FRIENDLY_FORMAT"]
UI_SCALE = _user_config["UI_SCALE"]
AUDIO_SORT_INDEX = _user_config["AUDIO_SORT_INDEX"]
AUDIO_VIEW_MODE = _user_config["AUDIO_VIEW_MODE"]
