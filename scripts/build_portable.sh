#!/usr/bin/env bash
# 构建 Windows 便携免安装绿色包（zip）。
#
# 产物：dist/stock-sentinel-portable-win64-YYYYMMDD.zip
# 用户侧体验：下载 -> 解压 -> 双击 start_portable.bat -> 自动打开浏览器。
#
# 原理：内嵌 python-3.11 embeddable + pip 装好全部依赖 + 净化后的源码。
# 不用 PyInstaller：本项目依赖（akshare/langchain/子进程架构）冻结风险高且易被杀软误报；
# 便携包就是真 Python，行为与源码运行完全一致。
#
# 注意：源码剔除口径与 scripts/publish_github.sh 保持一致（内部文档与个人配置不入包）。
set -euo pipefail

PY_VERSION=3.11.9
APP_NAME=stock-sentinel
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/build/portable"
PKG="$BUILD/$APP_NAME"
OUT="$ROOT/dist/${APP_NAME}-portable-win64-$(date +%Y%m%d).zip"
SYS_TAR=/c/Windows/System32/tar.exe
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

echo "==> 清理并准备构建目录"
rm -rf "$BUILD"
mkdir -p "$PKG"

echo "==> 导出源码（git archive，天然排除未跟踪文件）"
git -C "$ROOT" archive HEAD | tar -x -C "$PKG"

echo "==> 剔除内部/个人内容（与 publish_github.sh 同口径）"
# 注意：scripts/ 与 tests/ 先保留——冒烟验证（verify_startup.py 依赖 tests 的隔离底座）
# 要用，打包前最后一步才移除。
cd "$PKG"
cp docs/publish/README_GITHUB.md README.md
rm -rf docs test_scripts docker
rm -f start.bat requirements.lock
rm -f config/favorites.json config/alert_rules.json config/company_profiles.json \
      config/sentiment_history.json config/decision_rules.json config/strategies.json \
      config/trade_strategy_long.json config/trade_strategy_mid_long.json \
      config/trade_strategy_short.json config/valuation_rules.json

echo "==> 下载内嵌 Python ${PY_VERSION} (embeddable amd64)"
mkdir -p python
if ! curl -L --retry 3 -o python-embed.zip \
    "https://mirrors.huaweicloud.com/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"; then
  curl -L --retry 3 -o python-embed.zip \
    "https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"
fi
"$SYS_TAR" -xf python-embed.zip -C python
rm -f python-embed.zip

echo "==> 启用 embedded python 的 site-packages（pip 前置）"
cat > python/python311._pth <<'EOF'
python311.zip
.
Lib\site-packages

import site
EOF

echo "==> 引导 pip"
curl -L --retry 3 -o get-pip.py https://bootstrap.pypa.io/get-pip.py
python/python.exe get-pip.py --no-warn-script-location -i "${PIP_INDEX}"
rm -f get-pip.py

echo "==> 安装依赖（走清华源，约 300MB 下载，请耐心等）"
python/python.exe -m pip install --no-warn-script-location -i "${PIP_INDEX}" -r requirements.txt

echo "==> 写入启动器 start_portable.bat"
cat > start_portable.bat <<'EOF'
@echo off
chcp 65001 >nul
title Stock Sentinel - 本地监控台
cd /d "%~dp0"

if not exist ".env" (
  python\python.exe -c "import secrets; f=open('.env','w',encoding='utf-8'); f.write('APP_PASSWORD='+secrets.token_urlsafe(12)); f.close()"
  echo [INFO] 已生成 .env（含随机登录口令），可用记事本查看或修改。
)

echo [INFO] 正在启动服务（首次启动预热约几秒到十几秒）...
start "" /b python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8888

set RETRY=0
:waitloop
timeout /t 1 /nobreak >nul
curl -s -o nul -m 2 http://127.0.0.1:8888/api/health
if not errorlevel 1 goto ready
set /a RETRY+=1
if %RETRY% lss 40 goto waitloop
echo [WARN] 服务未在 40 秒内就绪，请查看上方日志排查。
goto keepopen

:ready
start "" http://localhost:8888/
echo [INFO] 服务运行中：http://localhost:8888/   登录口令见 .env 文件
echo [INFO] 关闭本窗口即停止服务。

:keepopen
pause >nul
EOF
# cmd.exe 对 LF 行尾的 goto 标签解析有边缘 case，bat 一律转 CRLF
sed -i 's/$/\r/' start_portable.bat

echo "==> 冒烟验证（启动完整应用，隔离临时库，不触网）"
python/python.exe scripts/verify_startup.py

echo "==> 移除打包前最后的开发文件"
rm -rf scripts tests

echo "==> 打 zip"
mkdir -p "$ROOT/dist"
rm -f "$OUT"
"$SYS_TAR" -a -c -f "$OUT" -C "$BUILD" "$APP_NAME"

echo "==> 完成：$OUT ($(du -sh "$OUT" | cut -f1))"
