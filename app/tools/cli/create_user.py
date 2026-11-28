"""命令行管理账号（管理员在服务器上建号 / 重置口令）。

容器内用法::

    # 查看已有用户
    docker exec -it monitor-stock python -m app.tools.cli.create_user --list

    # 新建用户（交互输入口令，不回显）
    docker exec -it monitor-stock python -m app.tools.cli.create_user alice

    # 非交互（脚本/CI 用；注意别把口令留在 shell history 里）
    docker exec monitor-stock python -m app.tools.cli.create_user alice --password 'xxx'

    # 重置某人口令（会让该用户所有会话下线）
    docker exec -it monitor-stock python -m app.tools.cli.create_user alice --reset-password
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# 允许直接 `python app/tools/cli/create_user.py` 运行（容器内推荐 -m 方式）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core import auth  # noqa: E402
from app.db import store  # noqa: E402


def _read_password(confirm: bool = True) -> str:
    pwd = getpass.getpass("口令（至少 8 位）: ")
    if confirm:
        again = getpass.getpass("再输一次: ")
        if pwd != again:
            print("两次输入不一致", file=sys.stderr)
            raise SystemExit(2)
    return pwd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建 / 查看 / 重置 monitor_stock 账号")
    parser.add_argument("username", nargs="?", help="用户名；留空配合 --list 使用")
    parser.add_argument("--password", help="口令（不传则交互输入）")
    parser.add_argument("--display-name", default="", help="显示名（可选）")
    parser.add_argument("--reset-password", action="store_true", help="重置已有用户口令")
    parser.add_argument("--list", action="store_true", help="列出所有用户")
    args = parser.parse_args(argv)

    auth.ensure_initialized()

    if args.list or not args.username:
        users = store.list_users()
        if not users:
            print("还没有任何用户")
            return 0
        print(f"{'ID':<4} {'用户名':<20} {'状态':<6} 创建时间")
        for user in users:
            state = "启用" if int(user.get("is_active") or 0) else "停用"
            print(f"{user['id']:<4} {user['username']:<20} {state:<6} {user.get('created_at') or ''}")
        return 0

    username = args.username.strip()

    if args.reset_password:
        user = store.get_user_by_username(username)
        if not user:
            print(f"用户不存在: {username}", file=sys.stderr)
            return 1
        password = args.password or _read_password()
        err = auth.validate_password(password)
        if err:
            print(err, file=sys.stderr)
            return 2
        salt, pwd_hash = auth.hash_password(password)
        store.update_user_password(int(user["id"]), salt, pwd_hash)
        auth.revoke_user_sessions(int(user["id"]))
        print(f"已重置 {username} 的口令，该用户其它会话已下线")
        return 0

    if store.get_user_by_username(username):
        print(f"用户已存在: {username}（如需改口令请加 --reset-password）", file=sys.stderr)
        return 1

    password = args.password or _read_password()
    try:
        user = auth.create_user(username, password, args.display_name or "")
    except ValueError as exc:
        print(f"创建失败: {exc}", file=sys.stderr)
        return 2
    print(f"已创建用户 {user.get('username')}（id={user.get('id')}）")
    print("提示：新用户的自选、持仓、流水、设置全部独立，初始为空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
