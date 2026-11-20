#!/usr/bin/env bash
#
# 检查模板里是否引用了外部 CDN 的静态资源（css/js/图片/字体/iframe）。
#
# 背景：2026-10-29 手机端出现整页裸 HTML，原因是 base.html/login.html 从
# cdn.jsdelivr.net 拉 Bootstrap；国内移动网络对该域名经常超时/被墙，
# 加载失败时页面退化成无样式列表。前端资源必须走 {% static %} 本地文件，
# 由 nginx 直接提供（见 deploy/nginx/*.conf 的 /static/ 段）。
#
# 只拦 <link>/<script>/<img>/<source>/<iframe> 的资源地址，
# 不拦 <a href="https://..."> 这类正常的站外跳转链接。
#
# 用法：
#   bash deploy/check-no-cdn.sh            # 扫描仓库根目录
#   bash deploy/check-no-cdn.sh books      # 只扫指定目录
#
set -euo pipefail

ROOTS=("$@")
if [ ${#ROOTS[@]} -eq 0 ]; then
  ROOTS=(".")
fi

# 匹配形如： <link ... href="https://cdn...">  /  <script src="//cdn...">
PATTERN='<(link|script|img|source|iframe)[^>]*(href|src)[[:space:]]*=[[:space:]]*"((https?:)?//)[^"]*"'

hits=$(grep -rnE "$PATTERN" --include='*.html' \
  --exclude-dir=dist --exclude-dir=staticfiles --exclude-dir=venv --exclude-dir=.git \
  "${ROOTS[@]}" || true)

if [ -n "$hits" ]; then
  echo "❌ 检测到模板引用了外部 CDN 静态资源（国内网络常被墙，会导致页面样式/交互整块失效）：" >&2
  echo "$hits" >&2
  echo "" >&2
  echo "请把资源下载到 books/static/books/vendor/ 后改用 {% static '...' %} 引用。" >&2
  exit 1
fi

echo "✅ 未发现外部 CDN 静态资源引用。"
