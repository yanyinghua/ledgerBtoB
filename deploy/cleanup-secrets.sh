#!/usr/bin/env bash
#
# 清理仓库 Settings → Secrets → Actions 里多余的 DJANGO_* 密钥。
# 这些密钥已不再被 ci.yml 使用（生产密钥改存服务器 /etc/ledger/env），
# 留着既无作用又多一个泄露面，应删除。
#
# 用法：
#   GITHUB_TOKEN=ghp_xxx ./deploy/cleanup-secrets.sh
#   # 或交互式输入
#   ./deploy/cleanup-secrets.sh
#
# 需要权限：PAT 需含 repo（含 actions:read/write 可用于删 Secrets）
set -euo pipefail

REPO="yanyinghua/ledger"
SECRETS=(DJANGO_SECRET_KEY DJANGO_DEBUG DJANGO_HSTS_SECONDS DJANGO_ALLOWED_HOSTS DJANGO_SECURE_SSL_REDIRECT)

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  read -rsp "GitHub PAT (用于删除 Secrets): " GITHUB_TOKEN
  echo
fi

BASE="https://api.github.com/repos/$REPO/actions/secrets"

for name in "${SECRETS[@]}"; do
  echo ">> 删除 $name ..."
  http_code=$(curl -s -o /tmp/secret_del_resp -w '%{http_code}' \
    -X DELETE \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$BASE/$name")

  case "$http_code" in
    204) echo "   ✅ 已删除 ($name)" ;;
    404) echo "   ⚠️  不存在/已删除 ($name)" ;;
    401) echo "   ❌ 401 鉴权失败，请检查 PAT 权限（需 repo 权限）"; exit 1 ;;
    403) echo "   ❌ 403 禁止，PAT 可能无 actions 权限或仓库为组织禁用"; exit 1 ;;
    *)   echo "   ❌ 意外响应 $http_code："; cat /tmp/secret_del_resp; exit 1 ;;
  esac
done

echo "完成。GitHub 上应仅剩 DEPLOY_HOST / DEPLOY_USER / SSH_PRIVATE_KEY。"
