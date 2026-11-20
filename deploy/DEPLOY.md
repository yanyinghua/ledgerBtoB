# 腾讯云轻量服务器部署手册（ledger.yanyinghuaerp.top）

适用：Ubuntu 22.04 轻量服务器 + systemd + nginx + gunicorn + Let's Encrypt。
CI 实际为 **GitHub Actions**（`.github/workflows/ci.yml`，校验生产配置 + 跑测试），详见第 14 节；历史遗留的 GitLab CI（`.gitlab-ci.yml`）与下面实际部署相互独立，可保留也可删除。

> **宝塔/OpenCloudOS 服务器差异（本机实测 124.223.184.190）**
> 腾讯云轻量应用镜像常自带 **宝塔面板**：nginx 在 `/www/server/nginx`（非 `/etc/nginx`），
> vhost 目录为 `/www/server/panel/vhost/nginx/*.conf`，nginx/gunicorn 运行用户为 **`www`**（非 `www-data`），
> 且 `dnf` 在 `/etc/dnf/dnf.conf` 中 `exclude=...nginx...` 以防止覆盖宝塔组件（故**不要**用 dnf 装 nginx/certbot）。
> 适配要点：systemd 单元与 [`deploy.sh`](deploy.sh) 用 `RUN_USER=www`；vhost 写到宝塔 vhost 目录；HTTPS 证书用
> **宝塔面板 → 站点 → SSL → Let's Encrypt** 申请（更省事），之后把 `DJANGO_SECURE_SSL_REDIRECT` 改为 `True`。

---

## 0. 前置：DNS 解析（腾讯云控制台）
在 **DNS 解析 → yanyinghuaerp.top** 添加一条记录：
- 主机记录：`ledger`
- 记录类型：`A`
- 记录值：轻量服务器**公网 IP**
- TTL：默认

同时在 **轻量服务器 → 防火墙** 放行 `80`、`443`（以及你平时用的 `22`）。

---

## 1. 服务器初始化（一次性）
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.10-venv python3-pip nginx certbot python3-certbot-nginx \
                     git ufw

# 建应用目录与日志/运行目录
sudo mkdir -p /srv/ledger /var/log/ledger /var/www/letsencrypt
sudo chown -R www-data:www-data /var/log/ledger
```

## 2. 部署代码
把本仓库放到 `/srv/ledger`（git clone 或 scp）。假设项目根即 `/srv/ledger`：
```bash
sudo chown -R www-data:www-data /srv/ledger
```

## 3. 虚拟环境与依赖
```bash
sudo -u www-data python3 -m venv /srv/ledger/venv
sudo -u www-data /srv/ledger/venv/bin/pip install -r /srv/ledger/requirements.txt
sudo -u www-data /srv/ledger/venv/bin/pip install gunicorn
```

## 4. 生产环境变量
```bash
sudo mkdir -p /etc/ledger
sudo cp /srv/ledger/deploy/env.template /etc/ledger/env
sudo chmod 600 /etc/ledger/env && sudo chown root:root /etc/ledger/env
# 编辑 /etc/ledger/env，把 DJANGO_SECRET_KEY 换成：
sudo -u www-data /srv/ledger/venv/bin/python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits+'-_') for _ in range(64)))"
```

## 5. 数据库与静态文件
```bash
cd /srv/ledger
sudo -u www-data /srv/ledger/venv/bin/python3 manage.py migrate
sudo -u www-data /srv/ledger/venv/bin/python3 manage.py collectstatic --noinput
# 部署映射 fixture（类别/发票状态等）：
sudo -u www-data /srv/ledger/venv/bin/python3 manage.py loaddata column_mappings
# 首次运行可选：初始化经手人/部门等基础数据
# sudo -u www-data /srv/ledger/venv/bin/python3 manage.py init_books --seed-winshare-mappings
```

## 6. 部署前自检（门禁）
```bash
# 复用 CI 的校验脚本，确认环境变量合规
sudo -u www-data DJANGO_SECRET_KEY="$(grep DJANGO_SECRET_KEY /etc/ledger/env | cut -d= -f2)" \
  DJANGO_DEBUG=False DJANGO_ALLOWED_HOSTS=ledger.yanyinghuaerp.top \
  DJANGO_SECURE_SSL_REDIRECT=True DJANGO_HSTS_SECONDS=31536000 \
  /srv/ledger/venv/bin/python3 /srv/ledger/scripts/check_env.py --strict-hsts

# Django 部署检查（应为 0 issues）
sudo -u www-data /srv/ledger/venv/bin/python3 manage.py check --deploy
```

## 7. systemd 服务
```bash
sudo cp /srv/ledger/deploy/systemd/ledger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ledger
sudo systemctl status ledger   # 应为 active (running)
```

## 8. nginx + HTTPS
```bash
sudo cp /srv/ledger/deploy/nginx/ledger.conf /etc/nginx/sites-available/ledger
sudo ln -s /etc/nginx/sites-available/ledger /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 签发证书并自动改写 443 段（certbot 会读取上面的 server 块）
sudo certbot --nginx -d ledger.yanyinghuaerp.top
```

### 多网站共用服务器（可选）
若本机还跑着其他站点，建议再放一个兜底默认站点，避免用 IP 直接访问时泄露站点、
并集中处理 ACME 验证：
```bash
sudo cp /srv/ledger/deploy/nginx/default.conf /etc/nginx/sites-available/default
sudo ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/
```
注意：监听 `80` 的 `default_server` 全机只能有一个，确认其他站点的配置没有重复声明。
（certbot 给其他域名签发证书时，也会复用这里的 `/.well-known/acme-challenge/`。）

### ⚠ 宝塔面板 nginx：重启后不自启的坑（已在生产机修复）

现网 nginx 是宝塔装的，用的是 SysV 脚本 `/etc/rc.d/init.d/nginx`（systemd 通过
sysv-generator 包一层）。**该脚本原版有缺陷：重启后 nginx 起不来，80/443 全部无人监听。**

原因：判断"是否已在运行"时只做了

```sh
[ -f $PIDFILE ] && mPID=`cat $PIDFILE` && ps ax | grep "^${mPID}$"
```

它只确认"存在这个 PID 的进程"，**不确认这个进程是不是 nginx**。机器重启后
`/www/server/nginx/logs/nginx.pid` 里残留的旧 PID 常常正好被 `systemd`（pid 1）等占用，
脚本就误判 "nginx already running" 并 `exit 1`，nginx 从此不启动。
（启动日志表现为 `nginx (pid ) already running`，括号里 pid 为空。）

修复：加一个 `is_nginx_running()`，通过 `/proc/$mPID/exe` 反查可执行文件，
确认确实是 `$NGINX_BIN` 才算在跑；`start`/`stop`/`status`/`reload` 四处的
`ps ax | grep` 判断全部换成它。

```sh
is_nginx_running() {
    [ -f "$PIDFILE" ] || return 1
    mPID=$(cat "$PIDFILE" 2>/dev/null)
    case "$mPID" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ -d "/proc/$mPID" ] || return 1
    if [ "$(readlink -f "/proc/$mPID/exe" 2>/dev/null)" = "$NGINX_BIN" ]; then
        return 0
    fi
    # 二进制被升级替换过时 exe 会指向 (deleted)，回退比对 cmdline
    case "$(tr '\0' ' ' < "/proc/$mPID/cmdline" 2>/dev/null)" in
        *"$NGINX_BIN"*) return 0 ;;
    esac
    return 1
}
```

说明：
- 这是**系统文件，不在本仓库版本管理内**。重装宝塔 / 换机器后需要重新打这个补丁。
- 生产机原脚本备份在 `/root/nginx-init.bak-20260810-003503`。
- 改完 SysV 脚本后必须 `systemctl daemon-reload`，否则 sysv-generator 不会重新生成 unit，
  `systemctl start/stop` 行为会不一致（会看到 "unit file changed on disk" 告警）。
- 本机还跑着一个 Docker 里的 nginx（`nginx -g daemon off;`），`pidof nginx` 会把它也算进来，
  所以判断必须按可执行文件路径比对，不能只按进程名。

## 9. 验证
浏览器访问 `https://ledger.yanyinghuaerp.top` ，应自动跳转 HTTPS 且 `manage.py check --deploy` 无告警。

## 10. 一键更新
代码更新后，在服务器上以 root 执行（已包含在仓库 `deploy/deploy.sh`）：
```bash
cd /srv/ledger && sudo git pull
sudo bash /srv/ledger/deploy/deploy.sh
```
脚本会自动：备份 SQLite → 安装依赖 → migrate → collectstatic → loaddata 映射（幂等）→ 修正属主 → 重启 ledger + reload nginx。

> **部署日志落盘**：`deploy.sh` 用 FIFO 命名管道 + `tee -a` 把部署全过程输出写到 `/var/log/ledger/deploy-<日期>.log`，与 `access.log`/`error.log` 同属 `www:www`，方便统一排查。
> ⚠ **chown 陷阱**：日志属主必须在 `EXIT` 陷阱里、`wait "$TEE_PID"`（等 tee 关闭文件）**之后**再 `chown`；否则 tee 仍持有文件时 chown 不生效，日志会变成 `root:root`。（此坑已修，详见 `deploy.sh`。）

## 11. 本地手动部署（无需任何 Git 平台，推荐）
若不想用 GitLab/GitHub CI（例如 gitlab.com 在大陆访问不便），直接用本机一条命令完成同步+部署：
```bash
DEPLOY_HOST=服务器公网IP  DEPLOY_USER=root  bash deploy/local-deploy.sh
```
脚本做的事与 GitLab CI 的 deploy 阶段完全一致：把代码 rsync 到 `/srv/ledger`（排除 venv/db/静态/密钥），
再 ssh 执行 `sudo bash /srv/ledger/deploy/deploy.sh`。首次需先在服务器按第 1~7 步准备好环境与 `/etc/ledger/env`。

> `.gitlab-ci.yml` 可保留（将来换 gitlab.cn / 极狐GitLab 可直接复用），也可删除；手动部署不依赖它。

## 12. GitLab CI 自动部署（可选参考）
> 本仓库实际使用的是 GitHub Actions（见第 14 节）。本节与仓库内的 `.gitlab-ci.yml`
> 仅作参考；若日后迁移到 GitLab 可据此启用。

push 到 `master` 即自动把代码 rsync 到服务器并跑 [`deploy.sh`](deploy.sh)。生产密钥(DJANGO_*)不进 GitLab，而是在服务器端由 [`deploy.sh`](deploy.sh) 调用 [`check_env.py`](../scripts/check_env.py) 做真实校验。

### 12.1 服务器准备 SSH 登录密钥
在服务器（或本地）生成一对部署专用密钥：
```bash
ssh-keygen -t ed25519 -f gitlab_deploy_key -N ""
# 把公钥加入登录用户的 authorized_keys（以 root 为例）
cat gitlab_deploy_key.pub | sudo tee -a /root/.ssh/authorized_keys
# 确认 /root/.ssh/authorized_keys 权限正确
sudo chmod 600 /root/.ssh/authorized_keys
```
`gitlab_deploy_key`（私钥）稍后填进 GitLab；`gitlab_deploy_key.pub` 可丢弃。

### 12.2 GitLab CI/CD Variables
项目 → Settings → CI/CD → Variables（均建议 Masked）：
| Key | Value | 说明 |
|---|---|---|
| `SSH_PRIVATE_KEY` | `gitlab_deploy_key` 的私钥全文 | 换行保留 |
| `DEPLOY_HOST` | 服务器公网 IP 或域名 | 如 `1.2.3.4` |
| `DEPLOY_USER` | `root` | 或具 NOPASSWD sudo 的用户 |

> 注意：GitLab.com 共享 Runner 出口 IP 不固定，需放行服务器 **22 端口**（`0.0.0.0/0`）或 GitLab 官方 CI IP 段；建议仅允许密钥登录并安装 fail2ban 降低风险。

### 12.3 触发
- 推送到非 `master` 分支：仅跑 `validate` + `test`。
- 推送到 `master`：测试通过后自动 `rsync` 代码并执行 `sudo bash /srv/ledger/deploy/deploy.sh`。
- 若 `/etc/ledger/env` 不合规，[`deploy.sh`](deploy.sh) 会在服务器端中止部署（check_env 门禁），流水线失败报警。

---

## 13. 推送代码到 GitHub 的鉴权（推荐 SSH 密钥，禁用明文 token）

> 经验证，以下两条「明文 token」方式都不可取，请勿使用：
> - **`git config` 里写 `http.extraHeader=Authorization: Basic <base64(token)>`**：token 以明文落盘到 `~/.gitconfig`，且一旦该 token 过期/轮换，git 会把它和后续新 token **叠加发送**（extraHeader 是累加的），GitHub 直接 401。本机曾因此卡住推送。
> - **token 写进仓库 HTTPS URL**（`https://<token>@github.com/...`）：GitHub 对 Git 操作返回 `403 / Password authentication is not supported`，不可用。
> 正确做法是用 **SSH 密钥**，无明文、可长期、与服务器部署密钥互不影响。

### 13.1 生成本机 GitHub 专用密钥
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_github -N "" -C "yanyinghua@ledger-github"
```

### 13.2 写入 ~/.ssh/config（与服务器部署密钥隔离）
```ssh
# 推送到 GitHub（本机专用）
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new

# 部署到云服务器（另一把密钥，互不干扰）
Host ledger
    HostName 124.223.184.190
    User root
    IdentityFile ~/.ssh/id_ed25519_ledger
    StrictHostKeyChecking accept-new
```

### 13.3 把公钥登记到 GitHub
复制公钥内容：
```bash
cat ~/.ssh/id_ed25519_github.pub
```
到 GitHub 网页 → 头像 → **Settings → SSH and GPG keys → New SSH key**，粘贴保存（该接口需要账户的 `admin:public_key` 权限，无法用普通 `repo` token 通过 API 代填）。

### 13.4 远程地址切到 SSH 并验证
```bash
git remote set-url origin git@github.com:yanyinghua/ledger.git
ssh -T git@github.com          # 应返回 Hi yanyinghua! You've successfully authenticated...
git ls-remote --heads origin   # 无需任何 token 即可列出分支
```
此后日常推送直接 `git push`，不再依赖任何明文 token。若 `~/.gitconfig` 里仍残留 `http.https://github.com.extraheader`，先 `git config --global --unset "http.https://github.com.extraheader"` 清掉。

### 13.5 令牌（PAT）安全处理（仅用于 API，绝不进聊天/历史）

PAT 仅用于**读/写 GitHub API**（查 CI 结果 `check-ci.sh`、清理 Secret `cleanup-secrets.sh`），
**绝不**用于 git 推送（推送用 13 节的 SSH 密钥）。PAT 一旦泄露等同于账户凭据，务必按下面方式保管：

- **绝不**把 token 粘进聊天、issue、commit、脚本文件或命令行参数。
- **存文件、用环境变量**：把 PAT 单独存到 `~/.config/github-token`（权限 `600`），用时临时导出到环境变量，用完 `unset`：

  ```bash
  install -m 600 /dev/null ~/.config/github-token   # 首次建空文件（仅本人可读写）
  cat > ~/.config/github-token                      # 粘贴 PAT，Ctrl-D 保存
  export GITHUB_TOKEN="$(cat ~/.config/github-token)"   # 仅在当前 shell 生效
  ./deploy/check-ci.sh                              # 查询最近 CI 状态（脚本读该变量）
  unset GITHUB_TOKEN                                # 用完即清
  ```

- **不落命令行历史**：避免 `TOKEN=ghp_xxx ./x.sh` 这种写法（会被记入 shell history）。要清历史里的残留可用 `history -d <行号>` 或 `history -c`。
- **最小权限**：仅授予所需 scope（`actions:read` 看 CI、`repo` 删 Secret）。不要给 `admin`/全量权限。
- **怀疑泄露立即吊销**：GitHub → 头像 → **Settings → Developer settings → Personal access tokens → Tokens (classic)**，找到对应 token 点 **Delete** 确认（注意：经典 PAT 只能通过网页吊销，**没有**可用的自撤销 API，token 本身缺 `admin` 范围时 `curl` 删自己会 404）。
- **轮换习惯**：长期任务用短期有效（如 7 天）token，到期即换。

> 参考脚本：`deploy/check-ci.sh`（查最近 3 次 run + 最新 run 的 job 状态）、`deploy/cleanup-secrets.sh`（删多余的 `DJANGO_*` Secret）。两者都从环境变量 `GITHUB_TOKEN` 读令牌，不内置任何密钥。

---

## 14. GitHub Actions 自动部署（当前实际使用的 CI）

仓库根 `.github/workflows/ci.yml`，**push / PR 时自动跑**，三阶段：

1. **validate**：`manage.py check`（Django 系统检查，无需生产密钥）。
2. **test**：`pip install -r requirements.txt` 后 `manage.py test books`（当前 169 个测试）。
3. **deploy**：仅当 **push 到 `master` 且前两阶段通过** 时，用 SSH 私钥 `rsync` 同步代码到
   `/srv/ledger/`，再 `ssh` 执行服务器上的 `deploy.sh`（含 migrate / collectstatic / 备份 / 重启）。
   PR 事件不触发部署。

> 生产密钥（DJANGO_*）**不放在 GitHub**，只存在服务器 `/etc/ledger/env`；部署时由 `deploy.sh`
> 在服务器端跑 `check_env.py` 做真实校验（门禁）。CI 侧无需、也不应持有生产密钥。

### 14.1 必须在 GitHub 网页配置的 Secrets
仓库 → **Settings → Secrets and variables → Actions**，逐项添加：

| Name | 示例值 | 说明 |
|---|---|---|
| `SSH_PRIVATE_KEY` | 部署私钥全文 | 对应服务器 root 的 `/root/.ssh/id_ed25519_gha_deploy`；公钥已加入 `authorized_keys` |
| `DEPLOY_HOST` | `124.223.184.190` | 服务器公网 IP（或 `ledger.yanyinghuaerp.top`） |
| `DEPLOY_USER` | `root` | 登录用户，需能无密码 sudo 或直接 root 登录 |

> ⚠ **`DEPLOY_HOST` 必须是服务器真实公网 IP 或域名，不能写本机 SSH 别名 `ledger`**。
> GitHub runner 上没有该别名，`ssh-keyscan`/`ssh` 会报 `Could not resolve hostname ledger`
> 并直接失败（连接根本不会到达服务器，服务端 auth 日志也无任何记录）。排查部署连不上时，
> 先在服务器 `grep 'github-actions' /var/log/secure` 确认 runner 是否曾连上来——若无记录，
> 几乎都是 `DEPLOY_HOST`/密钥 Secrets 配置问题，而非服务器侧。
>
> 部署私钥由 `ssh-keygen -t ed25519` 在服务器生成，私钥仅在创建时短暂展示，请立即存入
> GitHub Secrets；若怀疑泄露，在服务器删掉该密钥对并重新生成、更新 Secrets 即可。

### 14.2 清理遗留的 `DJANGO_*` Secrets（建议执行）

GitHub Secrets 页面里若还挂着 `DJANGO_SECRET_KEY`、`DJANGO_DEBUG`、`DJANGO_HSTS_SECONDS`、
`DJANGO_ALLOWED_HOSTS`、`DJANGO_SECURE_SSL_REDIRECT` 这五个，**它们当前未被 `ci.yml` 使用**
（生产密钥只存服务器 `/etc/ledger/env`，见 14 节开头），留着既无作用又多一个泄露面，建议删掉。

清理方式二选一：

- **网页手动删**：仓库 → Settings → Secrets and variables → Actions，逐条点 `DJANGO_*` 的
  `Delete`，确认。删完应只剩 `SSH_PRIVATE_KEY` / `DEPLOY_HOST` / `DEPLOY_USER`。
- **脚本批量删**：`deploy/cleanup-secrets.sh` 循环调 GitHub REST API 删除这五个密钥，
  需要带 `repo` 权限的 Personal Access Token（删 Secret 走 API，只能用 token，SSH 密钥管不了）：

  ```bash
  # 方式一：环境变量传入 token
  GITHUB_TOKEN=ghp_xxx ./deploy/cleanup-secrets.sh
  # 方式二：交互式输入（token 不落盘）
  ./deploy/cleanup-secrets.sh
  ```

  > ⚠ 若脚本报 `401` / `403`，是 PAT 权限不足（需 `repo` 范围），不是脚本问题。
  > 注意：本机推送用的是 SSH 密钥（见第 13 节），**SSH 不能用于删 Secret**，此处必须单独提供 PAT。

### 14.3 测试 step 必须用「宽松配置」（已踩坑）
测试 step 的 env 必须设为 `DJANGO_DEBUG=True`、`DJANGO_SECURE_SSL_REDIRECT=False`、`DJANGO_HSTS_SECONDS=0`。
否则 `SECURE_SSL_REDIRECT=True`（settings 默认值）会在 `DEBUG=False` 时把测试客户端的 HTTP 请求 **301 重定向到 HTTPS**，导致所有期望 200 的用例失败（曾一次出现 77 failures + 4 errors）。`DEBUG=True` 时 `SECRET_KEY` 自动生成，**不依赖 Secrets**。

### 14.4 本地如何查看 CI 结果
GitHub Actions 的结果读取需要令牌（PAT 或 `gh` 登录）。本机只配了 SSH 推送密钥，**SSH 不能读取 Actions 结果**；私有仓库未认证 API 返回 404。查看请直接到 GitHub 网页 **Actions** 标签页，或临时提供具备 `actions:read` 的 PAT。

---

## 备注
- **数据库引擎**：生产已于 2026-08-10 迁移到 PostgreSQL（见下方「迁移到 PostgreSQL」）。代码是双引擎的，`DJANGO_DB_ENGINE` 未设置时回落 SQLite，本地开发无需装 PG。
- **HSTS 谨慎**：`DJANGO_HSTS_SECONDS=31536000` 一旦生效，浏览器会强制 HTTPS 很长一段时间；确认证书续期正常后再开启，否则证书过期将导致无法访问。

---

## 迁移到 PostgreSQL

生产库已于 2026-08-10 从 SQLite 迁到 PostgreSQL 15。

### 为什么要迁
每次 `systemctl restart ledger` 之后，第一个 `POST /login/` 有概率返回 500。根因是
SQLite 单写者锁：gunicorn 冷启动时多个 worker 同时写 session/登录日志，撞上
`database is locked`。加大 `timeout` 只能降低概率，不能根治。PostgreSQL 支持并发写，
配合 `CONN_HEALTH_CHECKS=True` 后彻底消失（连续 5 次「重启 + 立即 POST」全部 200）。

### 引擎切换
`settings.py` 按 `DJANGO_DB_ENGINE` 选择引擎，**未设置时回落 `sqlite3`**。
这个回落很方便（本地开发不用装 PG），但也有个坑：任何拿不到 `/etc/ledger/env`
的执行路径都会静默连到那个早已停用的 SQLite 文件，而且**不会报错**。
systemd 靠 `EnvironmentFile=` 注入，cron 则必须自己 `source`——
`scripts/run_backup.sh` 已按此修正。

```bash
# 切到 PostgreSQL（/etc/ledger/env）
DJANGO_DB_ENGINE=postgresql
DJANGO_DB_NAME=ledger
DJANGO_DB_USER=ledger
DJANGO_DB_PASSWORD=<强密码>
DJANGO_DB_HOST=127.0.0.1
DJANGO_DB_PORT=5432
DJANGO_DB_CONN_MAX_AGE=60
```

> `EnvironmentFile` 只在**启动服务时**读取。改完 env 必须 `systemctl restart ledger`，
> 否则正在跑的进程仍用旧引擎——改了配置却毫无变化，多半就是这个原因。

### 数据搬迁步骤（已执行，留档备查）
```bash
# 1) 建库建角色，pg_hba.conf 中 127.0.0.1/32 与 ::1/128 设为 scram-sha-256
# 2) 空库建表结构
DJANGO_DB_ENGINE=postgresql python manage.py migrate --noinput
# 3) 从 SQLite 导出。用 jsonl（Django 4.2+）而非 json：
#    loaddata 读 json 会把整个文件读进内存，110 MB 的库能把 2 GB 小机器拖进 OOM；
#    jsonl 逐行流式，实测常驻内存约 100 MB。
DJANGO_DB_ENGINE=sqlite3 python manage.py dumpdata --natural-foreign --format jsonl \
  --exclude contenttypes --exclude auth.permission --exclude sessions \
  --exclude admin.logentry --exclude books.columnmappingconfig \
  --exclude books.userprofile -o /tmp/ledger_migrate.jsonl
# 4) migrate 已经播种了 logicrule/logicruletype/mappingstep/operator，
#    与 fixture 的显式 pk 冲突。生产库里这几张表是被改过的（如 logicrule 14 条 vs 种子 4 条），
#    所以要清空后以 fixture 为准，不能反过来跳过 fixture。
psql -c 'DELETE FROM books_logicrule; DELETE FROM books_logicruletype;
         DELETE FROM books_mappingstep; DELETE FROM books_operator;'
# 5) 导入 + 对齐序列（见下节，必做）
DJANGO_DB_ENGINE=postgresql python manage.py loaddata /tmp/ledger_migrate.jsonl
python manage.py sqlsequencereset books auth admin sessions | python manage.py dbshell
```

`books.userprofile` 必须排除：`post_save` 信号会在建 User 时自动补一条 profile，
和 fixture 里的显式行撞 `user_id` 唯一约束。让信号去建空 profile，再按 username
把 phone / salesperson_name / must_change_password 覆盖过去即可。

### ⚠ PostgreSQL 的自增序列必须手动对齐
PG 的序列只在走 `DEFAULT nextval()` 时前进。`loaddata` 和 `0016_seed_acceptance_config`
这类种子迁移都是**显式 pk 写入**，序列不会动，于是停在旧值；等用户下次新建记录，
拿到的 id 早被占用，直接 duplicate key 报错。SQLite 用 `max(rowid)+1`，天然没这问题，
所以这个坑只在迁到 PG 后才会暴露，而且是**延迟发作**的。

`deploy/deploy.sh` 已在每次 `loaddata` 后自动执行对齐。手动检查：

```bash
# seq_last 应当等于 max(id)
psql -U ledger -d ledger -tA -c \
  "SELECT 'max='||(SELECT max(id) FROM books_columnmappingconfig)
       ||' seq='||(SELECT last_value FROM books_columnmappingconfig_id_seq);"
```

### 回滚
SQLite 原库保留在 `/srv/ledger/db.sqlite3`（另有 `db.sqlite3.bak`）。真要回退：

```bash
sudo sed -i 's/^DJANGO_DB_ENGINE=.*/DJANGO_DB_ENGINE=sqlite3/' /etc/ledger/env
sudo systemctl restart ledger
```

注意迁移后写入的新数据只在 PG 里，回滚等于丢掉这部分。确认 PG 稳定运行一段时间后，
再考虑清理这两个 SQLite 文件。

---

## 备份与恢复

> 迁到 PostgreSQL 后，`backup_db` 会自动改用 `pg_dump -Fc` 产出 `db-<时间戳>.dump`
> （约 10 MB，SQLite 时代是 112 MB 的 `.sqlite3`）。命令用法不变，引擎由
> `DJANGO_DB_ENGINE` 决定，下面的 SQLite 说明仅适用于本地开发或回滚场景。
>
> 校验恢复链路（不碰生产库）：
> ```bash
> createdb ledger_restore_test
> pg_restore -d ledger_restore_test --no-owner --no-privileges /var/backups/ledger/db-xxx.dump
> # 比对行数与金额合计后 dropdb ledger_restore_test
> ```

### SQLite（本地开发 / 回滚场景）

数据库为单文件 SQLite（部署路径 `/srv/ledger/db.sqlite3`；Docker 部署下为绑定挂载 `./db_data`）。

### ⚠ 不要用 `cp` 直接拷运行中的库
gunicorn 可能正在写入，**朴素 `cp` 可能得到损坏副本**。请改用内置的在线热备命令（基于 SQLite 在线备份 API，读取一致性快照，**无需停服务**）：

```bash
cd /srv/ledger
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db
# 默认备份到 backups/<时间戳>.sqlite3，并自动做 PRAGMA integrity_check
# 指定路径：
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db --dest /var/backups/ledger/db-$(date +%F).sqlite3
# 仅校验已有备份是否完整：
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db --check /var/backups/ledger/db-xxx.sqlite3
```

> 一键部署 [`deploy.sh`](deploy.sh) 的步骤 2 已改为调用上述命令，因此每次更新都会自动做一致性热备。

### 恢复
```bash
cd /srv/ledger
# 恢复前会自动对当前库做一份安全备份（backups/pre-restore-<时间戳>.sqlite3），便于回滚
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db --restore /var/backups/ledger/db-xxx.sqlite3
sudo systemctl restart ledger   # 让 gunicorn 重新加载库文件
```
> 若坚持用 `cp` 恢复/备份，务必先 `systemctl stop ledger` 停止写入，操作完再 `systemctl start ledger`。

### 定时备份（crontab）
每日 03:00 自动热备并按分级策略清理旧备份。用 root 的 crontab：

```bash
sudo crontab -e
```
加入一行：
```cron
# 每日 03:00 数据库热备 + 清理旧备份（日志见 /srv/ledger/logs/backup_YYYY-MM.log）
0 3 * * * /srv/ledger/scripts/run_backup.sh
```

验证：
```bash
sudo crontab -l | grep run_backup            # 确认已写入
sudo /srv/ledger/scripts/run_backup.sh       # 立即手动跑一次
tail -20 /srv/ledger/logs/backup_$(date +%Y-%m).log
```

保留份数与天数可在 crontab 行内用环境变量覆盖，例如
`0 3 * * * BACKUP_KEEP=30 BACKUP_KEEP_DAYS=30 /srv/ledger/scripts/run_backup.sh`。

> 为什么包装成脚本而不是把 `manage.py` 直接写进 crontab：crontab 中 `%`
> 是特殊字符**必须转义成 `\%`**，`$(date +%Y%m%d-%H%M%S)` 漏转义会导致命令
> 被静默截断；时间戳在脚本里生成可完全绕开此坑。另外 cron 的输出默认发本地
> 邮件（本机无 MTA 等于丢弃），脚本落盘到 `logs/` 才能事后排查。

> ⚠ **备份文件名必须是 `db-<YYYYMMDD>-<HHMMSS>.sqlite3`**。这是 `backup_db`
> 识别「例行备份」的唯一格式（正则全串锚定）。若写成 `db-$(date +%F).sqlite3`
> 即 `db-2026-08-04.sqlite3`，会被当作**安全备份永久保留、永不清理**，
> 备份目录将重新无限增长（此前曾累积到 7.4 GB）。
>
> ⚠ 备份目录 `/var/backups/ledger` 属主是 root，任务需**以 root 运行**；
> 写成 `sudo -u www` 会因无写权限而失败。

同时建议备份配置文件 `/etc/ledger/env`（不含业务数据，但含运行所需环境变量）。

### 包装脚本日志轮转（logrotate）
`/srv/ledger/logs/` 下的备份/增量导入日志（`backup_*.log`、`incremental_*.log`）
由 `scripts/run_backup.sh` / `scripts/run_incremental.sh` 按月分文件追加，长期会缓慢
增长，交给 logrotate 按月压缩、保留 12 份（约一年）。配置文件随仓库版本化：

```bash
sudo cp /srv/ledger/deploy/logrotate/ledger-app /etc/logrotate.d/ledger-app
sudo logrotate -d /etc/logrotate.d/ledger-app   # 校验语法（dry-run，不实际轮转）

sudo logrotate -fv /etc/logrotate.d/ledger-app  # 首次手动强制轮转一次验证效果
ls -l /srv/ledger/logs/                        # 原文件应清空、生成 .1.gz 归档
/srv/ledger/scripts/run_backup.sh              # 再跑一次，确认能正常追加到清空后的原文件
tail -5 /srv/ledger/logs/backup_$(date +%Y-%m).log
```

后续由系统的 `/etc/cron.daily/logrotate` 自动执行，无需另设定时任务。

> ⚠ **配置命名必须是 `ledger-app`，不是 `ledger`**。`/etc/logrotate.d/ledger` 已被
> gunicorn 的 `/var/log/ledger/{access,error,deploy}.log` 轮转占用（带 `postrotate
> USR1 ledger.service`），同名会覆盖它。
>
> ⚠ 本配置用 **`copytruncate` + `su root root`** 而非 `create`：
> `logs/` 属主 `www:www` 且组可写（www 要写增量日志），logrotate 3.18+ 对组可写目录
> 默认拒绝轮转，须显式 `su`。且目录下两类日志属主不同（备份 root 写、增量 www 写），
> `copytruncate` 原地清空保留原文件属主/inode，脚本下一轮仍按文件名正常追加；`create`
> 会把属主统一改成配置指定的用户，破坏 www 侧日志的权限预期。

### Docker 部署
DB 文件在宿主机的绑定挂载 `./db_data`（docker-compose 中 `./db_data:/app/db.sqlite3`）。可在容器内执行上述 `manage.py backup_db`，或先 `docker compose stop web` 再拷贝 `./db_data`，完成后 `docker compose up -d`。
