#!/usr/bin/env bash
# 本地一键部署到腾讯云轻量服务器（手动部署用；CI 自动部署见 .github/workflows/ci.yml）
# 在开发机（本机）于项目根目录执行：
#   DEPLOY_HOST=服务器公网IP  DEPLOY_USER=root  bash deploy/local-deploy.sh
#
# 前置：
#   - 本机已安装 rsync + ssh，且能密钥登录服务器
#   - 服务器已按 DEPLOY.md 第1~7步准备好 /srv/ledger、venv、/etc/ledger/env
#
# 变量（与 CI 同名，便于复用）：
#   DEPLOY_HOST  服务器公网 IP 或域名（必填）
#   DEPLOY_USER 登录用户，默认 root

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-ledger}"
DEPLOY_USER="${DEPLOY_USER:-root}"
REMOTE_DIR=/srv/ledger

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXCLUDE_FILE="$SCRIPT_DIR/rsync-exclude.txt"
if [[ ! -f "$EXCLUDE_FILE" ]]; then
  echo "缺少排除清单 $EXCLUDE_FILE，已中止（rsync --delete 会误删服务器数据）" >&2
  exit 1
fi

echo "==> 同步代码到 $DEPLOY_USER@$DEPLOY_HOST:$REMOTE_DIR"
rsync -az --delete --exclude-from="$EXCLUDE_FILE" \
  ./ "$DEPLOY_USER@$DEPLOY_HOST:$REMOTE_DIR/"

echo "==> 在服务器执行 deploy.sh"
ssh "$DEPLOY_USER@$DEPLOY_HOST" "sudo bash $REMOTE_DIR/deploy/deploy.sh"
echo "==> 部署完成"
