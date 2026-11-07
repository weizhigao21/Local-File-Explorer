"""内置网页服务（纯 Python，禁止导入 Qt）

手机/平板与电脑同处一个 Wi-Fi 时，用浏览器打开 `http://<电脑IP>:8735/?t=<令牌>`
即可听音乐、看写真、看视频，无需装任何 App。

对外接口：
    get_server()        进程内单例（桌面窗口与 main.py 共用）
    WebServer.start()   启动，返回实际端口
    WebServer.url()     生成带令牌的访问地址
    lan_addresses()     本机局域网 IP 列表（用于在窗口里显示）
"""

from web_server.app import (
    WebServer,
    WebServerError,
    build_url,
    ensure_token,
    get_server,
    lan_addresses,
)

__all__ = [
    "WebServer",
    "WebServerError",
    "build_url",
    "ensure_token",
    "get_server",
    "lan_addresses",
]
