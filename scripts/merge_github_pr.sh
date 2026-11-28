#!/usr/bin/env bash
# 回流：把 GitHub 上已合并的 PR 提交 cherry-pick 回本地主线，再推 Gitee。
#
# 为什么不能整树拉回：GitHub 是净化镜像（删了 docs/、个人配置、部署脚本，
# README 换成开源版，Dockerfile.local 被改写）。整树覆盖会把这些净化版内容
# 写回本地主线，导致内部资料丢失、规则文件被删。只搬运 PR 的提交才是安全的。
#
# 前置：发版脚本已在 .workbuddy/github-synced-head 记录上次同步点。
#
# 用法：
#   bash scripts/merge_github_pr.sh <PR提交sha> [另一个sha ...]
#   bash scripts/merge_github_pr.sh --auto      # 自动找出 github/master 上领先标记点的提交
#   bash scripts/merge_github_pr.sh --dry-run   # 只列出将要回流的提交，不改文件
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

MARKER="$REPO_ROOT/.workbuddy/github-synced-head"
REMOTE_URL=$(git remote get-url github)
MODE="${1:-}"
DRY_RUN=0
[ "$MODE" = "--dry-run" ] && DRY_RUN=1

if [ ! -f "$MARKER" ]; then
  echo "!! 找不到同步标记 $MARKER，请先运行 publish_github.sh 建立基线" >&2
  exit 1
fi
SYNCED=$(cat "$MARKER")

echo "==> 拉取 GitHub 最新历史（不改动当前工作区）"
git fetch -q "$REMOTE_URL" master
GH_HEAD=$(git rev-parse FETCH_HEAD)

# 计算候选提交
if [ "$MODE" = "--auto" ] || [ "$MODE" = "--dry-run" ]; then
  # 列出 GitHub 上领先标记、且不是 sync: 同步提交的提交（旧->新）
  CANDIDATES=$(git log --reverse --format='%H%x09%s' "$SYNCED..$GH_HEAD" \
    | awk -F'\t' '$2 !~ /^sync:/ {print $1}')
  if [ -z "$CANDIDATES" ]; then
    echo "==> 没有需要回流的 PR 提交（GitHub HEAD: ${GH_HEAD:0:7}）"
    exit 0
  fi
  echo "==> 候选提交："
  for sha in $CANDIDATES; do
    git log -1 --format='  %h  %s  (by %an)' "$sha"
  done
else
  CANDIDATES=""
  for arg in "$@"; do
    # 每个参数解析为完整 sha，且必须存在于 GitHub 历史中
    sha=$(git rev-parse "$arg" 2>/dev/null || true)
    if [ -z "$sha" ]; then
      echo "!! 无法解析提交：$arg" >&2
      exit 1
    fi
    if ! git merge-base --is-ancestor "$sha" "$GH_HEAD" 2>/dev/null; then
      echo "!! 提交 $arg 不在 github/master 历史上，拒绝回流" >&2
      exit 1
    fi
    CANDIDATES="$CANDIDATES $sha"
  done
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "==> --dry-run：仅列出，未改动任何文件"
  exit 0
fi

# 逐条 cherry-pick（merge 提交需要 -m 1 指定主线）
echo "==> cherry-pick 到本地主线"
for sha in $CANDIDATES; do
  parents=$(git rev-list --parents -n 1 "$sha" | wc -w)
  if [ "$parents" -gt 2 ]; then
    echo "  -- merge commit，按 -m 1 挑选：${sha:0:7}"
    git cherry-pick -m 1 "$sha"
  else
    echo "  -- 普通提交：${sha:0:7}"
    git cherry-pick "$sha"
  fi
done

echo "==> 推送 Gitee（origin/master）"
git push origin master

echo "$GH_HEAD" > "$MARKER"
echo "==> 完成。已回流 $(echo $CANDIDATES | wc -w) 个提交，标记更新为 ${GH_HEAD:0:7}"
