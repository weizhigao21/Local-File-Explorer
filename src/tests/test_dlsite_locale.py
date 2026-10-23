"""守卫：DLsite 抓取只接受简体中文页面。

背景（v1.8.13）：`locale=zh-cn` cookie 失效时 DLsite 照样返回 HTTP 200，
只是整页日文。早年 `_probe()` 发探测请求时传了 `cookies=False`，而它的
响应会被 `fetch()` 当结果直接返回 —— 于是每次网络重新探测后的第一条作品
都会存成日文标题/日文分类（现场例：RJ01116988 的 genres 为 癒し/ささやき）。

三道防线，缺一不可：
1. `_probe()` 必须带 cookie         → 探测响应本身就是中文页
2. `page_locale()` 判定页面语言     → 判据单一来源
3. `fetch()` 拒绝日文页并重试       → 兜住 cookie 抖动的残余情况
"""
import inspect

import pytest

from audio_manager import dlsite, dlsite_db

_PAD = "<!--" + ("填充内容" * 600) + "-->"

# 中文页：<html lang> + 中文表头
ZH_PAGE = (
    '<html lang="zh-CN"><body><h1 id="work_name">中文标题</h1>'
    '<table id="work_outline">'
    "<tr><th>发售日</th><td>2024年05月31日</td></tr>"
    "<tr><th>声优</th><td>柚木つばめ</td></tr>"
    "<tr><th>分类</th><td><a>ASMR</a><a>舔耳</a></td></tr>"
    f"</table>{_PAD}</body></html>"
)

# 日文页：<html lang> + 日文表头
JA_PAGE = (
    '<html lang="ja-JP"><body><h1 id="work_name">日本語タイトル</h1>'
    '<table id="work_outline">'
    "<tr><th>販売日</th><td>2024年05月31日</td></tr>"
    "<tr><th>声優</th><td>柚木つばめ</td></tr>"
    "<tr><th>ジャンル</th><td><a>ASMR</a><a>耳舐め</a></td></tr>"
    f"</table>{_PAD}</body></html>"
)

# 没有 lang 属性、只有中文表头（兜底路径）
ZH_PAGE_NO_LANG = ZH_PAGE.replace('<html lang="zh-CN">', "<html>")
JA_PAGE_NO_LANG = JA_PAGE.replace('<html lang="ja-JP">', "<html>")


class FakeResponse:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")


@pytest.fixture
def patched(monkeypatch):
    """把 _request / proxy_candidates 换成假的，返回记录下来的调用参数列表"""
    calls = []

    def install(response):
        def fake_request(url, proxy, timeout, **kwargs):
            calls.append({"url": url, "proxy": proxy, **kwargs})
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(dlsite, "_request", fake_request)
        monkeypatch.setattr(dlsite, "proxy_candidates", lambda: ([("", "直连")], "auto"))
        dlsite.reset_network_state()
        dlsite._last_error = ""
        return calls

    return install


# ── page_locale 判定 ──
def test_page_locale_by_html_lang():
    assert dlsite.page_locale(ZH_PAGE) == "zh"
    assert dlsite.page_locale(JA_PAGE) == "ja"


def test_page_locale_falls_back_to_table_headers():
    """没有 lang 属性时靠信息表表头区分（中/日两套互斥）"""
    assert dlsite.page_locale(ZH_PAGE_NO_LANG) == "zh"
    assert dlsite.page_locale(JA_PAGE_NO_LANG) == "ja"


def test_page_locale_unknown_is_empty():
    """无法判断时返回空串——调用方放行，不能把正常页误杀"""
    assert dlsite.page_locale("") == ""
    assert dlsite.page_locale("<html><body>hello</body></html>") == ""


# ── 探测必须带语言 cookie（本次 bug 的根因）──
def test_probe_request_carries_locale_cookie(patched):
    calls = patched(FakeResponse(ZH_PAGE))
    dlsite._probe("https://www.dlsite.com/maniax/work/=/product_id/RJ00000001.html")
    assert calls, "探测必须真的发请求"
    for call in calls:
        assert call.get("cookies", True), (
            "探测请求不能传 cookies=False：它的响应会被 fetch() 当作抓取结果，"
            "缺 locale=zh-cn 就整条存成日文"
        )


def test_probe_source_never_disables_cookies():
    """静态兜底：_probe 源码里不得出现 cookies=False"""
    assert "cookies=False" not in inspect.getsource(dlsite._probe)


def test_probe_discards_japanese_page(patched):
    """探测到日文页时不能把它交给 fetch 当结果，但代理仍算可用"""
    patched(FakeResponse(JA_PAGE))
    proxy, html = dlsite._probe("https://www.dlsite.com/maniax/work/=/product_id/RJ00000001.html")
    assert proxy == ""  # 直连候选依然被记为可用
    assert html is None


# ── fetch 只放行中文页 ──
def test_fetch_accepts_chinese_page(patched):
    patched(FakeResponse(ZH_PAGE))
    html = dlsite.fetch("RJ00000002", delay=0)
    assert html == ZH_PAGE
    assert dlsite.last_error() == ""


def test_fetch_rejects_japanese_page(patched):
    """日文页即使 HTTP 200 也必须判为失败，且重试用尽后返回 None"""
    calls = patched(FakeResponse(JA_PAGE))
    assert dlsite.fetch("RJ00000003", delay=0, retries=2) is None
    assert "日文" in dlsite.last_error()
    assert len(calls) >= 2, "日文页应该触发重试"


def test_fetch_reports_http_error_not_locale(patched):
    """非 200 仍报 HTTP 错误，别串成语言问题"""
    patched(FakeResponse(ZH_PAGE, status=404))
    assert dlsite.fetch("RJ00000004", delay=0, retries=1) is None
    assert "HTTP 404" in dlsite.last_error()


# ── delete_work：删掉单条错误记录，下次扫描自动重抓 ──
def test_delete_work_removes_record(tmp_path, monkeypatch):
    monkeypatch.setattr(dlsite_db.config, "DLSITE_DB_PATH", str(tmp_path / "dlsite.db"))
    dlsite_db.init_db()
    dlsite_db.upsert_work({"rj_code": "RJ00000005", "title": "旧记录"})
    assert dlsite_db.has_work("RJ00000005")
    assert "RJ00000005" in dlsite_db.get_cached_rjs()

    assert dlsite_db.delete_work("RJ00000005") is True
    assert not dlsite_db.has_work("RJ00000005")
    assert dlsite_db.get_work("RJ00000005") is None
    assert "RJ00000005" not in dlsite_db.get_cached_rjs()

    assert dlsite_db.delete_work("RJ00000005") is False  # 幂等，重复删不报错
