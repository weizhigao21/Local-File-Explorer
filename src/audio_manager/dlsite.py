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
import time

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


def fetch(rj: str, delay: float = 2.0, retries: int = 5) -> str | None:
    """带重试地抓取详情页，返回 HTML；失败返回 None"""
    url = BASE_URL.format(rj)
    for attempt in range(retries):
        try:
            r = requests.get(url, impersonate="chrome", headers=HEADERS,
                             cookies=COOKIES, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(delay)
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
    try:
        r = requests.get(cover_url, impersonate="chrome",
                         headers=HEADERS, timeout=30)
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
