#!/usr/bin/env bash
# 数据库备份包装脚本：在线热备 + 按分级策略清理旧备份，结果追加到日志。
# 失败时写入独立告警日志并以非 0 退出码结束，便于上层（cron）感知。
#
# 用 cron 直接调 manage.py 也能跑，但包装成脚本有两个实际好处：
#   1. crontab 里的 % 必须转义成 \%，漏转义会导致命令被静默截断；
#      时间戳在脚本里生成就完全绕开这个坑。
#   2. cron 默认只把输出发本地邮件（本机没配 MTA 等于丢弃），
#      落盘到 logs/ 才能事后排查。
#
# 可用环境变量覆盖：
#   LEDGER_DIR        项目目录（默认 /srv/ledger）
#   BACKUP_DIR        备份输出目录（默认 /var/backups/ledger）
#   BACKUP_KEEP       保留最近份数（默认 20）
#   BACKUP_KEEP_DAYS  保留天数窗口（默认 14）
set -u

PROJECT_DIR="${LEDGER_DIR:-/srv/ledger}"

# cron 的环境几乎是空的，拿不到 systemd EnvironmentFile 里的 DJANGO_DB_*，
# settings.py 会因此回落到 sqlite3 —— 备份出来的是一份早已停用的旧库，
# 而且退出码是 0，不会有任何告警。必须显式加载同一份 env。
ENV_FILE="${LEDGER_ENV_FILE:-/etc/ledger/env}"
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ledger}"
KEEP="${BACKUP_KEEP:-20}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/backup_$(date +%Y-%m).log"
ALERT_FILE="$LOG_DIR/backup_failed.log"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"

# 备份文件名（db-<YYYYMMDD>-<HHMMSS>.sqlite3 或 .dump）由 backup_db 按引擎决定，
# 这是它识别「例行备份」的唯一格式。写成别的（如 db-$(date +%F).sqlite3）会被当作
# 安全备份永久保留，备份目录将重新无限增长。故这里不再写死扩展名，只传 --backup-dir。

PYTHON="$PROJECT_DIR/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

run_backup() {
  cd "$PROJECT_DIR" || { echo "无法进入项目目录 $PROJECT_DIR"; return 1; }
  "$PYTHON" manage.py backup_db \
    --backup-dir "$BACKUP_DIR" --keep "$KEEP" --keep-days "$KEEP_DAYS" 2>&1
}

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始备份（保留 $KEEP 份 / $KEEP_DAYS 天） ====="
  OUT="$(run_backup)"
  RC=$?
  echo "$OUT"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 结束（退出码 $RC） ====="

  if [ "$RC" -ne 0 ]; then
    {
      echo "!!!! 数据库备份失败 $(date '+%Y-%m-%d %H:%M:%S') 退出码=$RC !!!!"
      echo "$OUT" | tail -n 20
      echo "----------------------------------------"
    } >> "$ALERT_FILE"
  fi
  exit $RC
} >> "$LOG_FILE" 2>&1

exit $?
