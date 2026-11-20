#!/bin/sh
# 容器启动入口：等库 -> 迁移 -> 收集静态 -> 拉起 gunicorn
set -e

# 等待 PostgreSQL 真正可连（healthcheck 已判活，这里再兜一层，避免迁移抢跑）
python - <<'PY'
import os, sys, time
import psycopg2

deadline = time.time() + 60
while True:
    try:
        conn = psycopg2.connect(
            dbname=os.environ.get('DJANGO_DB_NAME', 'ledger'),
            user=os.environ.get('DJANGO_DB_USER', 'ledger'),
            password=os.environ.get('DJANGO_DB_PASSWORD', ''),
            host=os.environ.get('DJANGO_DB_HOST', 'db'),
            port=os.environ.get('DJANGO_DB_PORT', '5432'),
            connect_timeout=3,
        )
        conn.close()
        print('database is ready')
        break
    except Exception as exc:
        if time.time() > deadline:
            print('database not ready, giving up:', exc)
            sys.exit(1)
        print('waiting for database...')
        time.sleep(2)
PY

python manage.py migrate --noinput
python manage.py init_books            # 预置角色分组（管理员/查询员/业务员）
python manage.py init_admin             # 首次安装创建管理员（已有用户则跳过）
python manage.py collectstatic --noinput

exec gunicorn ledger.wsgi:application --bind 0.0.0.0:8000 --workers 3 --threads 2 --timeout 120
