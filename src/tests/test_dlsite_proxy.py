"""DLsite 代理选择与失败回退测试（不依赖 curl_cffi / bs4）"""
from audio_manager import dlsite
from resource_manager import config


class FakeResponse:
    def __init__(self, status=200, size=2000):
        self.status_code = status
        self.content = b"x" * size
        self.text = "<html>" + ("x" * size) + "</html>"


class FakeRequests:
    """按代理参数决定成功/失败的假 requests.get"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        result = self.behavior(kwargs.get("proxy"))
        if isinstance(result, Exception):
            raise result
        return result


def _reset():
    dlsite.reset_network_state()
    dlsite._last_error = ""


def test_normalize_proxy_variants():
    assert dlsite._normalize_proxy("127.0.0.1:7897", True) == "http://127.0.0.1:7897"
    assert dlsite._normalize_proxy("http://127.0.0.1:7897", True) == "http://127.0.0.1:7897"
    assert dlsite._normalize_proxy(
        "http=127.0.0.1:7890;https=127.0.0.1:7897", True
    ) == "http://127.0.0.1:7897"
    assert dlsite._normalize_proxy("127.0.0.1:7897", False) == ""
    assert dlsite._normalize_proxy("", True) == ""


def test_candidates_explicit_proxy_wins(monkeypatch):
    monkeypatch.setattr(config, "DL_PROXY", "socks5://127.0.0.1:1080")
    candidates, mode = dlsite.proxy_candidates()
    assert mode == "manual"
    assert candidates == [("socks5://127.0.0.1:1080", "指定代理")]


def test_candidates_auto_order(monkeypatch):
    """系统代理 → 直连 → 环境变量：环境变量代理不作为首选（常见为失效转发）"""
    monkeypatch.setattr(config, "DL_PROXY", "")
    monkeypatch.setattr(dlsite, "system_proxy", lambda: "http://127.0.0.1:7897")
    monkeypatch.setattr(dlsite, "env_proxy", lambda: "http://127.0.0.1:60241")
    candidates, mode = dlsite.proxy_candidates()
    assert mode == "auto"
    assert [proxy for proxy, _ in candidates] == [
        "http://127.0.0.1:7897", "", "http://127.0.0.1:60241",
    ]


def test_candidates_dedupe_env(monkeypatch):
    monkeypatch.setattr(config, "DL_PROXY", "")
    monkeypatch.setattr(dlsite, "system_proxy", lambda: "http://127.0.0.1:7897")
    monkeypatch.setattr(dlsite, "env_proxy", lambda: "http://127.0.0.1:7897")
    candidates, _ = dlsite.proxy_candidates()
    assert [proxy for proxy, _ in candidates] == ["http://127.0.0.1:7897", ""]


def test_probe_skips_broken_channel_and_caches(monkeypatch):
    _reset()
    fake = FakeRequests(
        lambda proxy: FakeResponse() if proxy == "http://good" else RuntimeError("boom")
    )
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(
        dlsite, "proxy_candidates",
        lambda: ([("http://bad", "系统代理"), ("http://good", "环境变量代理")], "auto"),
    )

    proxy, html = dlsite._resolve_proxy("https://example.test/work")
    assert proxy == "http://good"
    assert html and html.startswith("<html>")
    assert [call["proxy"] for call in fake.calls] == ["http://bad", "http://good"]

    # 第二次直接复用缓存，不再探测
    calls_before = len(fake.calls)
    proxy2, html2 = dlsite._resolve_proxy("https://example.test/work")
    assert (proxy2, html2) == ("http://good", None)
    assert len(fake.calls) == calls_before


def test_probe_all_failed_sets_readable_error(monkeypatch):
    _reset()
    fake = FakeRequests(
        lambda proxy: RuntimeError("Failed to perform, curl: (56) CONNECT tunnel failed, response 502")
    )
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(dlsite, "proxy_candidates", lambda: ([("http://bad", "系统代理"), ("", "直连")], "auto"))

    proxy, html = dlsite._resolve_proxy("https://example.test/work")
    assert proxy == ""
    assert html is None
    assert "代理隧道失败" in dlsite.last_error()


def test_fetch_reuses_resolved_proxy(monkeypatch):
    _reset()
    fake = FakeRequests(lambda proxy: FakeResponse())
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(dlsite, "_resolve_proxy", lambda url: ("http://good", None))

    html = dlsite.fetch("RJ01691063", retries=1)
    assert html and html.startswith("<html>")
    assert fake.calls[0]["proxy"] == "http://good"
    assert fake.calls[0]["cookies"] == dlsite.COOKIES


def test_fetch_returns_none_when_all_channels_failed(monkeypatch):
    """探测已失败且在冷却期内：直接返回 None，不再白等 30 秒超时"""
    _reset()
    monkeypatch.setattr(dlsite, "_proxy_resolved", "")
    monkeypatch.setattr(dlsite, "_proxy_error", "系统代理: 连接超时；直连: 连接超时")
    monkeypatch.setattr(dlsite, "_proxy_probed_at", dlsite.time.time())

    called = []
    monkeypatch.setattr(dlsite, "requests", FakeRequests(lambda proxy: called.append(proxy)), raising=False)
    assert dlsite.fetch("RJ01691063", retries=1) is None
    assert called == []
    assert "连接超时" in dlsite.last_error()


# ── 站点级故障判定（限流一律 404 ≠ 作品不存在）──


def test_probe_treats_404_as_reachable_channel(monkeypatch):
    """探测收到 404 说明通道可达（DLsite 有应答），不能判整个网络死刑"""
    _reset()
    fake = FakeRequests(lambda proxy: FakeResponse(status=404))
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(dlsite, "proxy_candidates", lambda: ([("", "直连")], "auto"))

    proxy, html = dlsite._resolve_proxy("https://example.test/work")
    assert proxy == ""                # 直连被标记为可用，抓取流程继续走
    assert html is None
    assert dlsite._proxy_error == ""  # 不留"网络探测失败"，60s 冷却不生效


def test_single_404_is_work_level_and_batch_is_site_level(monkeypatch):
    """单次 404 = 作品级；判定窗口内连续 3 次 = 站点级"""
    _reset()
    fake = FakeRequests(lambda proxy: FakeResponse(status=404))
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(dlsite, "_resolve_proxy", lambda url: ("", None))

    assert not dlsite.is_site_level_failure()
    assert dlsite.fetch("RJ00000001", retries=1) is None
    assert not dlsite.is_site_level_failure()   # 单次 404 不升级
    assert dlsite.fetch("RJ00000002", retries=1) is None
    assert dlsite.fetch("RJ00000003", retries=1) is None
    assert dlsite.is_site_level_failure()       # 连续 3 次判定为站点级


def test_site_level_cleared_on_success(monkeypatch):
    """站点级判定是粘性的，直到下一个 200 成功才解除"""
    _reset()
    holder = {"status": 404}
    fake = FakeRequests(lambda proxy: FakeResponse(status=holder["status"]))
    monkeypatch.setattr(dlsite, "requests", fake, raising=False)
    monkeypatch.setattr(dlsite, "_resolve_proxy", lambda url: ("", None))

    for rj in ("RJ00000001", "RJ00000002", "RJ00000003"):
        assert dlsite.fetch(rj, retries=1) is None
    assert dlsite.is_site_level_failure()

    holder["status"] = 200
    assert dlsite.fetch("RJ00000004", retries=1)
    assert not dlsite.is_site_level_failure()
