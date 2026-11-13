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
# locale=zh-cn 是唯一可靠的简体中文开关（实测：URL 上的 ?locale=zh_CN 无效，
# 不带这个 cookie 一律返回日文页）。每个请求都必须带上，包括网络探测那一次。
COOKIES = {"locale": "zh-cn", "adultchecked": "1"}

# ── 页面语言判定 ──
# DLsite 在 <html lang> 上直接标明语言：中文 zh-CN、日文 ja-JP。
# 表头词只作兜底（个别页面没有 lang 属性），中/日两套表头互斥，不会同时出现。
_HTML_LANG_RE = re.compile(r"<html[^>]*\blang\s*=\s*[\"']([^\"']+)[\"']", re.I)
_ZH_HEADERS = ("发售日", "声优", "分类", "插画", "文件形式", "文件容量", "年龄指定")
_JA_HEADERS = ("販売日", "声優", "ジャンル", "イラスト", "ファイル形式", "ファイル容量", "年齢指定")

DEFAULT_TIMEOUT = 30
PROBE_TIMEOUT = 8
MAX_RETRIES = 2
RETRY_DELAY = 1.5
RETRY_COOLDOWN = 60  # 全部代理都失败后，60 秒内不再重复探测

# ── 站点级故障判定 ──
# DLsite 对高频抓取的限流不返回 429，而是让所有作品页一律 404。这种失败与
# "作品本身不存在"必须区分开：一次限流若按作品级失败逐个入库，整个队列都会
# 被写进 24h 冷却。窗口内同一非 200 状态累计达到阈值即判定为站点级。
SITE_FAIL_WINDOW = 120.0  # 判定窗口（秒）
SITE_FAIL_THRESHOLD = 3   # 窗口内同一状态的出现次数阈值

# DLsite 对已下架（販売停止）作品的 404 错误页特征串。
# 实测页面标题「エラー: この作品は現在販売されていません」——这是作品级的
# 永久状态，不是限流，绝不能计入站点级判定。
SUSPENDED_MARKER = "販売されていません"


def _is_suspended_page(response):
    """是否为"作品已停止販売"的 404 错误页"""
    return (
        getattr(response, "status_code", None) == 404
        and SUSPENDED_MARKER in (response.text or "")
    )


_status_lock = threading.Lock()
_status_events = []  # [(time.time(), status_code)]
_site_level = False


def _note_http_status(status):
    """记录一次非 200 的 HTTP 状态，滚动窗口内同一状态达标则判定站点级故障"""
    global _site_level
    with _status_lock:
        now = time.time()
        _status_events.append((now, status))
        while _status_events and now - _status_events[0][0] > SITE_FAIL_WINDOW:
            _status_events.pop(0)
        _site_level = sum(1 for _, s in _status_events if s == status) >= SITE_FAIL_THRESHOLD
        if _site_level:
            _log(f"短时间内多个请求返回 HTTP {status}，判定为站点级拒绝（疑似限流），本轮失败不计入 24h 冷却")


def is_site_level_failure():
    """最近一轮失败是否为站点级（限流/封锁）；调用方据此决定是否写失败缓存"""
    with _status_lock:
        return _site_level


def _clear_site_status():
    global _site_level
    with _status_lock:
        _status_events.clear()
        _site_level = False

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

def page_locale(html: str) -> str:
    """判断详情页语言：返回 "zh"（简体中文）/ "ja"（日文）/ ""（无法判断）

    优先看 <html lang>，缺失时用信息表表头词兜底。"" 表示页面既没有 lang
    也没有可识别的表头——调用方按"放行"处理，避免把正常页面误判成日文。
    """
    if not html:
        return ""
    m = _HTML_LANG_RE.search(html)
    if m:
        lang = m.group(1).strip().lower()
        if lang.startswith("zh"):
            return "zh"
        if lang.startswith("ja"):
            return "ja"
    zh = sum(html.count(word) for word in _ZH_HEADERS)
    ja = sum(html.count(word) for word in _JA_HEADERS)
    if zh > ja:
        return "zh"
    if ja > zh:
        return "ja"
    return ""


# RJ 码识别已抽到零依赖的 audio_manager/rj_code.py（见该文件顶部说明）：
# 本模块顶层 import 了 curl_cffi + bs4，纯正则不该被它连带。
# 这里保留同名 re-export，`dlsite.extract_rj_code` 的既有调用方与测试不受影响。
from audio_manager.rj_code import _RJ_RE, extract_rj_code  # noqa: E402,F401  (re-export)


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

    注意 cookies=True：探测拿到的 HTML 会被 fetch() 直接当结果返回，
    早年这里传 False，导致每次重新探测后的第一条作品都落成日文页。
    """
    global _proxy_resolved, _proxy_probed_at, _proxy_error
    candidates, _mode = proxy_candidates()
    errors = []
    for proxy, label in candidates:
        try:
            response = _request(url, proxy, PROBE_TIMEOUT)
            if response.status_code == 200 and len(response.content) > 1000:
                _proxy_resolved = proxy
                _proxy_probed_at = time.time()
                _proxy_error = ""
                _clear_site_status()
                where = proxy or "直连"
                _log(f"网络通道就绪（{label}: {where}）")
                html = response.text
                if page_locale(html) == "ja":
                    # 探测这一页没切成中文就丢掉，交给 fetch 正常重试
                    _log("探测响应为日文页，交由抓取流程重试")
                    return proxy, None
                return proxy, html
            if response.status_code == 404:
                # 404 也是 DLsite 的应答：通道本身可达，只是这一页拿不到。
                # 不能据此判整个网络死刑——标记通道可用、不返回 HTML，
                # 交给 fetch 逐作品判断。
                _proxy_resolved = proxy
                _proxy_probed_at = time.time()
                _proxy_error = ""
                where = proxy or "直连"
                if _is_suspended_page(response):
                    # 探测的恰好是已下架作品：作品级状态，不计入站点级判定
                    _log(f"{where}可连通，探测目标为已下架作品（HTTP 404 販売停止页）")
                else:
                    _note_http_status(404)
                    _log(f"{where}可连通但站点返回 HTTP 404（可能被限流），交由抓取流程逐作品判断")
                return proxy, None
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
    _clear_site_status()


def fetch(rj: str, delay: float = RETRY_DELAY, retries: int = MAX_RETRIES) -> str | None:
    """抓取详情页 HTML；失败返回 None，原因见 last_error()

    只接受简体中文页：locale=zh-cn cookie 偶尔会失效（拿到日文页），
    这种页面照样是 HTTP 200，若直接入库就会留下日文标题/日文分类。
    因此每个响应都过一遍 page_locale()，判定为日文就当作失败重试，
    重试用尽仍为日文则返回 None，由调用方写入错误、24h 后自动重试。

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
                if page_locale(response.text) == "ja":
                    failure = "DLsite 返回日文页面（简体中文语言设置未生效）"
                else:
                    _clear_site_status()  # 拿到正常页面，站点级故障视为已解除
                    _last_error = ""
                    return response.text
            elif _is_suspended_page(response):
                # 已下架作品（販売停止）：作品级永久状态，不计入站点级判定，
                # 重试也不会变，直接放弃本轮
                failure = "作品已停止販売（DLsite 已下架）"
                break
            else:
                _note_http_status(response.status_code)
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

    # 信息表：2026-11 改版后为 c-productInfo__item（dt=字段名 dd=值）；
    # 旧版 #work_outline 表格兜底（中/日文表头均兼容）
    new_items = soup.select(".c-productInfo__item")
    if new_items:
        for item in new_items:
            key_el = item.select_one(".c-productInfo__key")
            val_el = item.select_one(".c-productInfo__value")
            if not key_el or not val_el:
                continue
            key = key_el.get_text(strip=True)
            val = val_el.get_text(" ", strip=True)
            links = [a.get_text(strip=True) for a in val_el.select("a") if a.get_text(strip=True)]
            if key == "社团名":
                d["circle"] = val
            elif key == "发售日":
                d["release_date"] = val
            elif key == "声优":
                d["cv"] = val
            elif key == "插画":
                d["illustrator"] = val
            elif key == "年龄指定":
                d["age_rating"] = val
            elif key == "作品形式":
                d["work_type"] = val
            elif key == "文件形式":
                d["file_type"] = val
            elif key == "文件容量":
                d["file_size"] = val
            elif key == "分类" and links:
                d["genres"] = links
    else:
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

    # 页面上有多个 JSON-LD（WebSite / BreadcrumbList / Product），全量遍历：
    # 面包屑第 3 项是社团（URL 里带 maker_id）；Product 块带简介/价格/品牌
    for script in soup.select("script[type='application/ld+json']"):
        try:
            ld = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(ld, dict):
            continue
        if ld.get("@type") == "BreadcrumbList":
            for it in ld.get("itemListElement", []):
                if it.get("position") == 3:
                    if not d.get("circle"):
                        d["circle"] = it.get("name")
                    m_maker = re.search(r"maker_id/([A-Za-z0-9]+)", it.get("item") or "")
                    if m_maker:
                        d.setdefault("brand", m_maker.group(1))
        elif ld.get("@type") == "Product":
            if not d.get("description") and ld.get("description"):
                d["description"] = [{"heading": "", "text": str(ld["description"]).strip()}]
            brand = ld.get("brand")
            if not d.get("brand") and isinstance(brand, dict):
                m_maker = re.search(r"maker_id/([A-Za-z0-9]+)", brand.get("url") or "")
                if m_maker:
                    d["brand"] = m_maker.group(1)
                elif brand.get("name") and not d.get("circle"):
                    d["circle"] = brand["name"]
            offers = ld.get("offers")
            if not d.get("price") and isinstance(offers, dict) and offers.get("price"):
                d["price"] = offers.get("price")

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

    # 封面兜底：2026-11 起 DLsite 页面移除了 GA 数据块，主封面改由
    # <meta property="og:image"> 提供（与 image_main 同为 _img_main.jpg）
    if not d.get("cover"):
        og = soup.select_one('meta[property="og:image"]')
        if og and (og.get("content") or "").startswith("http"):
            d["cover"] = og["content"]

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
