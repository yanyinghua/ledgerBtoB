#!/usr/bin/env python3
"""CI / 生产前置：校验部署所需环境变量已正确设置。

退出码：
  0 = 通过
  1 = 存在致命问题（不应部署）
  2 = 使用方式错误

用法：
  python3 scripts/check_env.py [--env-file .env] [--strict-hsts]

可在 CI 流水线早期运行，作为生产密钥配置的门禁：
未设置 DJANGO_SECRET_KEY / DEBUG 仍为 True / ALLOWED_HOSTS 为 "*" 时直接失败。
"""
import os
import sys
import argparse


def load_env_file(path):
    """从简单的 KEY=VALUE 文件载入变量（不覆盖已存在的环境变量）。"""
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, val = line.split('=', 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def _is_true(val):
    return val.strip().lower() in ('true', '1', 'yes', 'on')


def main():
    ap = argparse.ArgumentParser(description='校验生产环境变量')
    ap.add_argument('--env-file', help='可选：从 .env 文件载入变量（不覆盖已有环境变量）')
    ap.add_argument('--strict-hsts', action='store_true',
                    help='要求 DJANGO_SECURE_SSL_REDIRECT 开启且 DJANGO_HSTS_SECONDS>0')
    args = ap.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    errors = []
    warnings = []

    # 1) SECRET_KEY
    sk = os.environ.get('DJANGO_SECRET_KEY')
    if not sk:
        errors.append('DJANGO_SECRET_KEY 未设置（生产必须设置独立的长随机值）')
    elif len(sk) < 50:
        warnings.append('DJANGO_SECRET_KEY 长度 < 50，建议使用更长随机值')

    # 2) DEBUG
    if _is_true(os.environ.get('DJANGO_DEBUG', 'True')):
        errors.append('DJANGO_DEBUG 为 True（生产必须 False / 0）')

    # 3) ALLOWED_HOSTS
    ah = os.environ.get('DJANGO_ALLOWED_HOSTS', '*')
    hosts = [h.strip() for h in ah.split(',') if h.strip()]
    if not hosts or hosts == ['*']:
        errors.append('DJANGO_ALLOWED_HOSTS 未设置或为 "*"（生产必须指定具体域名）')

    # 4) HSTS（可选严格模式）
    if args.strict_hsts:
        ssl_on = os.environ.get('DJANGO_SECURE_SSL_REDIRECT', 'True').strip().lower() in ('true', '1')
        try:
            hsts = int(os.environ.get('DJANGO_HSTS_SECONDS', '0') or 0)
        except ValueError:
            hsts = 0
        if not ssl_on:
            errors.append('strict-hsts: DJANGO_SECURE_SSL_REDIRECT 未开启')
        if hsts <= 0:
            errors.append('strict-hsts: DJANGO_HSTS_SECONDS 未设置(>0)')

    # 5) 数据库引擎相关校验
    db_engine = os.environ.get('DJANGO_DB_ENGINE', 'sqlite3').strip().lower()
    # 本脚本仅用于生产部署门禁（deploy.sh 调用），硬性要求 PostgreSQL。
    # 禁止 sqlite3 或无引擎：那样 settings 会静默回落到文件库，既无并发能力，
    # 又会在 gunicorn 冷启动抢锁时偶发 500；更糟的是低 worker 数会掩盖该错误部署。
    if db_engine != 'postgresql':
        errors.append(
            f'DJANGO_DB_ENGINE 必须为 postgresql（当前为 "{db_engine}"）；'
            '生产环境禁止 SQLite 静默回落'
        )
    else:
        for var in ('DJANGO_DB_NAME', 'DJANGO_DB_USER', 'DJANGO_DB_PASSWORD', 'DJANGO_DB_HOST'):
            if not os.environ.get(var):
                errors.append(f'DJANGO_DB_ENGINE=postgresql 但 {var} 未设置')

    for w in warnings:
        print(f'[WARN] {w}')
    for e in errors:
        print(f'[FAIL] {e}')

    if errors:
        print(f'\n环境变量校验未通过：{len(errors)} 项错误。')
        sys.exit(1)
    if warnings:
        print(f'\n环境变量校验通过（含 {len(warnings)} 条警告）。')
    else:
        print('\n环境变量校验通过 ✅')
    sys.exit(0)


if __name__ == '__main__':
    main()
