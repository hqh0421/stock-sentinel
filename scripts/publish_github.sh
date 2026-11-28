#!/usr/bin/env bash
# 开源同步：把本地主线的改动，以「普通提交」增量同步到 GitHub（不再强推孤儿快照）。
#
# 背景与纪律：
#   - gitee(origin) 保存完整开发历史，其中早期提交含真实密钥，**永不推到 GitHub**。
#   - github 是从净化快照演化来的正常仓库：每次发版只推「净化后的增量提交」，
#     历史保留，外部 PR 可以正常 merge，贡献者署名进历史。
#   - 本地主线是唯一事实源；GitHub 是净化镜像。两边历史不同源，**禁止整树互相覆盖**。
#
# 三条铁律：
#   1. 只推净化树（本脚本的净化步骤不可省略）。
#   2. 推送前确认 GitHub 没有未回流的提交（.workbuddy/github-synced-head 标记）。
#   3. 普通 push，绝不用 --force（否则会冲掉 PR 合并历史）。
#
# 用法：
#   HTTPS_PROXY=http://127.0.0.1:7892 https_proxy=http://127.0.0.1:7892 \
#     bash scripts/publish_github.sh [commit-ish]      # 默认 HEAD
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

SRC_COMMIT=$(git rev-parse "${1:-HEAD}")
SRC_SHORT=$(git rev-parse --short "${SRC_COMMIT}")
REMOTE_URL=$(git remote get-url github)
MARKER="$REPO_ROOT/.workbuddy/github-synced-head"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# ---- 1. 检查 GitHub 是否有未回流的提交 ----
echo "==> 检查 GitHub 是否有未回流的提交"
mkdir -p "$(dirname "$MARKER")"
CURRENT_REMOTE=$(git ls-remote "$REMOTE_URL" refs/heads/master | awk '{print $1}')
if [ -f "$MARKER" ]; then
  SYNCED=$(cat "$MARKER")
  if [ "$CURRENT_REMOTE" != "$SYNCED" ]; then
    echo "!! GitHub 远端 HEAD ($CURRENT_REMOTE) 与上次同步记录 ($SYNCED) 不一致。" >&2
    echo "   说明有 PR 合并或网页改动尚未回流到本地主线。" >&2
    echo "   请先运行：bash scripts/merge_github_pr.sh" >&2
    exit 1
  fi
else
  echo "   首次运行：把当前远端 HEAD 记为同步基线"
fi

# ---- 2. 导出本地主线代码树并净化 ----
echo "==> 导出 ${SRC_SHORT} 的代码树"
TREE="$WORKDIR/tree"
REPO="$WORKDIR/repo"
mkdir -p "$TREE" "$REPO"
git archive "$SRC_COMMIT" | tar -x -C "$TREE"
cd "$TREE"

echo "==> 写入开源版 README"
cp docs/publish/README_GITHUB.md README.md

# 安全红线：docs/product/screenshots/*.png 是真实环境截图（含真实自选股、持仓金额、
# 钉钉 webhook token），图片无法被下面的文本 grep 检出，**严禁进入公开快照**。
# 唯一允许的截图来源是 docs/publish/screenshots/ —— 该目录的每张图都已逐张人工核验
# 为脱敏（股票名/代码/金额均为 •••••）；往该目录新增图片前必须先做同样的核验。
echo "==> 拷贝已核验的脱敏截图（仅 docs/publish/screenshots/）"
mkdir -p screenshots
cp docs/publish/screenshots/*.png screenshots/

echo "==> 移除内部文档与个人内容"
rm -rf docs test_scripts
rm -f scripts/deploy.sh docker/docker-compose.yml docker/DEPLOY.md requirements.lock
rm -f config/favorites.json config/alert_rules.json config/company_profiles.json \
      config/sentiment_history.json config/strategies.json \
      config/trade_strategy_long.json config/trade_strategy_mid_long.json \
      config/trade_strategy_short.json config/valuation_rules.json \
      config/market_regime_cache.json
# 注意：decision_rules.json 是规则契约（无密钥/持仓），CI 单测依赖它，不在删除之列。

echo "==> Dockerfile.local 去除私有 SDK 安装步骤"
sed -i -e '\|随仓库 wheels/ 提供|d' -e '\|COPY wheels/|d' -e '\|/tmp/wheels|d' docker/Dockerfile.local
cat >> docker/Dockerfile.local <<'EOF'

# 注：星耀数智(AmazingData) 私有 SDK 不随开源仓库分发（wheels/ 未入库）。
# 行情链路会自动降级到 腾讯 -> 雪球 -> 东财 -> AkShare 兜底，无需本 SDK。
EOF

# 泄露自检：只匹配真实密钥值格式（ModelScope ms-UUID / 钉钉 webhook URL / 钉钉 SEC 加签），
# 不匹配字段名引用；排除本脚本自身（其文本里含这些正则字面量）。
echo "==> 泄露自检（真实密钥值格式不应进入快照）"
if grep -rInE "ms-[0-9a-f-]{30,}|oapi\.dingtalk\.com/robot/send\?access_token=[a-z0-9]{16,}|SEC[a-f0-9]{32}" \
    --exclude=publish_github.sh . ; then
  echo "!! 快照中疑似含敏感内容，中止发布" >&2
  exit 1
fi

# ---- 3. 在 GitHub 历史上做增量提交 ----
echo "==> 拉取 GitHub 当前历史（保留 PR 合并记录）"
cd "$REPO"
git init -q -b master
git fetch -q "$REMOTE_URL" master
git reset -q --hard FETCH_HEAD

echo "==> 用净化树替换工作区内容"
git rm -rq .
(cd "$TREE" && tar -cf - .) | tar -xf -

git add -A
if git diff --cached --quiet; then
  echo "==> 净化内容与 GitHub 当前内容一致，无需同步"
  echo "$CURRENT_REMOTE" > "$MARKER"
  exit 0
fi

git -c core.autocrlf=false commit -q \
  -m "sync: monitor_stock@${SRC_SHORT} ($(date +%Y-%m-%d))"

echo "==> 普通推送到 ${REMOTE_URL}（非 --force）"
# 直接按 URL 推，不依赖本地远程名；代理按 URL 前缀匹配，照常生效。
git push "$REMOTE_URL" master:master

NEW_HEAD=$(git rev-parse HEAD)
echo "$NEW_HEAD" > "$MARKER"
echo "==> 完成。同步提交：$(git rev-parse --short HEAD)（源自 ${SRC_SHORT}）"
