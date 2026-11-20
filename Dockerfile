FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=ledger.settings \
    TZ=Asia/Shanghai

WORKDIR /app

# util-linux 提供 findmnt（机器指纹取系统盘 UUID）；curl 供健康检查；tzdata 供 Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl util-linux tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . /app/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
