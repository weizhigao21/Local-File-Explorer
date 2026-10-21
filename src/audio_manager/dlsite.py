"""
DLsite 作品详情抓取（中文版）。

由 G:\\code\\逆向\\japaneseasmr\\dlsite_detail.py 改造而来：
- 去掉命令行入口，保留 fetch/parse 核心
- 新增 extract_rj_code()（从歌单名提取 RJ 码）
- 新增 download_cover()（下载封面到本地缓存目录）
"""
import json
import os
import re
import threading
import time
from contextlib import suppress

from resource_manager import config

# 网络依赖可选：缺失时本模块降级为不可用（AVAILABLE=False），不影响主程序
try:
    from bs4 import BeautifulSoup
    from curl_cffi import requests
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

BASE_URL = "https://www.dlsite.com/maniax/work/=/product_id/{}.html"
HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
# locale=zh-cn 是关键：切换 DLsite 为简体中文（分类等字段返回中文）
COOKIES = {"locale": "zh-cn", "adultchecked": "1"}

DEFAULT_TIMEOUT = 30
PROBE_TIMEOUT = 8
MAX_RETRIES = 2
RETRY_DELAY = 1.5
RETRY_COOLDOWN = 60  # 全部代理都失败后，60 秒内不再重复探测

# 代理探测状态。None 表示尚未探测；"" 表示直连可用；其他为代理 URL。
# 多线程共享，用锁保护，避免 4 个抓取线程同时探测。
_proxy_lock = threading.Lock()
_proxy_resolved = None
_proxy_probed_at = 0.0
_proxy_error = ""
_last_error = ""
_log_callback = None


def set_log_callback(callback):
    """把探测/失败信息接到界面日志（传 None 取消）"""
    global _log_callback
    _log_callback = callback


def _log(message):
    print(f"[DLsite] {message}")
    if _log_callback is not None:
        with suppress(Exception):
            _log_callback(message)

# RJ 码（兼容新旧位数，如 RJ01196889 / RJ283610）
# 注意：不能用 \b 词边界——Python 的 \w 把 CJK 汉字当单词字符，
# "RJ01155419付" 中数字与汉字之间不构成 \b 会导致提取失败。
# 这里只要求 RJ 后面不是紧跟更多数字即可。
_RJ_RE = re.compile(r"(RJ\d{6,10})(?!\d)", re.IGNORECASE)


def extract_rj_code(name: str) -> str | None:
    """从歌单名/文件夹名中提取 RJ 码，无则返回 None（统一大写）"""
    if not name:
        return None
    m = _RJ_RE.search(name)
    return m.group(1).upper() if m else None


def _normalize_proxy(server, enabled=True):
    """把 Windows 的 ProxyServer 值规范成 URL；无效返回空串

    注册表可能是 "127.0.0.1:7897"，也可能是
    "http=127.0.0.1:7897;https=127.0.0.1:7897" 这种按协议分列的形式。
    """
    if not enabled or not server:
        return ""
    text = str(server).strip()
    if not text:
        return ""
    if "=" in text:
        parts = {}
        for item in text.split(";"):
            if "=" in item:
                name, value = item.split("=", 1)
                parts[name.strip().lower()] = value.strip()
        text = parts.get("https") or parts.get("http") or ""
        if not text:
            return ""
    if "://" not in text:
        text = "http://" + text
    return text


def system_proxy():
    """读取 Windows 系统代理（与浏览器/IE 设置一致）；非 Windows 返回空串"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except (ImportError, OSError, ValueError, IndexError):
        return ""
    return _normalize_proxy(server, enabled)


def env_proxy():
    """环境变量代理（curl 默认会读取，这里显式取出来参与排序）"""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value if "://" in value else "http://" + value
    return ""


def proxy_candidates():
    """返回 [(代理 URL 或 "", 说明), ...] 与模式名

    顺序：用户指定 > 系统代理 > 直连 > 环境变量。
    直连排在环境变量之前，是因为不少桌面环境里 HTTP_PROXY 是给命令行工具用的
    转发地址（甚至是失效的转发服务），而浏览器能上网靠的是系统代理。
    """
    explicit = (getattr(config, "DL_PROXY", "") or "").strip()
    if explicit:
        return [(explicit, "指定代理")], "manual"

    candidates = []
    sys_proxy = system_proxy()
    if sys_proxy:
        candidates.append((sys_proxy, "系统代理"))
    candidates.append(("", "直连"))
    env = env_proxy()
    if env and all(env != proxy for proxy, _ in candidates):
        candidates.append((env, "环境变量代理"))
    return candidates, "auto"


def _short_error(exc):
    """把底层异常翻译成用户能看懂的一句话"""
    text = str(exc)
    lower = text.lower()
    if "connect tunnel failed" in lower or "response 502" in lower:
        return "代理隧道失败（代理服务未运行或拒绝转发）"
    if "could not connect" in lower or "connection refused" in lower or "failed to connect" in lower:
        return "无法连接代理（端口未监听或地址错误）"
    if "timed out" in lower or "timeout" in lower:
        return "连接超时"
    if "could not resolve" in lower or "name resolution" in lower:
        return "域名解析失败"
    if "ssl" in lower or "certificate" in lower:
        return "TLS/证书错误"
    return f"{type(exc).__name__}: {text[:120]}"


def _request(url, proxy, timeout, cookies=True):
    """发一次请求；proxy 为空串时显式禁用环境变量代理"""
    kwargs = {
        "impersonate": "chrome",
        "headers": HEADERS,
        "timeout": timeout,
        "proxy": proxy or "",  # 空串 = 直连，避免 libcurl 自动套用失效的环境代理
    }
    if cookies:
        kwargs["cookies"] = COOKIES
    return requests.get(url, **kwargs)


def _reset_proxy_cache():
    global _proxy_resolved, _proxy_probed_at, _proxy_error
    with _proxy_lock:
        _proxy_resolved = None
        _proxy_probed_at = 0.0
        _proxy_error = ""


def _probe(url):
    """依次尝试候选代理，返回 (可用代理, 首次响应 HTML 或 None)

    调用方必须已持有 _proxy_lock：4 个抓取线程只允许一个去探测，
    其余线程等待结果，避免同时打 4 轮请求。
    """
    global _proxy_resolved, _proxy_probed_at, _proxy_error
    candidates, _mode = proxy_candidates()
    errors = []
    for proxy, label in candidates:
        try:
            response = _request(url, proxy, PROBE_TIMEOUT, cookies=False)
            if response.status_code == 200 and len(response.content) > 1000:
                _proxy_resolved = proxy
                _proxy_probed_at = time.time()
                _proxy_error = ""
                where = proxy or "直连"
                _log(f"网络通道就绪（{label}: {where}）")
                return proxy, response.text
            errors.append(f"{label}: HTTP {response.status_code}")
        except Exception as e:
            errors.append(f"{label}: {_short_error(e)}")
    _proxy_resolved = ""
    _proxy_probed_at = time.time()
    _proxy_error = "；".join(errors) or "无可用网络通道"
    _log(f"网络探测失败：{_proxy_error}")
    return "", None


def _resolve_proxy(url):
    """返回 (代理, 探测时顺带拿到的 HTML)；失败时 HTML 为 None"""
    with _proxy_lock:
        stale_error = bool(_proxy_error) and time.time() - _proxy_probed_at > RETRY_COOLDOWN
        if _proxy_resolved is not None and not stale_error:
            return _proxy_resolved, None
        return _probe(url)


def last_error():
    """最近一次失败原因（供后台线程写进日志）"""
    return _last_error or _proxy_error


def reset_network_state():
    """清空代理缓存与错误（用户改代理/网络恢复后调用，下一次抓取重新探测）"""
    global _last_error
    _last_error = ""
    _reset_proxy_cache()


def fetch(rj: str, delay: float = RETRY_DELAY, retries: int = MAX_RETRIES) -> str | None:
    """抓取详情页 HTML；失败返回 None，原因见 last_error()

    代理只探测一次并缓存；探测成功后直接复用。若请求过程中失败，清空缓存，
    下一次抓取重新探测（例如用户刚刚打开/切换了代理）。
    """
    global _last_error
    url = BASE_URL.format(rj)
    proxy, cached = _resolve_proxy(url)
    if cached:
        _last_error = ""
        return cached
    if _proxy_error and not proxy:
        _last_error = _proxy_error
        return None

    failure = ""
    for attempt in range(max(1, int(retries))):
        try:
            response = _request(url, proxy, DEFAULT_TIMEOUT)
            if response.status_code == 200:
                _last_error = ""
                return response.text
            failure = f"HTTP {response.status_code}"
        except Exception as e:
            failure = _short_error(e)
        if attempt + 1 < retries:
            time.sleep(delay)
    _last_error = failure or "网络请求失败"
    _reset_proxy_cache()  # 下次抓取重新探测（代理可能刚恢复或被切换）
    return None


def parse(html: str, rj: str) -> dict:
    """解析 DLsite 中文详情页，提取结构化信息"""
    soup = BeautifulSoup(html, "html.parser")
    d = {"rj_code": rj}

    # 标题
    h1 = soup.select_one("#work_name")
    d["title"] = h1.get_text(strip=True) if h1 else None

    # 信息表 #work_outline（中/日文表头均兼容）
    table = soup.select_one("#work_outline")
    if table:
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            key = th.get_text(strip=True)
            val = td.get_text(" ", strip=True)
            if key in ("发售日", "販売日"):
                d["release_date"] = val
            elif key in ("声优", "声優"):
                d["cv"] = val
            elif key in ("插画", "イラスト"):
                d["illustrator"] = val
            elif key in ("年龄指定", "年齢指定"):
                d["age_rating"] = val
            elif key in ("作品形式",):
                d["work_type"] = val
            elif key in ("文件形式", "ファイル形式"):
                d["file_type"] = val
            elif key in ("文件容量", "ファイル容量"):
                d["file_size"] = val
            elif key in ("分类", "ジャンル"):
                d["genres"] = [a.get_text(strip=True) for a in td.select("a") if a.get_text(strip=True)]

    # 圈子名：面包屑 JSON-LD 第 3 项（中文版 breadcrumb）
    crumb = soup.select_one("script[type='application/ld+json']")
    if crumb:
        try:
            ld = json.loads(crumb.string or "{}")
            if isinstance(ld, dict) and ld.get("@type") == "BreadcrumbList":
                items = ld.get("itemListElement", [])
                for it in items:
                    if it.get("position") == 3:
                        d["circle"] = it.get("name")
        except json.JSONDecodeError:
            pass

    # 封面 + 价格 + maker_id：页面底部 GA 埋点 var contents = {...}
    m = re.search(r"var contents = (\{.*?\});\s*</script>", html, re.S)
    if m:
        try:
            ga = json.loads(m.group(1))
            if ga.get("detail"):
                it = ga["detail"][0]
                if it.get("image_main"):
                    d["cover"] = "https:" + it["image_main"]
                d["price"] = it.get("price")
                d["work_type_code"] = it.get("work_type")
                d["lang_options"] = it.get("lang_options")
                d["brand"] = it.get("brand")  # maker/circle ID
        except json.JSONDecodeError:
            pass

    # 简介：itemprop="description" 下的 work_parts 分段
    desc = soup.select_one('[itemprop="description"]')
    if desc:
        parts = []
        for wp in desc.select(".work_parts"):
            heading_el = wp.select_one(".work_parts_heading")
            text_el = wp.select_one(".work_parts_multitype_item.type_text")
            t = text_el.get_text("\n", strip=True) if text_el else ""
            parts.append({"heading": heading_el.get_text(strip=True) if heading_el else "", "text": t})
        d["description"] = parts

    # 体验版下载
    trial = soup.select_one(".trial_file a")
    if trial and trial.get("href"):
        d["trial"] = "https:" + trial["href"]

    return d


def download_cover(cover_url: str, dest_path: str) -> bool:
    """下载封面图片到本地路径，成功返回 True（已存在则直接复用）"""
    if not cover_url:
        return False
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True
    proxy, _cached = _resolve_proxy(cover_url)
    if _proxy_error and not proxy:
        return False
    try:
        r = _request(cover_url, proxy, DEFAULT_TIMEOUT, cookies=False)
        if r.status_code == 200 and r.content:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            tmp = dest_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, dest_path)
            return True
    except Exception:
        pass
    return False
