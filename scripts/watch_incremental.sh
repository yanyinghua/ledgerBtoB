#!/usr/bin/env bash
# 监听 /mnt/win_share：仅当目录内容指纹变化时才跑增量导入（等价于 inotify 触发，
# 但纯 bash 轮询、零外部依赖，靠 systemd 用户服务 + linger 在注销/重启后持续运行）。
set -u

PROJECT_DIR="/home/yanyinghua/projects/my-project1"
SRC="${WINSHARE_DIR:-/mnt/win_share}"
LOG_DIR="$PROJECT_DIR/logs"
FP_FILE="$LOG_DIR/.winshare_fingerprint"
INTERVAL=60   # 轮询间隔（秒）

mkdir -p "$LOG_DIR"

# 计算目录指纹：所有文件的 mtime+size+路径排序后取 md5
compute_fp() {
  find "$SRC" -type f -printf '%T@ %s %p\n' 2>/dev/null | sort | md5sum | awk '{print $1}'
}

LAST="$(cat "$FP_FILE" 2>/dev/null || echo "")"
echo "$(date '+%Y-%m-%d %H:%M:%S') watch_incremental 启动（监听 $SRC，每 ${INTERVAL}s 轮询）"

while true; do
  # 源未挂载/不存在：跳过本轮，不更新指纹（避免把「空目录」当作稳定状态）
  if [ ! -d "$SRC" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') 数据源 $SRC 未挂载，跳过本次轮询"
    sleep "$INTERVAL"
    continue
  fi
  CUR="$(compute_fp)"
  if [ "$CUR" != "$LAST" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') 检测到 $SRC 变动，触发增量导入"
    "$PROJECT_DIR/scripts/run_incremental.sh"
    LAST="$CUR"
    echo "$CUR" > "$FP_FILE"
  fi
  sleep "$INTERVAL"
done
