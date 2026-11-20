#!/usr/bin/env bash
# 一键部署 / 更新：往来账管理系统 (ledger)
# 在腾讯云轻量服务器上以 root 执行：
#   sudo bash /srv/ledger/deploy/deploy.sh
# 前置：代码已位于 /srv/ledger，虚拟环境 /srv/ledger/venv，
#       生产环境变量 /etc/ledger/env 已按 DEPLOY.md 第4步配置。

set -euo pipefail

# ---------------------------------------------------------------------------
# 部署日志落盘：把整个部署过程（含 migrate/collectstatic/pip 等子命令输出）同时
# 写终端与 /var/log/ledger/deploy-YYYYMMDD.log，便于事后审计。
# 用命名管道 + tee，脚本退出后 tee 收到 EOF 再 flush，避免丢失末尾几行。
# ---------------------------------------------------------------------------
LOG_DIR=/var/log/ledger
mkdir -p "$LOG_DIR"
DEPLOY_LOG="$LOG_DIR/deploy-$(date +%F).log"
LOGPIPE=$(mktemp -u)
mkfifo "$LOGPIPE"
tee -a "$DEPLOY_LOG" < "$LOGPIPE" &
TEE_PID=$!
exec > "$LOGPIPE" 2>&1
trap 'exec >&- 2>&-; wait "$TEE_PID" 2>/dev/null; chown "$RUN_USER":"$RUN_USER" "$DEPLOY_LOG" 2>/dev/null || true; rm -f "$LOGPIPE"' EXIT

APP_DIR=/srv/ledger
VENV="$APP_DIR/venv"
ENV_FILE=/etc/ledger/env
BACKUP_DIR=/var/backups/ledger
RUN_USER="${RUN_USER:-www}"

log() { echo "==> [$1] $2"; }

log "$(date '+%F %T')" "开始部署 ledger"

# 1) 载入生产环境变量
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少环境变量文件 $ENV_FILE，请先按 DEPLOY.md 第4步配置" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$APP_DIR"

# 1.5) 生产环境变量自检（门禁）：校验 /etc/ledger/env，不通过则中止部署
# 仅当启用 HTTPS(SECURE_SSL_REDIRECT=True) 时才做 --strict-hsts 强制校验；
# HTTP-only 阶段用普通校验即可（仍检查 SECRET_KEY/DEBUG/ALLOWED_HOSTS）。
if [ "${DJANGO_SECURE_SSL_REDIRECT:-True}" = "True" ]; then
  CHECK_ARGS="--strict-hsts"
else
  CHECK_ARGS=""
fi
if ! "$VENV/bin/python" "$APP_DIR/scripts/check_env.py" $CHECK_ARGS; then
  echo "环境变量校验未通过，已中止部署（请检查 /etc/ledger/env）" >&2
  exit 1
fi

# 2) 备份数据库（在线一致性热备，替代 cp，避免活库拷贝损坏）
# 引擎感知：SQLite 备份为 *.sqlite3，PostgreSQL 备份为 *.dump（backup_db 自动判断）。
mkdir -p "$BACKUP_DIR"
"$VENV/bin/python" manage.py backup_db \
  --backup-dir "$BACKUP_DIR" \
  --keep "${BACKUP_KEEP:-20}" --keep-days "${BACKUP_KEEP_DAYS:-14}" \
  && log "$(date '+%F %T')" "已在线热备数据库到 $BACKUP_DIR" \
  || log "$(date '+%F %T')" "⚠ 数据库备份失败，请检查"

# 3) 依赖（始终确保安装，变动时才生效）
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" install -q gunicorn

# 4) 数据库迁移
"$VENV/bin/python" manage.py migrate --noinput

# 5) 收集静态文件
"$VENV/bin/python" manage.py collectstatic --noinput

# 6) 部署映射 fixture（幂等，已存在则跳过）
"$VENV/bin/python" manage.py loaddata column_mappings || \
  log "$(date '+%F %T')" "loaddata 跳过或无需更新"

# 6.1) PostgreSQL：修正自增序列
# loaddata 与 0016 这类种子迁移都用「显式 pk」写入，而 PostgreSQL 的序列只在
# 走 DEFAULT nextval() 时才前进 —— 显式 pk 不会推动它。于是序列停留在旧值，
# 下一次由用户新建记录时就会撞上 duplicate key（SQLite 用 max(rowid)+1，天然没这问题）。
# 每次部署后对齐一次，成本极低，可彻底避免这类偶发 500。
# 先落盘再喂给 dbshell：管道只返回最后一条命令的状态，
# sqlsequencereset 自己失败（如漏了 app 名）会被 dbshell 的 0 掩盖成「成功」。
if [[ "${DJANGO_DB_ENGINE:-sqlite3}" == "postgresql" ]]; then
  SEQ_SQL="$(mktemp)"
  if "$VENV/bin/python" manage.py sqlsequencereset books auth admin sessions > "$SEQ_SQL" 2>/dev/null \
     && [[ -s "$SEQ_SQL" ]] \
     && "$VENV/bin/python" manage.py dbshell < "$SEQ_SQL" >/dev/null 2>&1; then
    log "$(date '+%F %T')" "PostgreSQL 自增序列已对齐"
  else
    log "$(date '+%F %T')" "警告：自增序列对齐失败，请手动执行 sqlsequencereset"
  fi
  rm -f "$SEQ_SQL"
fi

# 7) 修正属主，确保 gunicorn(www) 可访问
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/staticfiles" 2>/dev/null || true
# SQLite 才需要给应用用户数据库文件属主；PostgreSQL 的数据由 PG 进程自行管理。
if [[ "${DJANGO_DB_ENGINE:-sqlite3}" == "sqlite3" && -f "$APP_DIR/db.sqlite3" ]]; then
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/db.sqlite3" 2>/dev/null || true
fi
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/media" 2>/dev/null || true

# 7.5) 同步 gunicorn worker/thread 配置到 systemd unit（sed 改写，保留现有 User/路径等）
# 仅改写 --workers / --threads 数值，避免整体覆盖破坏服务器上既有的运行用户与目录属主。
# 背景：PostgreSQL 环境下应使用多 worker（提升并发与单进程宕机容错），不再需要当初为
# 规避 SQLite 写锁而临时降到 1 worker 的设置。改过 unit 文件后须 daemon-reload 才能生效。
UNIT_FILE=/etc/systemd/system/ledger.service
if [[ -f "$UNIT_FILE" ]]; then
  if sed -i -E 's/(--workers )[0-9]+/\12/; s/(--threads )[0-9]+/\12/' "$UNIT_FILE"; then
    log "$(date '+%F %T')" "已同步 $UNIT_FILE 的 workers/threads 为 2/2"
  else
    echo "警告：sed 改写 $UNIT_FILE 失败，gunicorn worker 配置未同步" >&2
  fi
else
  log "$(date '+%F %T')" "未找到 $UNIT_FILE，跳过 worker 配置同步（请确认 systemd 单元是否存在）"
fi

# 8) 重启服务（改过 unit 文件需先 daemon-reload 才能生效）
systemctl daemon-reload
systemctl restart ledger
# nginx 重载：兼容 systemd 托管与宝塔(二进制)两种管理方式
systemctl reload nginx 2>/dev/null || \
  /www/server/nginx/sbin/nginx -s reload 2>/dev/null || \
  nginx -s reload 2>/dev/null || true

log "$(date '+%F %T')" "部署完成。访问 https://ledger.yanyinghuaerp.top"
