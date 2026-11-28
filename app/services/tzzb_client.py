"""同花顺投资账本 MCP 客户端：OAuth 2.1 + PKCE + Streamable HTTP JSON-RPC。

为什么要项目自己实现，而不是靠 AI 客户端中转
--------------------------------------------
中转方案（人在对话里点一下）没法进调度，也就没法「每天自动把成交拉进来」。
但代价是这条链路要自己维护：token 生命周期、协议握手、会话头，任何一环
对方改了都会静默失效。所以这里把每个失败点都显式打日志，不静默兜底。

协议要点（都已对本服务实测过，不是照抄规范）
--------------------------------------------
- 授权：``GET /oauth2/authorize`` 会 302 到 ``/pc/mcpAuth.html``（前端授权页），
  用户登录同花顺并同意后回跳 ``redirect_uri?code=&state=``。
- 客户端类型：动态注册（``POST /oauth2/register``）拿到的是**公共客户端**
  （``token_endpoint_auth_method=none``），没有 client_secret，安全性完全依赖 PKCE。
  所以 ``code_verifier`` 必须每次重新生成，不能复用。
- ⚠️ 注册时**不能**带 ``scope``：带 ``tools.read`` 会直接
  ``400 invalid_scope``。scope 是服务端按客户端预置的，不是客户端声明的。
- MCP：``POST /mcp``，必须同时接受 ``application/json`` 与 ``text/event-stream``，
  服务端可能用 SSE 逐帧返回。``initialize`` 之后要补一条 ``notifications/initialized``，
  否则后续 ``tools/list`` 可能被拒。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

ISSUER = "https://tzzb.10jqka.com.cn"
MCP_URL = f"{ISSUER}/mcp"
REGISTER_URL = f"{ISSUER}/oauth2/register"
AUTHORIZE_URL = f"{ISSUER}/oauth2/authorize"
TOKEN_URL = f"{ISSUER}/oauth2/token"

CLIENT_NAME = "Local Stock Monitor"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
MCP_PROTOCOL_VERSION = "2025-06-18"

_TOKEN_LOCK = threading.Lock()

# 提前 120 秒续期，避免「取出时还有效、真正请求时已过期」的边界
_REFRESH_MARGIN = 120


def _root() -> Path:
    # app/services/tzzb_client.py → 项目根
    return Path(__file__).resolve().parents[2]


def token_path() -> Path:
    """令牌文件位置。放 config/ 是因为它已经挂进容器，宿主机授权后容器立刻能读。"""
    override = os.environ.get("TZZB_TOKEN_FILE")
    if override:
        return Path(override)
    return _root() / "config" / "tzzb_token.json"


def make_pkce() -> Tuple[str, str]:
    """生成 (code_verifier, code_challenge)。S256，必须每次新建。"""
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class TzzbAuthError(RuntimeError):
    """需要人重新授权（refresh 也救不回来）。"""


class TzzbClient:
    """令牌读写 + OAuth + MCP 调用。实例本身无状态，可全局复用。"""

    def __init__(self, token_file: Optional[Path] = None):
        self._token_file = Path(token_file) if token_file else token_path()
        self._session_id: Optional[str] = None
        self._rpc_id = 0
        self._initialized = False

    # ---------------- 令牌 ----------------
    def load_token(self) -> Optional[Dict[str, Any]]:
        if not self._token_file.exists():
            return None
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("tzzb token 读取失败 path=%s error=%r", self._token_file, exc)
            return None
        return data if isinstance(data, dict) else None

    def save_token(self, token: Dict[str, Any]) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(token)
        payload["saved_at"] = int(time.time())
        tmp = self._token_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._token_file)
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            # Windows 上 chmod 基本无效，忽略即可（真正的隔离靠用户目录权限）
            pass
        logger.info("tzzb token 已保存 path=%s", self._token_file)

    def is_authorized(self) -> bool:
        token = self.load_token()
        return bool(token and token.get("access_token"))

    def status(self) -> Dict[str, Any]:
        """给接口/前端看的授权状态。不回传 token 本身。"""
        token = self.load_token()
        if not token:
            return {"authorized": False, "hint": "尚未授权，请先运行 app/tools/cli/tzzb_auth.py"}
        expires_at = token.get("expires_at")
        now = int(time.time())
        return {
            "authorized": True,
            "has_refresh": bool(token.get("refresh_token")),
            "expires_at": expires_at,
            "expires_in": (int(expires_at) - now) if expires_at else None,
            "expired": bool(expires_at and int(expires_at) <= now),
            "client_id": token.get("client_id"),
            "scope": token.get("scope"),
        }

    # ---------------- OAuth ----------------
    def register(self, redirect_uri: str = DEFAULT_REDIRECT) -> Dict[str, Any]:
        """动态注册公共客户端。注意不要带 scope（会被 400 拒绝）。"""
        import httpx

        body = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        resp = httpx.post(REGISTER_URL, json=body, timeout=20)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"客户端注册失败 {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def build_authorize_url(self, client_id: str, code_challenge: str, state: str,
                            redirect_uri: str = DEFAULT_REDIRECT) -> str:
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": MCP_URL,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    def exchange_code(self, code: str, code_verifier: str, client_id: str,
                      redirect_uri: str = DEFAULT_REDIRECT) -> Dict[str, Any]:
        import httpx

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        resp = httpx.post(TOKEN_URL, data=data, timeout=30,
                          headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(f"换取令牌失败 {resp.status_code}: {resp.text[:300]}")
        token = resp.json()
        token.setdefault("client_id", client_id)
        self._decorate(token)
        self.save_token(token)
        self._reset_session()
        return token

    def refresh(self) -> Dict[str, Any]:
        """用 refresh_token 续期。失败抛 TzzbAuthError，提示人去重新授权。"""
        import httpx

        token = self.load_token() or {}
        refresh_token = token.get("refresh_token")
        client_id = token.get("client_id")
        if not refresh_token or not client_id:
            raise TzzbAuthError("没有 refresh_token，需要重新授权")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        resp = httpx.post(TOKEN_URL, data=data, timeout=30,
                          headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise TzzbAuthError(f"刷新令牌失败 {resp.status_code}: {resp.text[:200]}")
        fresh = resp.json()
        # RFC 6749 §6：服务端可以不返回新的 refresh_token，此时沿用旧的
        fresh.setdefault("refresh_token", refresh_token)
        fresh.setdefault("client_id", client_id)
        self._decorate(fresh)
        self.save_token(fresh)
        self._reset_session()
        logger.info("tzzb token 续期成功")
        return fresh

    @staticmethod
    def _decorate(token: Dict[str, Any]) -> None:
        """把 expires_in 换算成绝对时间，避免每次都要读文件时间。"""
        expires_in = token.get("expires_in")
        try:
            seconds = int(expires_in) if expires_in is not None else 3600
        except (TypeError, ValueError):
            seconds = 3600
        token["expires_at"] = int(time.time()) + seconds

    def _access_token(self, force_refresh: bool = False) -> str:
        """取一个可用令牌：快过期就自动续，续不动就要求重新授权。"""
        with _TOKEN_LOCK:
            token = self.load_token()
            if not token or not token.get("access_token"):
                raise TzzbAuthError("尚未授权投资账本 MCP，请先完成 OAuth 授权")
            expires_at = token.get("expires_at")
            stale = force_refresh or not expires_at or int(expires_at) - _REFRESH_MARGIN <= int(time.time())
            if stale:
                if token.get("refresh_token"):
                    token = self.refresh()
                elif force_refresh:
                    raise TzzbAuthError("访问令牌已失效且无 refresh_token，需要重新授权")
            return str(token["access_token"])

    # ---------------- MCP JSON-RPC ----------------
    def _reset_session(self) -> None:
        self._session_id = None
        self._initialized = False

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _parse_body(self, resp: Any) -> Dict[str, Any]:
        """兼容两种响应：普通 JSON，或 SSE（event: message / data: {...}）。"""
        ctype = (resp.headers.get("content-type") or "").lower()
        text = resp.text or ""
        if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
            chunks: List[str] = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunks.append(line[5:].strip())
            payload = "\n".join(c for c in chunks if c)
            if not payload:
                raise RuntimeError(f"MCP 返回空 SSE：{text[:200]}")
            return json.loads(payload)
        return json.loads(text)

    def _post_rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
                  retry_on_401: bool = True) -> Dict[str, Any]:
        import httpx

        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            body["params"] = params

        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = httpx.post(MCP_URL, json=body, headers=headers, timeout=60)

        if resp.status_code == 401 and retry_on_401:
            # 令牌可能在两次请求之间被服务端吊销/过期，强制续一次再重试
            logger.info("tzzb MCP 401，尝试刷新令牌后重试 method=%s", method)
            self._access_token(force_refresh=True)
            return self._post_rpc(method, params, retry_on_401=False)

        if resp.status_code >= 400:
            raise RuntimeError(f"MCP {method} 失败 {resp.status_code}: {resp.text[:300]}")

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        data = self._parse_body(resp)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise RuntimeError(f"MCP {method} 返回错误：{err.get('message') or err}")
        return data

    def _notify(self, method: str) -> None:
        """JSON-RPC 通知（无 id、无响应体）。协议要求 initialize 后补一条。"""
        import httpx

        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            httpx.post(MCP_URL, json={"jsonrpc": "2.0", "method": method},
                       headers=headers, timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tzzb 通知 %s 发送失败（无碍）：%r", method, exc)

    def initialize(self) -> Dict[str, Any]:
        if self._initialized:
            return {"ok": True, "cached": True}
        data = self._post_rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0.0"},
        })
        self._notify("notifications/initialized")
        self._initialized = True
        return data.get("result") or data

    def list_tools(self) -> List[Dict[str, Any]]:
        self.initialize()
        data = self._post_rpc("tools/list", {})
        result = data.get("result") or {}
        tools = result.get("tools") or []
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        self.initialize()
        data = self._post_rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result = data.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"工具 {name} 执行失败：{result}")
        # MCP 标准是 content: [{type:'text', text:'...'}]，文本里通常是 JSON 字符串
        content = result.get("content")
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return joined
        if isinstance(result.get("structuredContent"), (dict, list)):
            return result["structuredContent"]
        return result


tzzb_client = TzzbClient()
