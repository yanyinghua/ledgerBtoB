# 运维备忘（ledger 往来账管理系统）

日常运维速查。详细部署步骤见同目录 [`DEPLOY.md`](DEPLOY.md)，本文档聚焦**已踩过的坑与常用流程**。

---

## 1. 环境与地址

| 项 | 值 |
|---|---|
| 代码仓库 | `git@github.com:yanyinghua/ledger.git`（SSH） |
| 云服务器 | SSH Host `ledger` → `124.223.184.190`，登录 `root`，应用目录 `/srv/ledger` |
| 运行用户 | `www`（宝塔面板），nginx 在 `/www/server/nginx`（非 `/etc/nginx`） |
| 站点 | https://ledger.yanyinghuaerp.top |
| 数据库 | 单文件 SQLite：`/srv/ledger/db.sqlite3` |
| 生产环境变量 | `/etc/ledger/env`（不进仓库） |

---

## 2. 鉴权（重点，已踩坑）

### 2.1 推送 GitHub → 用 SSH 密钥，禁用明文 token

本机 GitHub 专用密钥：`~/.ssh/id_ed25519_github`，在 `~/.ssh/config` 中以 `Host github.com` 配置。
公钥已登记到 GitHub **Settings → SSH and GPG keys**。

**两种明文 token 方式都不可用，切勿使用：**

- ❌ `git config` 写 `http.extraHeader=Authorization: Basic <base64(token)>`
  - token 明文落盘到 `~/.gitconfig`；
  - `http.extraHeader` 是**累加**的，旧 token 过期后 git 会把旧 token 与新 token **一起发出**，GitHub 直接返回 **401**。本机曾因此卡住推送。
  - 清除残留：`git config --global --unset "http.https://github.com.extraheader"`
- ❌ token 写进仓库 HTTPS URL（`https://<token>@github.com/...`）
  - GitHub 对 Git 操作返回 **403 / Password authentication is not supported**。

**验证 SSH 推送可用（无需任何 token）：**
```bash
ssh -T git@github.com            # 应返回 Hi yanyinghua! You've successfully authenticated...
git ls-remote --heads origin     # 能列出分支即正常
git push --dry-run origin master # 应显示 Everything up-to-date（或待推提交）
```

### 2.2 部署到云服务器 → 用另一把密钥（与 GitHub 隔离）

`~/.ssh/config` 中 `Host ledger` 使用 `~/.ssh/id_ed25519_ledger`，与 GitHub 密钥互不影响。
切勿把同一把密钥或 token 混用于两处。

---

## 3. 日常开发与上线流程

1. 本地改代码，跑测试：`python3 manage.py test books`
2. 提交：`git commit`
3. 推送（走 SSH，无需 token）：`git push`
4. 部署到服务器（rsync + 远程 [`deploy.sh`](deploy.sh)）：
   ```bash
   bash deploy/local-deploy.sh
   ```
   等价于 [`DEPLOY.md`](DEPLOY.md) 第 11 节，自动完成：同步代码 → 服务器 `sudo bash /srv/ledger/deploy/deploy.sh`。

---

## 4. Django 迁移

- 检查是否有待生成迁移：`python3 manage.py makemigrations --check --dry-run`
- 生成：`python3 manage.py makemigrations books`
- 应用：`python3 manage.py migrate`

> **重要**：只有改动 [`models.py`](../books/models.py) 才会产生迁移；**改行数据（如编辑某几条往来/保证金记录）永远不会生成迁移**。若 `makemigrations --check` 报待生成，说明是模型代码与最后一次迁移不一致，与业务数据无关。

---

## 5. 服务器部署细节（`deploy/deploy.sh` 执行顺序）

1. 载入并校验 `/etc/ledger/env`（`scripts/check_env.py` 门禁，不通过即中止）
2. SQLite **在线热备**（一致性快照，无需停服）→ `/var/backups/ledger/`
3. `pip install -r requirements.txt` + `gunicorn`
4. `python manage.py migrate --noinput`
5. `python manage.py collectstatic --noinput`
6. `python manage.py loaddata column_mappings`（映射配置，幂等）
7. 修正属主（`www` 可访问 staticfiles / db / media）
8. `systemctl restart ledger` + `nginx` reload
9. **部署日志落盘**：`deploy.sh` 用 FIFO + `tee -a` 把全部输出写到 `/var/log/ledger/deploy-<日期>.log`（与 access.log/error.log 同属 `www:www`）。
   ⚠ **chown 陷阱**：日志属主必须在 `EXIT` 陷阱、`wait "$TEE_PID"`（tee 关闭文件）**之后**再 `chown`；否则 tee 仍持有文件时 chown 不生效，日志会变成 `root:root`。

## 5.1 生产安全配置验证（每次部署后建议做一次）

```bash
# 站点可达（未登录应 302 跳 /signin/）
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://ledger.yanyinghuaerp.top/
# HTTP 应 301 跳 HTTPS
curl -s -o /dev/null -w "HTTP %{http_code} -> %{redirect_url}\n" http://ledger.yanyinghuaerp.top/
# HTTPS 响应头应含 HSTS
curl -s -I https://ledger.yanyinghuaerp.top/ | grep -i strict-transport
```
预期：`DEBUG=False`、`SECURE_SSL_REDIRECT=True`、`HSTS_SECONDS=31536000`（值见 `/etc/ledger/env`）。

---

## 6. CI（GitHub Actions）

见 [`DEPLOY.md`](DEPLOY.md) 第 14 节。`.github/workflows/check-env.yml` 在 push/PR 触发：先校验生产 Secrets（`check_env.py --strict-hsts`），再 `manage.py test books`。
**本地 SSH 密钥无法读取 Actions 结果** —— 需到 GitHub 网页 Actions 标签页查看，或临时给 `actions:read` 的 PAT。Secrets 只能在 GitHub 网页配置（Settings → Secrets），不在本地。

---

## 7. 操作日志（OperationLog）覆盖范围

`OperationLog` 是**定向审计日志**，仅在代码显式调用 `log_operation()` 时写入，**不自动抓取全部行为**。当前覆盖的关键写操作：

- 客户映射（CustomerSalesperson）新增 / 编辑 / 删除
- 业务员变更提醒：采纳 / 忽略（admin）
- 名称映射（NameMapping）新增 / 编辑 / 删除 + 应用全部
- 导入：上传成功 / 上传失败 / 删除 / 重跑
- 查询页标注 / 备注就地更新
- 承兑汇票期初余额：设置 / 更新
- 系统任务「校准现任」（`calibrate_handlers` 命令）

**不记录**：登录 / 登出（走独立 `LoginLog`）、纯查询 / 翻页、对账单 / 存款等只读页面。
如需更广覆盖，在对应写接口按上述模式补 `log_operation(...)` 即可，无需改模型。

### 7.1 反查命令 `trace_oplog`（按 ID 追溯操作日志与业务记录）

看到任意 `OperationLog.target_id`（如 `216863`），用此命令一键反查到具体操作日志 + 关联的业务记录实体：

```bash
# 按对象标识反查（最常用）：216863 指某条 MasterRecord 的主键
python3 manage.py trace_oplog 216863

# 按操作日志自身主键反查
python3 manage.py trace_oplog --log-id 1

# 限定对象类型，避免跨类型误匹配（如只查 MasterRecord）
python3 manage.py trace_oplog 216863 --type MasterRecord
```

**映射关系**：`target_type` 直接对应模型/数据表（`MasterRecord`→`books_masterrecord`、`CustomerSalesperson`、`NameMapping`、`SourceUpload`、`AcceptanceOpeningBalance`；`系统` 无具体行）。命令按 `target_type` 自动定位并打印关键字段。

**注意事项**：
- 批量接口（如"更新标注/备注"）的 `target_id` 会用 `、` 拼接多个主键（如 `216863、216864`），命令按**完整主键**精确匹配，不会把 `216863` 误配进 `1216863`。
- 若关联记录已被删除 / 重跑导入清除，会提示「找不到 pk」而非报错；`系统` 类型或无具体行时提示跳过。

服务器上以 `www` 用户运行：
```bash
ssh ledger 'cd /srv/ledger && sudo -u www /srv/ledger/venv/bin/python manage.py trace_oplog 216863'
```

### 7.2 在 Django admin 中点击定位（图形化）

若只是想快速从日志跳到对应记录，无需命令行：进入 **`/console-9a2k/books/operationlog/`**（操作日志列表），
`target_id` 列已渲染为**可点击链接**（列表与详情页均有），按 `target_type` 自动跳转到对应业务页面：

| 对象类型 | 点击跳转到 |
|---|---|
| `MasterRecord`（主表交易） | 发票穿透明细页 `/invoice/<pk>/` |
| `CustomerSalesperson`（客户映射） | 客户映射编辑页 `/data/customer/<pk>/edit/` |
| `NameMapping`（名称映射） | 名称映射编辑页 `/data/namemap/<pk>/edit/` |
| `SourceUpload`（导入批次） | 导入列表页 `/import/list/` |
| `AcceptanceOpeningBalance`（承兑期初） | 承兑报表页 `/acceptance/report/` |
| `系统` / 空标识 | 显示 `—`（无可点击目标） |

- 批量主键（如"更新标注/备注"多选）会用 `、` 逐条生成链接，每条都可点。
- 链接以新标签页（`target="_blank"`）打开，不离开日志页。

> 操作日志当前**仅在 Django admin 可见**，应用内没有独立页面。命令行 `trace_oplog` 与 admin 点击定位互为补充：前者适合脚本/无界面追溯，后者适合人工点选。

### 7.3 导出与按时间段清理

**查询时间段**：admin 列表页 `created_at` 筛选器自带「从 / 至」日期区间输入（Django 内置 `DateFieldListFilter`）；配合 `date_hierarchy`（按年/月/日下钻）与 `action`/`target_type` 筛选，可快速定位某时间段日志。

**导出 CSV（admin）**：列表页勾选记录 → 顶部「动作」选 **「导出选中记录为 CSV」** → 下载 `operation_log.csv`（带 UTF-8 BOM，Excel 直接打开不乱码）。

**按时间段清理 / 备份（`clean_oplog` 命令）**：admin 为防篡改禁止删记录，清理走运维命令，且**强制先导出 CSV 备份再删**。

```bash
# 导出某时间段备份并删除（删除前必备份）
python3 manage.py clean_oplog --before 2026-01-01
python3 manage.py clean_oplog --after 2025-01-01 --before 2026-01-01

# 只导出备份、不删除（适合纯“备份/导出记录”诉求，不影响线上）
python3 manage.py clean_oplog --before 2099-01-01 --export-only --output /tmp/oplog-backup.csv

# 仅预览待删除数量，不写文件也不删
python3 manage.py clean_oplog --before 2026-01-01 --dry-run
```

**专用导出命令 `export_oplog`（推荐用于纯备份/导出）**：按时间段导出 CSV，不删数据、不耦合删除语义。

```bash
# 导出 2026-01-01 之前（含）的全部操作日志
python3 manage.py export_oplog --before 2026-01-01

# 导出某时间段（含两端日期）
python3 manage.py export_oplog --after 2025-01-01 --before 2026-01-01

# 指定输出路径（默认写当前目录 oplog-export-<日期>.csv）
python3 manage.py export_oplog --before 2026-01-01 --output /tmp/oplog.csv
```

| 命令 | 用途 |
|---|---|
| `export_oplog` | **纯导出**：按 `--before`/`--after` 时间段导出 CSV，不影响线上 |
| `clean_oplog` | 清理：先导出备份再删除；`--export-only` 等价于只备份不删，`--dry-run` 只统计 |

两命令通用参数：

| 参数 | 作用 |
|---|---|
| `--before YYYY-MM-DD` | `created_at` 日期 ≤ 该日的记录（含） |
| `--after YYYY-MM-DD` | 可选，`created_at` 日期 ≥ 该日（含），用于区间 |
| `--output PATH` | 备份 CSV 路径 |
| `--export-only`（仅 clean_oplog） | 只导出不删除 |
| `--dry-run`（仅 clean_oplog） | 只统计数量，不落盘不删 |

> 服务器以 `www` 运行：`sudo -u www /srv/ledger/venv/bin/python manage.py export_oplog ...`（或 `clean_oplog ...`）

> 完整导出使用说明见仓库根 **《操作日志导出功能使用说明.md》**。

---

## 8. 数据库备份与恢复（SQLite）

> 不要用 `cp` 直接拷运行中的库（可能损坏）。用内置热备命令。

日常无需手工备份：crontab 每日 03:00 自动执行（`scripts/run_backup.sh`），
部署时也会各备一次。以下为手工操作方式。

```bash
cd /srv/ledger

# 热备（自动 integrity_check）+ 顺带清理旧备份
sudo venv/bin/python manage.py backup_db \
  --dest "/var/backups/ledger/db-$(date +%Y%m%d-%H%M%S).sqlite3" \
  --keep 20 --keep-days 14

# 只看会清理哪些，不实际删除
sudo venv/bin/python manage.py backup_db --keep 20 --dry-run

# 校验某份备份是否可恢复
sudo venv/bin/python manage.py backup_db --check /var/backups/ledger/db-xxx.sqlite3

# 恢复（会自动先对当前库做安全备份）
sudo venv/bin/python manage.py backup_db --restore /var/backups/ledger/db-xxx.sqlite3
sudo systemctl restart ledger   # 让 gunicorn 重新加载库文件
```

**备份保留策略**（三层并集，命中任一层即保留）：最近 `--keep` 份 ∪ 最近
`--keep-days` 天每天最新一份 ∪ 每月最新一份。稳态占用约 3 GB。

> ⚠ 两个容易踩的坑：
> 1. **文件名必须是 `db-<YYYYMMDD>-<HHMMSS>.sqlite3`**。写成 `db-$(date +%F).sqlite3`
>    不匹配清理正则，会被当作安全备份**永久保留**，备份目录重新无限增长。
> 2. **要用 `sudo` 以 root 执行**，`/var/backups/ledger` 属主是 root，
>    `sudo -u www` 会因无写权限失败。
>
> `pre-restore-*` 与 `db.pre-*`（破坏性操作前的安全备份）永不被自动清理。

**定时任务排查**（每日 03:00，见 DEPLOY.md「定时备份」）：
```bash
sudo crontab -l | grep run_backup                       # 确认任务存在
tail -30 /srv/ledger/logs/backup_$(date +%Y-%m).log     # 本月执行日志
cat /srv/ledger/logs/backup_failed.log                  # 仅失败时才有内容
sudo /srv/ledger/scripts/run_backup.sh                  # 立即手动触发一次
```
备份日志按月分文件（`backup_YYYY-MM.log`），失败会另写 `backup_failed.log`。

---

## 9. 安全 Checklist

- [ ] 不在 `~/.gitconfig` 写明文 token（尤其 `http.extraHeader`）
- [ ] 暴露/失效的 token 立即到 GitHub 后台 **Revoke**
- [ ] 服务器部署密钥与 GitHub 推送密钥分开
- [ ] `/etc/ledger/env` 权限 `600`、属主 `root`

**已知已暴露、待 revoke 的 token（曾出现在聊天/旧 gitconfig）：**
- 共 4 个 GitHub PAT 已暴露，明文已从本文档移除（避免公开仓库二次泄露）。
- 其中 1 个曾藏在全局 gitconfig 的 `http.extraHeader`；另 1 个为会话临时使用、切到 SSH 后已无需。
- 请到 **Settings → Developer settings → Personal access tokens** 逐一 Revoke，并从 gitconfig 中删除 `http.extraHeader`。

> GitHub 的令牌管理接口不接受用 PAT 调用，无法经 API 批量撤销，只能网页 **Settings → Developer settings → Personal access tokens** 手动 Revoke。

---

## 10. 常用命令速查

```bash
# 本地
python3 manage.py check --deploy        # Django 部署检查
python3 manage.py test books             # 跑测试
python3 manage.py makemigrations --check --dry-run

# 推送（SSH，无 token）
git push
git push --dry-run origin master

# 部署
bash deploy/local-deploy.sh              # 本机一键同步+部署

# 服务器（ssh root@ledger 后）
systemctl status ledger
systemctl restart ledger
journalctl -u ledger -f                  # 看 gunicorn 日志
```
