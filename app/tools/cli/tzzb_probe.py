"""探测同花顺投资账本 MCP 真实暴露的工具与返回结构。

为什么单独一个脚本
------------------
适配器的字段映射不能靠猜。必须先拿到真实的 ``tools/list`` 和一次
``tools/call`` 的返回样例，才知道它用 ``secID`` 还是 ``sec_code``、
成交方向是 ``buy`` 还是 ``B``、金额是字符串还是数字。
猜错的结果是「导入 0 条记录」或者「买卖全反」。

在容器里跑（容器有 httpx，宿主机没有）：

    docker exec monitor-stock-local python /app/app/tools/cli/tzzb_probe.py
    docker exec monitor-stock-local python /app/app/tools/cli/tzzb_probe.py --call <tool名>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.tzzb_client import tzzb_client  # noqa: E402


def shape(value, depth: int = 0, max_depth: int = 3):
    """把返回结构压成「类型骨架」，避免把真实持仓金额打到屏幕上。"""
    if depth >= max_depth:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1, max_depth) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if not value:
            return []
        return [shape(value[0], depth + 1, max_depth), f"...共{len(value)}项"]
    if isinstance(value, float):
        return 0.0
    if isinstance(value, int):
        return 0
    if isinstance(value, bool):
        return False
    return type(value).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description="探测投资账本 MCP 工具")
    parser.add_argument("--call", default="", help="额外调用一次指定工具，打印返回骨架")
    parser.add_argument("--raw", action="store_true", help="打印原始返回（会暴露真实数据，谨慎）")
    args = parser.parse_args()

    status = tzzb_client.status()
    if not status.get("authorized"):
        print("[x] 尚未授权。请先在宿主机运行：")
        print("    python app/tools/cli/tzzb_auth.py")
        return 1
    print(f"[授权状态] {json.dumps(status, ensure_ascii=False)}")

    try:
        info = tzzb_client.initialize()
        print(f"[握手成功] protocolVersion={info.get('protocolVersion')} "
              f"server={json.dumps(info.get('serverInfo') or {}, ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[x] MCP 握手失败：{exc}")
        return 2

    try:
        tools = tzzb_client.list_tools()
    except Exception as exc:  # noqa: BLE001
        print(f"[x] tools/list 失败：{exc}")
        return 3

    print(f"\n[工具清单] 共 {len(tools)} 个\n")
    for tool in tools:
        name = tool.get("name")
        desc = (tool.get("description") or "").replace("\n", " ")[:110]
        print(f"  · {name}")
        if desc:
            print(f"      {desc}")
        schema = (tool.get("inputSchema") or {}).get("properties") or {}
        if schema:
            params = ", ".join(f"{k}:{(v or {}).get('type', '?')}" for k, v in schema.items())
            print(f"      参数: {params}")

    if args.call:
        print(f"\n[调用 {args.call}]\n")
        try:
            result = tzzb_client.call_tool(args.call, {})
        except Exception as exc:  # noqa: BLE001
            print(f"[x] 调用失败：{exc}")
            return 4
        if args.raw:
            print(json.dumps(result, ensure_ascii=False, indent=2)[:6000])
        else:
            print(json.dumps(shape(result), ensure_ascii=False, indent=2)[:4000])
            print("\n（只显示结构骨架。要真实样例加 --raw，注意会打印你的持仓数据）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
