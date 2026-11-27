"""密钥泄露自检（只读，绝不输出密钥值）。

用法：
    python scripts/harden_secrets.py            # 检查当前工作区
    python scripts/harden_secrets.py --history  # 同时列出 git 历史里出现过敏感文件的提交

检查项：
1. 被 git 跟踪的 JSON 里，密钥类字段是否非空；
2. 源码里是否还残留长 Cookie / Token 字面量（曾把 1520 字符的雪球 Cookie
   硬编码在 stock_service.py 的 defaults 里）；
3. 环境变量是否已接管密钥（推荐部署方式）。

本脚本不做删除、不改写文件、不打印值。发现问题时给出可执行的处置建议。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENSITIVE_KEY_PARTS = ("api_key", "secret", "password", "token", "webhook", "cookie", "passwd")
SECRET_FILES = (
    "config/models.json",
    "config/user_settings.json",
    "config/auth.json",
    "config/xueqiu_auth.json",
    "config/amazingdata_auth.json",
)
COOKIE_LITERAL = re.compile(r"['\"]xueqiu_cookie['\"]\s*:\s*['\"]([^'\"]{80,})['\"]")


def tracked_files() -> set[str]:
    try:
        out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True)
        return set(out.splitlines())
    except Exception:
        return set()


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(part in low for part in SENSITIVE_KEY_PARTS)


def _nonempty_fields(payload: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_sensitive(str(key)) and isinstance(value, str) and value.strip():
                found.append(path)
            elif isinstance(value, (dict, list)):
                found.extend(_nonempty_fields(value, path))
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            found.extend(_nonempty_fields(value, f"{prefix}[{i}]"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="密钥泄露自检（只读）")
    parser.add_argument("--history", action="store_true", help="同时检查 git 历史")
    args = parser.parse_args()

    tracked = tracked_files()
    problems: list[str] = []

    print("== 1. 被 git 跟踪的配置里是否仍有明文密钥 ==")
    for rel in SECRET_FILES:
        path = ROOT / rel
        if not path.exists():
            print(f"  [跳过] {rel}（本地不存在）")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print(f"  [跳过] {rel}（无法解析）")
            continue
        fields = _nonempty_fields(payload)
        is_tracked = rel in tracked
        state = "已跟踪" if is_tracked else "未跟踪（正确）"
        if fields and is_tracked:
            problems.append(f"{rel} 仍被跟踪且含非空字段：{', '.join(fields)}")
            print(f"  [危险] {rel} {state}；非空敏感字段 {len(fields)} 个：{', '.join(fields)}")
        elif fields:
            print(f"  [注意] {rel} {state}；本地含 {len(fields)} 个非空敏感字段（不入库即可）")
        else:
            print(f"  [安全] {rel} {state}；无明文密钥")

    print("\n== 2. 源码里是否残留长凭证字面量 ==")
    hits = 0
    for path in sorted((ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in COOKIE_LITERAL.finditer(text):
            hits += 1
            rel = path.relative_to(ROOT)
            print(f"  [危险] {rel} 仍有 {len(match.group(1))} 字符的 cookie 字面量")
            problems.append(f"{rel} 含硬编码 cookie 字面量")
    if not hits:
        print("  [安全] 未发现硬编码凭证字面量")

    print("\n== 3. 环境变量接管情况 ==")
    import os
    env_names = (
        "MONITOR_STOCK_LLM_API_KEY", "MONITOR_STOCK_LLM_VISION_API_KEY",
        "MONITOR_STOCK_DINGTALK_WEBHOOK_URL", "MONITOR_STOCK_DINGTALK_SECRET",
        "MONITOR_STOCK_XUEQIU_COOKIE", "MONITOR_STOCK_MODELS_FILE",
    )
    for name in env_names:
        print(f"  {name}: {'已设置' if os.environ.get(name, '').strip() else '未设置'}")

    if args.history:
        print("\n== 4. git 历史里出现过敏感文件的提交 ==")
        try:
            out = subprocess.check_output(
                ["git", "-C", str(ROOT), "log", "--oneline", "--all", "--", *SECRET_FILES],
                text=True,
            )
            lines = [line for line in out.splitlines() if line.strip()]
            print(f"  共 {len(lines)} 条提交涉及这些文件（历史中的明文不会因本次改动消失）")
            for line in lines[:10]:
                print(f"  {line}")
        except Exception as exc:
            print(f"  历史检查失败：{type(exc).__name__}")

    print("\n== 处置建议 ==\n"
          "  1) 已入库的密钥视为已泄露：到服务商后台轮换 LLM API Key、钉钉加签密钥与 webhook、雪球 Cookie。\n"
          "  2) 生产部署改用环境变量或仓库外的 MONITOR_STOCK_MODELS_FILE，不要依赖本地明文文件。\n"
          "  3) 如需彻底清除历史，请自行评估 git filter-repo / BFG（会重写历史，属破坏性操作，本脚本不执行）。\n"
          "  4) 模板文件 config/*.example.json 只含空值，可安全提交。")

    if problems:
        print(f"\n结论：发现 {len(problems)} 处待处理问题。")
        return 1
    print("\n结论：当前工作区未发现被跟踪的明文密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
