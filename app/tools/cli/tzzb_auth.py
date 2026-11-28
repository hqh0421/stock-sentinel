"""同花顺投资账本 MCP 一次性授权（OAuth 2.1 + PKCE，本地回环回调）。

为什么这个脚本必须用标准库、而且在宿主机跑
--------------------------------------------
授权要「打开系统浏览器 + 监听 127.0.0.1 回环端口」。浏览器在宿主机，
而项目跑在容器里；宿主机的 Python 没有 httpx。
所以这里只用 urllib + http.server 自包含实现，不 import 任何第三方库，
只从 app.services.tzzb_client 借常量与 PKCE 工具（那个模块的 httpx 是
方法内 import，不会在导入期触发）。

用法
----
    python app/tools/cli/tzzb_auth.py
    python app/tools/cli/tzzb_auth.py --port 8765
    python app/tools/cli/tzzb_auth.py --status      # 只看当前授权状态

授权成功后令牌写到 config/tzzb_token.json。该目录已挂载进容器，
所以宿主机授权完，容器里的服务立刻就能用，不需要重启。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.tzzb_client import (  # noqa: E402
    AUTHORIZE_URL,
    MCP_URL,
    MCP_PROTOCOL_VERSION,
    REGISTER_URL,
    TOKEN_URL,
    CLIENT_NAME,
    make_pkce,
    tzzb_client,
    token_path,
)

TIMEOUT_SECONDS = 300


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    """标准库 POST。返回解析后的 JSON（失败抛 RuntimeError，带上原始响应）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:400]}") from exc


def _post_form(url: str, form: dict, timeout: int = 30) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:400]}") from exc


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class _CallbackHandler(BaseHTTPRequestHandler):
    """只处理 /callback，把 code/state 塞进队列后回一个可关闭的页面。"""

    result_queue: "queue.Queue[dict]" = queue.Queue()
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        payload = {
            "code": (params.get("code") or [""])[0],
            "state": (params.get("state") or [""])[0],
            "error": (params.get("error") or [""])[0],
            "error_description": (params.get("error_description") or [""])[0],
        }

        ok = bool(payload["code"]) and not payload["error"] and payload["state"] == self.expected_state
        if payload["error"]:
            title = "授权失败"
            detail = payload["error_description"] or payload["error"]
        elif payload["state"] != self.expected_state:
            title = "授权失败"
            detail = "state 校验不通过（可能是跨站请求伪造），请重新运行脚本"
        elif not payload["code"]:
            title = "授权失败"
            detail = "没有拿到授权码"
        else:
            title = "授权成功"
            detail = "可以关闭这个页面，回到命令行查看结果。"

        html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>{title}</title>
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;padding:48px;color:#2c2c2a">
<h2 style="font-weight:500">{title}</h2><p style="color:#5f5e5a">{detail}</p></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

        if not payload["error"]:
            self.result_queue.put(payload)
        else:
            self.result_queue.put({"error": payload["error_description"] or payload["error"]})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认会往 stderr 打访问日志，对用户没意义
        return


def cmd_status() -> int:
    st = tzzb_client.status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if st.get("authorized"):
        left = st.get("expires_in")
        if left is not None:
            print(f"\n令牌剩余约 {left // 60} 分钟。" + ("（已过期，下次调用会自动续期）" if left <= 0 else ""))
    else:
        print(f"\n令牌文件：{token_path()}")
    return 0 if st.get("authorized") else 1


def cmd_authorize(port: int, no_browser: bool) -> int:
    if not _port_free(port):
        print(f"[x] 端口 {port} 被占用，回环回调收不到授权码。")
        print(f"    换个端口重试：python {Path(__file__).name} --port {port + 1}")
        return 2

    redirect_uri = f"http://127.0.0.1:{port}/callback"

    print("[1/4] 注册 OAuth 公共客户端…")
    try:
        # ⚠️ 不要带 scope：服务端对 scope 是预置的，声明会直接 400 invalid_scope
        client = _post_json(REGISTER_URL, {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
    except RuntimeError as exc:
        print(f"[x] 注册失败：{exc}")
        return 3
    client_id = client.get("client_id")
    if not client_id:
        print(f"[x] 注册返回里没有 client_id：{client}")
        return 3
    print(f"      client_id = {client_id}")

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"

    _CallbackHandler.result_queue = queue.Queue()
    _CallbackHandler.expected_state = state
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"[2/4] 在浏览器中打开授权页（回调 {redirect_uri}）…")
    if no_browser:
        print("      已跳过自动打开，请手动复制下面的链接：")
        print(f"\n{auth_url}\n")
    else:
        opened = False
        try:
            opened = webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            print("      没能自动打开浏览器，请手动复制下面的链接：")
            print(f"\n{auth_url}\n")
    print(f"      请在 {TIMEOUT_SECONDS} 秒内完成同花顺登录并点击同意授权。")

    try:
        result = _CallbackHandler.result_queue.get(timeout=TIMEOUT_SECONDS)
    except queue.Empty:
        print("[x] 等待超时，没有收到授权回调。")
        return 4
    finally:
        server.shutdown()
        server.server_close()

    if result.get("error"):
        print(f"[x] 授权被拒绝或出错：{result['error']}")
        return 4

    print("[3/4] 用授权码换取访问令牌…")
    try:
        token = _post_form(TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        })
    except RuntimeError as exc:
        print(f"[x] 换取令牌失败：{exc}")
        return 5

    if not token.get("access_token"):
        # 只报字段名，不回显值：异常响应里可能带着半成品的 access_token / refresh_token，
        # 打到终端就等于把它写进了 shell 历史或 CI 日志。
        print(f"[x] 令牌响应异常，返回字段：{sorted(token.keys())}")
        for key in ("error", "error_description"):
            if token.get(key):
                print(f"      {key} = {token.get(key)}")
        return 5

    token.setdefault("client_id", client_id)
    try:
        expires_in = int(token.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    token["expires_at"] = int(time.time()) + expires_in
    tzzb_client.save_token(token)

    print("[4/4] 完成。")
    print(f"      令牌文件：{token_path()}")
    print(f"      有效期约 {expires_in // 60} 分钟，"
          f"{'支持 refresh_token 自动续期' if token.get('refresh_token') else '⚠️ 没有 refresh_token，到期需重新授权'}")
    print(f"      scope = {token.get('scope') or '(服务端未返回)'}")
    print("\n接下来可以验证：python app/tools/cli/tzzb_auth.py --status")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同花顺投资账本 MCP 授权")
    parser.add_argument("--port", type=int, default=8765, help="本地回环回调端口（默认 8765）")
    parser.add_argument("--status", action="store_true", help="只看授权状态，不发起授权")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印链接")
    args = parser.parse_args()

    if args.status:
        return cmd_status()
    return cmd_authorize(args.port, args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
