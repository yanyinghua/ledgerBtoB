#!/usr/bin/env bash
# ============================================================================
# 往来账管理系统 —— 一键安装
#
#   ./install.sh                 安装并启动（默认 80 端口）
#   HTTP_PORT=8080 ./install.sh  指定对外端口
#   ./install.sh --port 8080     同上
#
# 幂等：重复执行不会重建已有数据卷，只会补齐 .env 并拉起服务。
# ============================================================================
set -euo pipefail

PORT="${HTTP_PORT:-80}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-80}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1. 环境检查 ----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "未检测到 docker，请先安装：https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "未检测到 docker compose 插件，请升级 Docker（>=20.10 自带 compose 插件）"
docker info >/dev/null 2>&1 || die "docker 守护进程未运行，或当前用户无权限（可尝试 sudo usermod -aG docker $USER 后重新登录）"

# ---- 2. 生成 .env ---------------------------------------------------------
if [[ ! -f .env ]]; then
  say "生成 .env"
  rand() {
    if command -v openssl >/dev/null 2>&1; then openssl rand -base64 48 | tr -d '\n/+=' | head -c 50;
    else LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 50; fi
  }
  sed -e "s|__GENERATE_SECRET_KEY__|$(rand)|" \
      -e "s|__GENERATE_DB_PASSWORD__|$(rand)|" \
      deploy/env.docker.template > .env
  chmod 600 .env
else
  say ".env 已存在，保留原有密钥与数据库口令"
fi

if grep -q '^HTTP_PORT=' .env; then
  sed -i.bak "s|^HTTP_PORT=.*|HTTP_PORT=${PORT}|" .env && rm -f .env.bak
else
  printf 'HTTP_PORT=%s\n' "$PORT" >> .env
fi

# ---- 3. 启动 --------------------------------------------------------------
say "构建并启动服务（首次约需几分钟）"
docker compose up -d --build

# ---- 4. 等待就绪 ----------------------------------------------------------
URL="http://127.0.0.1:${PORT}/signin/"
say "等待服务就绪：${URL}"
ok=0
for _ in $(seq 1 90); do
  if curl -fs -o /dev/null "${URL}"; then ok=1; break; fi
  sleep 2
done
if [[ ${ok} -ne 1 ]]; then
  echo "服务未在 3 分钟内就绪，最近日志："
  docker compose logs --tail 40 web
  exit 1
fi

# ---- 5. 收尾提示 ----------------------------------------------------------
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
printf '\n\033[1;32m安装完成\033[0m\n\n'
printf '  本机访问: %s\n' "${URL}"
printf '  局域网访问: http://%s:%s/signin/   （访问不到请检查防火墙是否放行 %s 端口）\n' "${HOST_IP}" "${PORT}" "${PORT}"
cat <<EOF

  1) 用初始账号登录：admin / admin123，登录后立即修改密码
  2) 新装默认 30 天试用；进入「授权管理」页 /license/ 粘贴授权码激活
  3) 数据保存在 docker 卷 pgdata，卸载前请先备份：
       docker compose exec -T db pg_dump -U ledger ledger > backup-$(date +%F).sql

  常用命令：
     docker compose logs -f web     查看日志
     docker compose restart         重启
     docker compose down            停止（数据保留）
EOF
