#!/usr/bin/env bash
# ============================================================================
# 生成交付包：只打包运行必需的文件，剔除代码仓库、虚拟环境、业务数据与内部文档。
#
#   ./scripts/build_package.sh              版本号取 git tag / 当前日期
#   ./scripts/build_package.sh 1.2.0        指定版本号
#
# 产物：dist/ledger-<版本>.tar.gz 与同名 .sha256
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  VERSION=$(git describe --tags --always 2>/dev/null | sed 's/^v//') || true
  VERSION="${VERSION:-$(date +%Y%m%d)}"
fi

NAME="ledger-${VERSION}"
STAGE="${ROOT}/dist/${NAME}"
OUT="${ROOT}/dist"

echo "==> 版本: ${VERSION}"
rm -rf "${STAGE}"
mkdir -p "${STAGE}"

# ---- 打包清单（白名单，其余一律不带） --------------------------------------
INCLUDE=(
  manage.py requirements.txt
  Dockerfile docker-compose.yml docker-entrypoint.sh install.sh
  ledger books licensing
  deploy/nginx/docker.conf deploy/env.docker.template
  docs/安装手册.md
)

for item in "${INCLUDE[@]}"; do
  if [[ ! -e "${item}" ]]; then
    echo "错误：缺少 ${item}" >&2
    exit 1
  fi
  mkdir -p "${STAGE}/$(dirname "${item}")"
  cp -a "${item}" "${STAGE}/${item}"
done

# 安装手册放一份到根目录，买方解压第一眼就能看到
cp -a docs/安装手册.md "${STAGE}/安装手册.md"

# ---- 清理 ------------------------------------------------------------------
find "${STAGE}" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true
rm -rf "${STAGE}/books/tests.py" 2>/dev/null || true

# ---- 自检：确认没夹带私货 --------------------------------------------------
echo "==> 自检"
LEAK=$(find "${STAGE}" \( -name '.git*' -o -name '.env' -o -name '*.sqlite3' \
        -o -name 'db.sqlite3' -o -name 'private.key' \) -print | head -5)
if [[ -n "${LEAK}" ]]; then
  echo "错误：包内发现不应外发的文件：" >&2
  echo "${LEAK}" >&2
  exit 1
fi

# ---- 压缩 ------------------------------------------------------------------
mkdir -p "${OUT}"
TARBALL="${OUT}/${NAME}.tar.gz"
tar -czf "${TARBALL}" -C "${OUT}" "${NAME}"
(cd "${OUT}" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256")

echo
echo "==> 产物"
ls -lh "${TARBALL}"
cat "${OUT}/${NAME}.tar.gz.sha256"
echo
echo "解压试用： tar -xzf ${NAME}.tar.gz && cd ${NAME} && ./install.sh"
echo "（清理暂存目录： rm -rf dist/${NAME}）"
