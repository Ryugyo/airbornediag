"""测试用的假 MindIE 服务。

只存在于测试进程内，用于在不部署模型的情况下验证真实的 HTTP 调用路径：
请求体、状态码、响应解析与各类失败。

工程代码里没有任何模拟或降级分支：调用失败一律报错，不会返回替代回答。
本模块只提供被调用的服务端，不参与回答的生成。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_ANSWER = "这是假服务返回的回答。"


class _Behaviour:
    """假服务对每个请求的应答方式。"""

    def __init__(self, body: bytes, status: int, delay: float, content_type: str) -> None:
        self.body = body
        self.status = status
        self.delay = delay
        self.content_type = content_type


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: List[Dict[str, Any]] = []
        self.behaviour = _Behaviour(b"{}", 200, 0.0, "application/json")

    def handle_error(self, request: Any, client_address: Any) -> None:
        """客户端提前断开（例如超时）时不打印堆栈，避免污染测试输出。"""
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        self.server.requests.append(
            {
                "path": self.path,
                # 键统一转小写，便于断言。
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": raw,
                "json": parsed,
            }
        )

        behaviour = self.server.behaviour
        if behaviour.delay:
            time.sleep(behaviour.delay)

        try:
            self.send_response(behaviour.status)
            self.send_header("Content-Type", behaviour.content_type)
            self.send_header("Content-Length", str(len(behaviour.body)))
            self.end_headers()
            self.wfile.write(behaviour.body)
        except OSError:
            # 客户端已断开，例如超时测试。
            pass

    def log_message(self, format: str, *args: Any) -> None:
        """安静运行，不输出访问日志。"""
        return None


class FakeService:
    """已启动的假服务。"""

    def __init__(self, base_url: str, server: _Server) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return self._server.requests

    def last_request(self) -> Dict[str, Any]:
        if not self.requests:
            raise AssertionError("假服务没有收到任何请求")
        return self.requests[-1]


@contextmanager
def fake_mindie(
    *,
    payload: Any = None,
    raw_body: Optional[str] = None,
    status: int = 200,
    delay: float = 0.0,
    content_type: str = "application/json",
) -> Iterator[FakeService]:
    """启动一个假服务，退出上下文时关闭。

    payload 会被序列化为 JSON 响应体；raw_body 用于构造非 JSON 的响应体。
    delay 用于制造超时，status 用于制造 HTTP 错误。
    """
    if raw_body is not None:
        body = raw_body.encode("utf-8")
    else:
        if payload is None:
            payload = {"text": [DEFAULT_ANSWER]}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    server = _Server(("127.0.0.1", 0), _Handler)
    server.behaviour = _Behaviour(body, status, delay, content_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    try:
        yield FakeService("http://{}:{}".format(address[0], address[1]), server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def unused_port() -> int:
    """返回一个当前无人监听的端口，用于制造连接失败。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
