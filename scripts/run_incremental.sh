#!/usr/bin/env bash
# 增量导入包装脚本：切到项目目录，跑 import_incremental，结果追加到日志。
# 导入失败时写入独立告警日志并以非 0 退出码结束，便于上层（cron / systemd）感知。
#
# 数据源目录通过环境变量 WINSHARE_DIR 指定（默认 /mnt/win_share）。
# 正式环境建议把它指向一个稳定的本地目录（如拷贝进来的 incoming 文件夹），
# 避免直接依赖可能未挂载的临时挂载点。
set -u

PROJECT_DIR="/home/yanyinghua/projects/my-project1"
SRC="${WINSHARE_DIR:-/mnt/win_share}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/incremental_$(date +%Y-%m).log"
ALERT_FILE="$LOG_DIR/incremental_failed.log"

mkdir -p "$LOG_DIR"

PYTHON="$(command -v python3)"

run_import() {
  cd "$PROJECT_DIR" || { echo "无法进入项目目录 $PROJECT_DIR"; return 1; }
  "$PYTHON" manage.py import_incremental --src "$SRC" 2>&1
}

# 挂载/目录不可用：跳过本轮并记录告警，避免静默误判为「无新数据」或崩溃循环
if [ ! -d "$SRC" ]; then
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 跳过：数据源目录不存在或未挂载：$SRC ====="
  } >> "$LOG_FILE"
  {
    echo "!!!! 数据源不可用 $(date '+%Y-%m-%d %H:%M:%S') 目录不存在或未挂载：$SRC !!!!"
  } >> "$ALERT_FILE"
  exit 0
fi

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始增量导入（源：$SRC） ====="
  OUT="$(run_import)"
  RC=$?
  echo "$OUT"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 结束（退出码 $RC） ====="

  if [ "$RC" -ne 0 ]; then
    {
      echo "!!!! 增量导入失败 $(date '+%Y-%m-%d %H:%M:%S') 退出码=$RC !!!!"
      echo "$OUT" | tail -n 20
      echo "----------------------------------------"
    } >> "$ALERT_FILE"
    if [ -n "${DISPLAY:-}" ] && command -v notify-send >/dev/null 2>&1; then
      notify-send "往来账增量导入失败" "$(date '+%Y-%m-%d %H:%M:%S') 详见 logs/incremental_failed.log"
    fi
  fi
  exit $RC
} >> "$LOG_FILE" 2>&1

exit $?
