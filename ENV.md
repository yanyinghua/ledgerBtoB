# 生产环境变量清单

本系统（`ledger`）的密钥、调试开关与域名均通过**环境变量**注入，不写死在代码中，也不入库。
所有变量名以 `DJANGO_` 为前缀；未设置时使用「开发回退值」，仅适合本地测试，**切勿用于生产**。

## 变量清单

| 变量 | 必填（生产） | 默认值（开发回退） | 说明 |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | ✅ 必须 | 已内置一个强随机回退值 | 会话/令牌签名密钥。生产须设为**独立、长随机、不入库**的值。生成：`python3 -c "import secrets;print(secrets.token_urlsafe(60))"` |
| `DJANGO_DEBUG` | ❌ | `True` | 调试模式。`True`/`1` 为开；生产必须设为 `False`（或 `0`/`False`/`F`）。 |
| `DJANGO_ALLOWED_HOSTS` | 建议 | `*` | 允许访问的 Host，逗号分隔。生产改为具体域名，如 `example.com,www.example.com`。 |
| `DJANGO_HSTS_SECONDS` | ❌ | `0`（关闭） | HSTS 时长（秒）。设为 `31536000` 可启用严格传输安全；启用前须确认全站 HTTPS。 |
| `DJANGO_SECURE_SSL_REDIRECT` | ❌ | `True`（生产） | 是否将 HTTP 重定向到 HTTPS。生产默认开。 |

## 行为说明

- `DEBUG=False` 时自动启用以下安全设置（无需单独变量）：
  - `SECURE_SSL_REDIRECT`（受 `DJANGO_SECURE_SSL_REDIRECT` 控制）
  - `SESSION_COOKIE_SECURE = True`
  - `CSRF_COOKIE_SECURE = True`
  - `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')`（前置反向代理时识别 HTTPS）
  - 若 `DJANGO_HSTS_SECONDS > 0`：`SECURE_HSTS_SECONDS` / `SECURE_HSTS_INCLUDE_SUBDOMAINS` / `SECURE_HSTS_PRELOAD`
- `STATIC_ROOT` 固定为 `BASE_DIR/staticfiles`，生产执行 `collectstatic` 后由 nginx 提供静态文件。

## 示例：生产环境文件（.env）

```dotenv
# /etc/ledger/env —— 仅 root 可读 (chmod 600)
DJANGO_SECRET_KEY=把这里替换成 python3 -c "import secrets;print(secrets.token_urlsafe(60))" 的输出
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=example.com,www.example.com
DJANGO_HSTS_SECONDS=31536000
DJANGO_SECURE_SSL_REDIRECT=True
```

## 示例：systemd 服务注入

```ini
# /etc/systemd/system/ledger.service
[Service]
User=www-data
WorkingDirectory=/opt/ledger
EnvironmentFile=/etc/ledger/env
ExecStart=/opt/venv/bin/gunicorn ledger.wsgi:application --bind 127.0.0.1:8000
```

或直接在 `ExecStart` 前用 `Environment=` 逐条写入。

## 上线生产最小步骤

1. 准备上面的环境文件，植入独立 `DJANGO_SECRET_KEY`。
2. `DJANGO_DEBUG=False` 并设 `DJANGO_ALLOWED_HOSTS`。
3. 前置 nginx 提供 HTTPS（证书），并将静态文件指向 `collectstatic` 产物；反向代理设置 `X-Forwarded-Proto: https`。
4. 启动应用（gunicorn / uwsgi），由 systemd 管理。
5. `python3 manage.py migrate` 与 `python3 manage.py collectstatic` 在部署时执行。
6. 数据库当前为 SQLite，单机可用；真实并发生产建议迁移至 PostgreSQL/MySQL。

> 提示：本仓库 [`books/fixtures/column_mappings.json`](books/fixtures/column_mappings.json) 可用 `python3 manage.py loaddata column_mappings` 一键部署列映射配置。

## CI 环境变量校验

仓库内置校验脚本 [`scripts/check_env.py`](scripts/check_env.py)，可在 CI 或部署前置阶段运行，作为生产密钥配置门禁：

```bash
# 本地/CI 直接读环境变量
python3 scripts/check_env.py                 # 基础校验
python3 scripts/check_env.py --strict-hsts    # 额外要求 HSTS + SSL 重定向
python3 scripts/check_env.py --env-file .env # 从 .env 文件载入（不覆盖已有变量）
```

判定规则：
- `DJANGO_SECRET_KEY` 必须设置，且**不能等于** [`settings.py`](ledger/settings.py) 中的开发回退值；
- `DJANGO_DEBUG` 不能为 `True`；
- `DJANGO_ALLOWED_HOSTS` 不能为空或 `*`。

退出码：`0` 通过，`1` 致命错误（不应部署），`2` 使用方式错误。

[`.github/workflows/check-env.yml`](.github/workflows/check-env.yml) 已配置在 push/PR 时自动运行该校验（变量取自仓库 Secrets；
未配置则校验失败，从而阻断不合规范的部署）。GitLab CI 见仓库根 [`.gitlab-ci.yml`](.gitlab-ci.yml)（含 `env-check`
与 `django-test` 两个作业，变量在「项目设置 → CI/CD → Variables」中配置）。其他 CI（Drone 等）
使用时，只需加一步执行 `python3 scripts/check_env.py --strict-hsts` 即可。
