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
      config/trade_strategy_short.json config/valuation_rules.json \
      config/market_regime_cache.json

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
# 健康探测用包内 python（urllib），不用 curl：老版本 Win10 没有系统 curl，
# 会导致服务已就绪但浏览器不自动打开。零外部依赖。
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
python\python.exe -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8888/api/health',timeout=2)" >nul 2>&1
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

echo "==> 写入使用说明.txt"
# UTF-8 带 BOM：老版本 Win10 的记事本才能正确识别中文（新记事本默认 UTF-8 不受影响）
printf '\xef\xbb\xbf' > 使用说明.txt
cat >> 使用说明.txt <<'EOF'
Stock Sentinel · A股个人盯盘监控台（免安装绿色版）
====================================================
一句话：解压 -> 双击 start_portable.bat -> 浏览器自动打开，开始使用。
不需要安装 Python 或任何依赖，所有东西都在这个文件夹里。

三步上手
--------
1. 把整个文件夹解压到任意位置（建议纯英文路径，如 D:\stock-sentinel）。
2. 双击 start_portable.bat。首次运行会自动生成 .env 文件（内含随机登录口令）。
3. 服务就绪后自动打开浏览器 http://localhost:8888/ ，用 .env 里的口令登录。

登录口令
--------
· 打开文件夹里的 .env 文件（记事本即可），APP_PASSWORD= 后面就是口令。
· 想改成自己好记的：改完保存，关掉黑窗口重新双击 start_portable.bat 即生效。

日常使用
--------
· 黑窗口是服务本体：开着就在监控，关掉即停止（数据不会丢）。
· 开机自启（可选）：把 start_portable.bat 的快捷方式放进
  「开始菜单 -> 程序 -> 启动」文件夹。

数据与备份
----------
· 全部数据（持仓、流水、设置、SQLite 数据库）都在本文件夹的 config\ 里。
· 备份 = 复制整个文件夹。换电脑 = 整个文件夹拷过去。
· 升级版本 = 下载新 zip 解压到新文件夹，把旧文件夹的 config\ 整个拷过来。

常见问题
--------
· 提示端口被占：用记事本打开 start_portable.bat，把其中两处 8888 改成
  其他数字（如 8899），保存重开。
· 浏览器没自动打开：手动访问 http://localhost:8888/ ；仍不行看黑窗口里的报错。
· AI 分析不可用：需要在网页「设置」里填自己的大模型 API Key
  （OpenAI 兼容接口即可，DeepSeek/通义千问等都支持）。不填不影响行情监控与记账。
· 杀毒软件提示：本包内嵌真实 Python 运行时，如被拦截请添加信任目录。

免责声明
--------
本工具仅供学习与研究，辅助管理自己的投资纪律，不构成任何投资建议。
行情数据来自公开免费接口，可能有延迟或缺失，请以券商软件为准。
EOF
sed -i 's/$/\r/' 使用说明.txt

echo "==> 冒烟验证（启动完整应用，隔离临时库，不触网）"
python/python.exe scripts/verify_startup.py

echo "==> 移除打包前最后的开发文件"
# logs/ 与 market_regime_cache.json 都是冒烟运行时应用自己写下的构建机产物，
# 必须在冒烟之后删（前置删会被冒烟重新写出来）
rm -rf scripts tests logs
rm -f config/market_regime_cache.json

echo "==> 打 zip"
mkdir -p "$ROOT/dist"
rm -f "$OUT"
"$SYS_TAR" -a -c -f "$OUT" -C "$BUILD" "$APP_NAME"

echo "==> 生成 SHA256 校验文件"
( cd "$ROOT/dist" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )

echo "==> 完成：$OUT ($(du -sh "$OUT" | cut -f1))"
cat "$OUT.sha256"
