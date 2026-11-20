#!/usr/bin/env bash
#
# 查询 yanyinghua/ledger 仓库最近一次 GitHub Actions run 的整体与 job 状态。
# 仅用于「看 CI 结果」，需要 Personal Access Token（PAT），但**绝不写在脚本里/命令行参数里**。
#
# 安全用法（PAT 存文件、临时导成环境变量）：
#   install -m 600 /dev/null ~/.config/github-token   # 首次建空文件（权限 600）
#   cat > ~/.config/github-token                       # 粘贴 PAT 后 Ctrl-D 保存
#   export GITHUB_TOKEN="$(cat ~/.config/github-token)"
#   ./deploy/check-ci.sh
#
# 用完可 `unset GITHUB_TOKEN` 从当前 shell 清掉。
set -euo pipefail

REPO="yanyinghua/ledger"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "未设置 GITHUB_TOKEN 环境变量。" >&2
  echo "请先从 ~/.config/github-token 读取并导出：" >&2
  echo '  export GITHUB_TOKEN="$(cat ~/.config/github-token)"' >&2
  exit 1
fi

echo ">>> 最近 3 次 workflow run："
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/actions/runs?per_page=3" \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin)['workflow_runs']:
    print(f\"{r['id']} | {r['display_title']} | {r['status']} | {r['conclusion']} | {r['head_sha'][:7]} | {r['html_url']}\")
"

latest=$(curl -s -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/actions/runs?per_page=1" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['workflow_runs'][0]['id'])")

echo ">>> run $latest 各 job 状态："
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/actions/runs/$latest/jobs" \
  | python3 -c "
import sys, json
for j in json.load(sys.stdin)['jobs']:
    print(f\"  {j['name']} | {j['status']} | {j['conclusion']}\")
"
