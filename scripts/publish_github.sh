#!/usr/bin/env bash
# 开源发布：把当前代码打包成「单提交净化快照」强推到 GitHub。
#
# 背景：gitee（origin）保存完整开发历史；github 是公开仓库，只保留净化快照，
# 不携带完整历史（历史中的 config/*.bak 曾含真实密钥，README/docs 含内部运维
# 与个人策略内容）。因此 github 的 master 每次都是全新孤儿提交，必须 --force。
#
# 用法：bash scripts/publish_github.sh [commit-ish]   # 默认 HEAD
#
# 快照内容：
#   - 导出 HEAD 跟踪的文件（git archive，天然排除 .env / wheels/ 等未跟踪内容）
#   - 删除内部目录与个人配置（docs/、test_scripts/、config/*.json 个人数据等）
#   - README 替换为开源版（docs/publish/README_GITHUB.md）
#   - Dockerfile.local 去掉私有 SDK 安装两行（延迟导入，缺失不影响运行）
set -euo pipefail

SRC_COMMIT=$(git rev-parse "${1:-HEAD}")
SRC_SHORT=$(git rev-parse --short "${SRC_COMMIT}")
REMOTE_NAME=github
REMOTE_URL=$(git remote get-url "$REMOTE_NAME")
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> 导出 ${SRC_SHORT} 的代码树"
git archive "${SRC_COMMIT}" | tar -x -C "${WORKDIR}"
cd "${WORKDIR}"

echo "==> 写入开源版 README"
cp docs/publish/README_GITHUB.md README.md

echo "==> 移除内部文档与个人内容"
rm -rf docs test_scripts
rm -f scripts/deploy.sh docker/docker-compose.yml docker/DEPLOY.md requirements.lock
rm -f config/favorites.json config/alert_rules.json config/company_profiles.json \
      config/sentiment_history.json config/decision_rules.json config/strategies.json \
      config/trade_strategy_long.json config/trade_strategy_mid_long.json \
      config/trade_strategy_short.json config/valuation_rules.json

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

echo "==> 生成单提交快照"
git init -q -b master
git add -A
git -c core.autocrlf=false commit -q \
  -m "release: sanitized snapshot of monitor_stock@${SRC_SHORT} ($(date +%Y-%m-%d))"

echo "==> 强推到 ${REMOTE_URL}"
git remote add github "${REMOTE_URL}"
git push --force github master:master

echo "==> 完成。快照提交：$(git rev-parse --short HEAD)（源自 ${SRC_SHORT}）"
