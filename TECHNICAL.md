# 客户往来账管理系统 — 技术文档

> 适用仓库：`my-project1`（单账套版，仓库 `github.com/yanyinghua/ledger`，生产 `/srv/ledger`）
> 多账套版在另一个项目 `~/projects/ledger-multi`（仓库 `yanyinghua/ledger-multi`），见文末「附：与多账套版的差异」。
> 本文中的行号基于当前 HEAD，可能随改动漂移，请以函数/类名检索为准。

---

## 1. 项目概览

**定位**：把来自税务局、银行、微信、承兑等多渠道的 Excel 原始数据，统一清洗成一张「往来主表」，
在此基础上做客户往来明细、账龄与催款、业务员业绩、保证金、票据包库存、发票穿透、对账函等查询与导出。

**技术栈**

| 层 | 选型 |
|---|---|
| Web 框架 | Django 4.2（`ledger` 项目 + `books` 应用） |
| 数据库 | SQLite（生产已可切 PostgreSQL，见 §10.1） |
| Excel | pandas + openpyxl（读）、openpyxl（导出写） |
| 前端 | Bootstrap 5 + 原生模板，响应式（手机可用） |
| 进程 | gunicorn（unix socket）+ nginx 反代 + systemd |
| 测试 | Django TestCase（SQLite 内存库） |

**代码规模**（不含 venv / migrations）

| 文件 | 行数 | 说明 |
|---|---|---|
| `books/tests.py` | 2605 | 34 个 TestCase / 180 个测试方法 |
| `books/views.py` | 2186 | 全部业务视图与导出 |
| `books/imports/engine.py` | 623 | 导入引擎 |
| `books/models.py` | 609 | 数据模型 |
| `books/imports/utils.py` | 310 | 清洗/归一/映射工具 |
| `books/admin.py` | 331 | 后台（列映射、算子、规则、映射库） |
| `books/imports/rules.py` / `operators.py` | 203 / 172 | 规则块 / 算子注册表 |

**数据规模**（本地库实测，供量级参考）：主表 69399 行、发票货物明细 237045 行、原始导入行 59236 行、
导入批次 134、客户-业务员映射 2025、资金明细业务员表 6059；`db.sqlite3` 约 162 MB。

---

## 2. 目录结构

```
my-project1/
├── manage.py
├── db.sqlite3                  # 本地 SQLite 库（生产在 /srv/ledger/db.sqlite3 或 PG）
├── requirements.txt
├── ledger/                     # Django 项目
│   ├── settings.py             # 数据库/安全/中间件/静态（§10.1）
│   ├── urls.py                 # 路由分组（§10.2）
│   └── wsgi.py
├── books/                      # 唯一业务应用
│   ├── models.py               # 数据模型（§4）
│   ├── views.py                # 业务视图（§6）
│   ├── forms.py                # 名称映射/客户映射/算子步骤/规则表单
│   ├── admin.py                # 后台：列映射、算子、规则、映射库、账套相关
│   ├── decorators.py           # 角色与行级权限（§7）
│   ├── middleware.py           # ForcePasswordChangeMiddleware
│   ├── context_processors.py   # user_role（模板取角色）
│   ├── salesperson.py          # 派生「最新业务员」与现任比对
│   ├── validators.py           # 密码复杂度
│   ├── imports/                # 导入引擎（§5）
│   │   ├── engine.py           # 主流程：读表→映射→规则→去重→写库
│   │   ├── utils.py            # 金额/日期/单位名/红冲/客户映射
│   │   ├── operators.py        # 算子（积木）注册表
│   │   └── rules.py            # 跨字段规则块注册表
│   ├── management/commands/    # 21 个管理命令（§9）
│   ├── templatetags/money.py   # cny 金额过滤
│   ├── templates/books/        # 23 个页面模板
│   ├── migrations/             # 24 个迁移（最新 0024_reconcileletter）
│   └── tests.py
├── deploy/                     # 部署脚本（§10.3）
│   ├── deploy.sh local-deploy.sh rsync-exclude.txt
│   ├── systemd/ledger.service  nginx/ledger.conf
│   ├── check-ci.sh cleanup-secrets.sh  logrotate/
│   └── DEPLOY.md OPS.md env.template
├── scripts/
│   ├── check_env.py            # 生产环境变量门禁
│   ├── run_backup.sh           # 定时热备包装（cron 用）
│   ├── run_incremental.sh watch_incremental.sh
├── exports/ backups/ logs/     # 运行期产物（已被 .gitignore 与 rsync-exclude 排除）
└── Dockerfile docker-compose.yml docker-entrypoint.sh   # 可选方案，生产未用
```

---

## 3. 快速上手

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt   # 本机无 venv，直接用系统 python3 也可
python3 manage.py migrate            # 单账套用 migrate（多账套版是 migrate_all）
python3 manage.py init_books         # 建「管理员/查询员/业务员」三个分组 + 预置列映射
python3 manage.py createsuperuser
python3 manage.py runserver 0.0.0.0:8000
```

**跑测试**（生产安全开关会把测试请求 301 到 HTTPS，必须关掉）：

```bash
DJANGO_SECURE_SSL_REDIRECT=False DJANGO_HSTS_SECONDS=0 python3 manage.py test books
python3 manage.py test books.tests.BackupPruneTest        # 单个类
```

**常用环境变量**：`DJANGO_SECRET_KEY`、`DJANGO_DEBUG`、`DJANGO_ALLOWED_HOSTS`、
`DJANGO_SECURE_SSL_REDIRECT`、`DJANGO_HSTS_SECONDS`、`DJANGO_DB_ENGINE`、
`WINSHARE_BASE`（共享盘源目录，默认 `/mnt/win_share`）。

---

## 4. 数据模型

### 4.1 实体关系

```
User ─1:1─ UserProfile(绑定业务员/强制改密)
  │
  ├──1:N─ SourceUpload(导入批次) ─1:N─ RawImportRow(原始行 JSON + 行哈希)
  │                                        └──1:N─ MasterRecord.raw_row  (穿透溯源)
  └──1:N─ OperationLog(业务操作日志) / LoginLog(登录审计)

MasterRecord ─1:N─ InvoiceLineItem (发票货物明细，按发票号挂接)
OpeningBalance        单位期初余额（往来）
AcceptanceOpeningBalance  承兑期初库存（单条总额）
FundReceiptHandler    资金明细业务员表（手工登记的银行收款经手人，独立表，不进主表）
NameMapping           原始名 + 税号 → 规范名
CustomerSalesperson   客户 → 现任业务员 + 账期
SalespersonChangeReminder  业务员变更提醒（导入派生 ≠ 现任，人工确认）
ReconcileLetter       对账函编号（year+seq 唯一）
ColumnMappingConfig ─1:N─ MappingStep ─N:1─ Operator        （列映射 + 算子管道）
LogicRule ─N:1─ LogicRuleType                                （跨字段规则块）
```

### 4.2 表清单

| 模型 | 作用 | 关键字段 / 约束 |
|---|---|---|
| `MasterRecord` | **核心主表**：所有渠道统一后的往来记录 | `source_type`、`source_upload`、`raw_row`、`unit_name`(idx)、`biz_type`、`year`(idx)、`period`(YYYYMM, idx)、`occur_date`(idx)、`category`、`debit`/`credit`(Decimal 18,2)、`tag`、`handler`(idx)、`remark`、`tax_no`、`invoice_number`、`package_no`(idx)、`blue_invoice_no`；`ordering=['occur_date','id']` |
| `InvoiceLineItem` | 发票货物明细行（来自「信息汇总表」sheet） | `source_upload`、`master_record`(可空=未挂上)、`invoice_number`(idx)、`goods_name`、`spec`、`unit`、`quantity`、`price`、`amount`、`tax_rate`、`tax`、`row_index` |
| `SourceUpload` | 导入批次 | `source_type`、`file_name`、`uploaded_by`、`status`(success/partial/failed/empty)、`row_count`、`mode`(full/incremental)、`note` |
| `RawImportRow` | 原始行暂存（重跑/穿透用） | `data`(JSON)、`row_hash`(sha256…实为 md5，idx) |
| `ColumnMappingConfig` | 可配置列映射 | `source_type` + `source_column` → `target_field`；`order` 小者优先取第一个非空；同字段多列=别名 |
| `Operator` / `MappingStep` | 算子注册表镜像 / 每列的算子管道 | `MappingStep(order)` 串联，`params` JSON |
| `LogicRuleType` / `LogicRule` | 规则块注册表镜像 / 按来源启用的行级规则 | `LogicRule(order, is_active)` |
| `OpeningBalance` | 单位期初（往来） | `unit_name` + `biz_type` + 借贷 |
| `AcceptanceOpeningBalance` | 承兑期初库存 | `as_of_date` + `amount`（单条） |
| `FundReceiptHandler` | 银行收款经手人手工表 | `unit_name`、`amount`、`handler`、`occur_date` |
| `NameMapping` | 名称归一 | `unique_together(raw_name, tax_no)` |
| `CustomerSalesperson` | 客户-业务员 | `unique_together(unit_name, salesperson)`；`is_current`、`manual_override`、`credit_term`(默认 30) |
| `SalespersonChangeReminder` | 变更提醒 | `unique_together(unit_name, suggested_salesperson, status)`；状态 待处理/已采纳/已忽略 |
| `LoginLog` | 登录审计 | 由 `user_logged_in/out/failed` 信号自动写 |
| `OperationLog` | 业务操作日志 | `log_operation()` 统一写入，含 IP |
| `ReconcileLetter` | 对账函 | `unique_together[(unit,year,m_start,m_end), (year,seq)]`；`letter_no` = `对账函字〔年〕第NNNN号` |
| `UserProfile` | 用户扩展 | `salesperson_name`（业务员行级过滤依据）、`must_change_password` |

### 4.3 关键枚举

- **SOURCE_TYPE**：`sales_invoice` 销项 / `purchase_invoice` 进项 / `bank` 银行 / `wechat` 微信 / `acceptance` 承兑 / `other` / `fund_handler` 资金明细业务员表
- **BIZ_TYPE**（主表业务类型）：发票 / 银行 / 承兑 / 微信 / 其他（由 `BIZ_TYPE_MAP` 从 source_type 映射）
- **TAG**：红冲 / 已红冲 / 作废 / 保证金 / 迪链 / 个人
- **EXCLUDED_TAGS = ['作废']**：只有「作废」在导入环节被过滤不入库；**红冲必须保留**，否则借贷余额、账期、回款统计不完整
- **TRANSFORM / TARGET_FIELD**：列映射的旧式单一转换与目标字段（单位名称、发生时间、借贷金额、类别、经手人、备注、税号、发票号码）

---

## 5. 导入引擎（`books/imports/`）

### 5.1 总体流程

```
Excel(xlsx, 多 sheet)
  → _read_sheet()      表头自动识别（第 0 行是标题则下移一行 + 回退校验）
  → _iter_rows()       跳过明细 sheet / 无金额 sheet / 表头泄漏行
  → RawImportRow       原始行 JSON 落库（bulk_create，2000 一批）
  → _build_record()
        extract_fields()  按 ColumnMappingConfig(order) 取首个非空
                          → MappingStep 算子管道 apply_operator()
        normalize_unit_name()  单位名归一（查 NameMapping）
        发票号：数电优先（数电发票号码 > 数电票号码 > 发票号码）
        → LogicRule(order) 逐条 apply_rule()  红冲/作废/借贷方向/类别/迪链/业务员映射
        作废行直接丢弃（不入库）
  → 去重 → MasterRecord.bulk_create
  → 发票类再跑 _import_detail_sheets()  解析「信息汇总表」建 InvoiceLineItem
  → 回写 SourceUpload(row_count / status / note)
```

### 5.2 三种导入方式

| 入口 | 函数 | 去重键 | 说明 |
|---|---|---|---|
| 全量 | `run_import()` :431 | **自然键** `_natural_key()` :416 = (来源, 日期, 单位, 借, 贷, 摘要, 发票号) | 跨文件格式也有效；重复行计入 `skipped` |
| 增量 | `run_import_incremental()` :524 | **原始行 md5** `_canon_hash()` :410 | 适合定期刷新同一份全量导出；已存在的行不重复写 |
| 重跑 | `rerun_import()` :601 | 无（删旧重建） | 按**当前**映射/规则重算该批次，用于校准后修复（不重新解析文件，用已存的 `RawImportRow`） |

> 全量与增量的去重维度不同：全量靠主表自然键（容忍格式变化），增量靠原始行哈希（精确但换列就全变新行）。
> 两者都在事务内执行（`@transaction.atomic`）。

### 5.3 列映射 + 算子（积木）

- `ColumnMappingConfig`：一条 =「某来源的某原始列 → 主表某字段」；同字段可配多条（别名），按 `order` 取第一个非空。
- `MappingStep`：给某条映射串接算子管道，按 `order` 依次执行，`params` 为 JSON 参数。
- 代码注册表是**权威来源**（`operators.py:OPERATORS`、`rules.py:RULE_TYPES`），
  `Operator` / `LogicRuleType` 两张表只是后台下拉的镜像，用 `init_operators` 命令同步。

**算子清单**（`operators.py`）

| key | 作用 | 参数 |
|---|---|---|
| `none` | 原样透传 | — |
| `trim` | 去首尾空白 | — |
| `fullwidth_brackets` | 半角括号转全角并归一全角标点 | — |
| `replace` | 文本替换 | find / replace |
| `regex_extract` | 正则提取 | pattern / group |
| `regex_salesperson` | 提取业务员姓名（去复核/经办/开票等前缀，取 2-4 字中文） | — |
| `to_date` | 多格式日期 → ISO | — |
| `to_decimal` | 去货币符号/千分位 → Decimal | — |
| `negate` / `abs` | 取负 / 绝对值 | — |
| `lookup_name` | 按原始名（可选同行税号）查 `NameMapping` 归一 | use_tax_no / tax_no_column |
| `constant` | 置常量 | val |

### 5.4 逻辑规则块（`rules.py`，作用于整行）

| key | 作用 | 主要参数 |
|---|---|---|
| `red_detect` | 红冲/作废识别：扫关键字列或「是否正数发票=否」；命中作废置 tag 并丢弃，命中红冲置 tag 并解析 `blue_invoice_no` | red_fields / red_keywords / negate_field / negate_value / void_fields / void_keywords |
| `direction_swap` | 微信等按类别把 credit 移到 debit，并归一为「支出/收入」 | outflow_values / inflow_values |
| `salesperson_map` | 维护客户-业务员映射（**只提醒不自动改**：`auto_current=False`） | require_category / min_year / source_label |
| `category_default` | 类别为空时按借贷方向给默认值 | debit_category / credit_category / default_category |
| `acceptance_direction` | 承兑按「操作类型」定方向：签收=贷方、申请=借方（迪链无该列则跳过） | op_field / recv_values / pay_values |
| `dilian_tag` | 类别列等于「迪链」时打 `tag='迪链'`，与普通承兑区分 | mark_field / mark_value / tag |

### 5.5 发票货物明细挂接（重点，历史事故区）

明细来自 xlsx 的「信息汇总表」sheet，判定：`is_goods_detail_sheet()` = 含「货物或应税劳务名称」**且**存在金额类列。

挂接规则（`_import_detail_sheets()` :119）：

1. 先删本 upload 既有明细 → 幂等重建；
2. 主表索引 `_iter_master_invoice_keys()` :96 同时收录 `invoice_number` 与原始行里的候选票号（短号/数电号互认）；
3. **本次 upload 优先**，其次回退同 `source_type` 的历史主表（增量导入常常不新增主表行，明细必须能挂到历史主表）；
4. 取号顺序主表与明细**必须一致**：`INVOICE_NO_CANDIDATES = ['数电发票号码','数电票号码','发票号码']`。

> **踩过的坑（勿回退）**：
> - 主表曾按映射取短号「发票号码」、明细取 20 位「数电发票号码」→ 两边永远对不上，2539 条明细挂不上。
> - 20 位数电票号超过 int64/浮点精度，该列只要有一个空单元格 pandas 就读成 float64（变 `2.6422e+19`，末几位失真）→
>   `_invoice_no_dtype()` :272 强制所有票号列按 `dtype=str` 读取；`_norm_invoice_no()` :56 把科学计数法视为无效。
> - 若某期体检「明细行 = 0」，多半是**源文件本身没有货物明细 sheet**（税务导出时没勾「信息汇总表」），不是代码 bug。

### 5.6 其他关键常量与工具

- `BIZ_TYPE_MAP` :29 — source_type → 主表 biz_type
- `DETAIL_MARKERS` :50 — 明细 sheet 特征列，用于跳过（避免明细重复计入主表）
- `SALES_HANDLER_FROM = 2024-09-01` :47 — 销项「复核人即业务员」仅此日期之后生效，更早的 handler 置空
- `BULK = 2000` — 批量写入阈值
- `utils.py`：`to_decimal` :23、`to_date` :45、`fullwidth_brackets` :76、`extract_salesperson` :84、
  `extract_reviewer_name` :106、`normalize_unit_name` :120、`apply_name_mapping_to_history` :136（批量改写历史单位名）、
  `detect_red` :210（从备注解析对应蓝字发票号）、`upsert_customer_salesperson` :228、`record_salesperson_reminder` :289

---

## 6. 业务功能地图（`books/views.py`）

| 功能 | 视图（行号） | 要点 |
|---|---|---|
| 认证 | `LoginView` 39、`PasswordChangeView` 44 | 改密后清 `must_change_password`，否则中间件反复跳转；`LoginView` 覆盖了 django 同名导入 |
| 首页 | `home` 61 | 计数走 `get_visible_queryset`（受角色过滤） |
| 数据导入 | `import_upload` 78、`import_list` 123、`import_delete` 132、`import_rerun` 144 | 个人流水打 `tag='个人'`；fund_handler 导入后自动 `match_bank_handlers`；成功/失败都记日志；删除级联删主表 |
| 名称归一 | `namemap_apply_all` 156 | `apply_name_mapping_to_history` 原地改写历史单位名（POST + 管理员） |
| **往来明细** | `statement` 180、`statement_export` 318 | 期初取 `OpeningBalance` 汇总；运行余额用 `Window(Sum(debit-credit))` :246；作废排除但红冲保留；分页 20/50/100/200，合计为跨页全量 |
| 催款 / 账龄 | `collection` 521、`aging` 596（共用 `_customer_aging` 467 FIFO 冲销、`_aging_buckets` 447 分 30/60/90 桶） | collection 用统一账期；aging 用**每客户自身** `credit_term` |
| 业务员业绩 | `sales_perf` 1005、`_perf_for_customer` 943 | 开票额取销项 debit 净额，红冲按负数扣减；`expand=1` 展开到客户 |
| 保证金 | `deposit` 1087 | `tag='保证金'` 过滤 + 汇总；导出含汇总/明细两个 sheet |
| 票据包/承兑 | `acceptance` 1292、`acceptance_detail` 1350、`acceptance_export` 1411 | 按单位 + 按 `package_no` 双汇总；穿透明细回读 `rec.raw_row.data`；承兑 vs 迪链靠 `tag='迪链'`；签收=贷方、申请=借方 |
| 承兑报表 | `acceptance_report` 1516、`_acceptance_report_rows` 1478 | 期初取 `AcceptanceOpeningBalance.objects.first()`，有期初则只统计 `as_of_date` 之后 |
| 综合查询 | `global_search` 1900、`_search_queryset` 1605 | Q 或查询 unit/remark/handler/发票号；金额区间是 **OR** 语义（`Q(debit__gte)\|Q(credit__gte)`） |
| 发票检索/穿透 | `invoice_search` 1703、`invoice_detail` 1823、`_matched_invoice_lines` 1684 | 货物名/规格走 `line_items__` + `distinct()`；穿透先按 `source_upload` 找明细，找不到回退同 `source_type` :1848 |
| **对账函** | `reconcile` 788、`reconcile_issue` 821、`_allocate_letter_no` 710、`_build_reconcile` 740 | 预览不占号，点「出具编号」才分配；并发用事务 + `IntegrityError` 重试 20 次；期初 = `OpeningBalance` + `period__lt` 主表；打印靠模板 `@media print` |
| 基础数据 CRUD | NameMapping 1970-2025、CustomerSalesperson 2028-2138 | 列表带 `SalespersonChangeReminder` 待处理角标；改客户映射逐字段 diff 后写日志 |
| 就地改标注 | `record_tag_update` 2142 | 解析 `tag_<pk>`/`remark_<pk>` 批量改主表，支持 AJAX 与表单 |

**已知冗余**：`PLACEHOLDERS` :419 与 `/r/<key>/` 路由已成死代码（里面的页面都已实现）；
`aging` 与 `collection` 重复了一套 as_of/账期解析；6 个导出函数各自重复 openpyxl 建表代码。

---

## 7. 权限与数据可见性

| 机制 | 位置 | 说明 |
|---|---|---|
| 角色 | `decorators.py:get_user_role` :52 | 按 **Django 分组**判断，优先级 管理员 > 查询员 > 业务员 |
| 管理员门禁 | `require_admin` :47、`AdminRequiredMixin` :14 | 仅登录 + 管理员；非管理员跳首页 |
| 行级过滤 | `visible_unit_names` :60、`get_visible_queryset` :75 | 业务员 = 本人 `profile.salesperson_name` 的客户集；未绑定业务员 → `qs.none()`；管理员/查询员不限制 |
| 强制改密 | `middleware.ForcePasswordChangeMiddleware` | `must_change_password=True` 时除改密/登出外全部跳转 |
| 模板取角色 | `context_processors.user_role` | 菜单渲染用 |

> 注意：部分导出视图（`aging_export`、`collection_export`、`global_search_export` 等）没有显式单位校验，
> 依赖 queryset 过滤；改动这些视图时不要忘记业务员越权风险。

---

## 8. 日志与审计

- **登录日志** `LoginLog`：由 `user_logged_in` / `user_logged_out` / `user_login_failed` 信号自动写（models.py:513-534）。
  IP 只信任 nginx 注入的 `X-Real-IP`：**不能**用 `X-Forwarded-For` 首段，因为 nginx 配的是
  `$proxy_add_x_forwarded_for`，客户端可伪造首段掩盖真实来源。
- **操作日志** `OperationLog`：统一用 `log_operation(user, action, target_type, target_id, detail, request=)`（models.py:591）。
  调用点：导入/删除/重跑/应用映射/承兑期初/名称映射增删改/客户映射增删改/改标注。
  ⚠ 现状：`target_id` 有的传 str 有的传 int；`target_type` 混用模型名与中文；`reconcile_issue` 分配编号**不记日志**。
- 维护命令：`clean_oplog`（按时间导出 CSV 并删除，需给 `--before` 或 `--after`）、
  `export_oplog`（只导出）、`trace_oplog`（按 `target_id`/`--log-id` 反查）。

---

## 9. 管理命令（`books/management/commands/`）

| 命令 | 用途 | 关键参数 | 写库 | 幂等 |
|---|---|---|---|---|
| `import_winshare` | 全量导入（按文件名去重），末尾自动 `match_bank_handlers` | `--include-overlap`、`--force`（删旧重导） | 是 | 否 |
| `import_incremental` | 增量导入（按行哈希去重） | `--src`（可重定向源目录） | 是 | 是 |
| `import_invoice_details` | 用原始 xlsx 重建 `InvoiceLineItem` | `--src`/`--source-type`/`--file`+`--upload` | 是 | 是（先删后建） |
| `relink_invoice_details` | 按发票号把孤儿明细补挂主表 | **默认预览**，`--apply` 才写 | 仅 apply | 是 |
| `invoice_health` | 体检各期主表↔明细挂接，排查「某期无明细」 | `--periods`(默认6)、`--source-type`、`--uploads`、`--no-lines`/`--unit` | 否 | — |
| `clean_dup_invoices` | 删「发票号为空」的重复主表 | 默认进项；`--apply`；`--backup` CSV | 仅 apply | 是 |
| `backup_db` | 在线热备 / 恢复 / 校验 | `--dest`/`--restore`/`--check`/`--backup-dir`、`--keep`(20)/`--keep-days`(14)、`--dry-run` | 否（写文件） | — |
| `export_master` | 主表导出 Excel（明细+汇总两 sheet） | `--output`/`--source`/`--year` | 否 | — |
| `match_bank_handlers` | 按 `FundReceiptHandler` 回填银行收款经手人（只填空） | `--dry-run`、`--show-unmatched N` | 是 | 是 |
| `backfill_sales_handlers` | 按备注「复核人」回填销项经手人/备注/发票号 | `--dry-run`、`--from-date`(默认 2024-09-01) | 是 | 是 |
| `init_books` | 建三个角色分组；`--seed-winshare-mappings` 灌样例列映射 | — | 是 | 是 |
| `init_operators` | 把代码里的算子/规则注册表同步进数据库 | — | 是 | 是 |
| `clean_oplog` / `export_oplog` / `trace_oplog` | 操作日志清理 / 导出 / 反查 | `--before`/`--after`/`--output`/`--export-only` | 部分是 | — |
| `reconcile_unmatched` | 导出三份未匹配对账 CSV | `--output` | 否 | — |
| 其它 | `clean_headers`、`clean_void_records`、`calibrate_handlers`、`migrate_fund_handler`、`rerun_invoice_imports` | 多带 `--dry-run` | — | — |

> 约定：**破坏性命令默认只预览**，必须 `--apply` 才真写；`RawImportRow` 作为留痕，清理命令一律不删。

---

## 10. 配置、部署与运维

### 10.1 `ledger/settings.py` 要点

| 项 | 位置 | 说明 |
|---|---|---|
| 数据库 | :98-121 | `DJANGO_DB_ENGINE` 默认 `sqlite3`（`db.sqlite3`）；PG 分支 :100-114 |
| **生产守卫** | :126-130 | `DEBUG=False` 且非 postgresql → `ImproperlyConfigured` |
| `DEBUG` | :29 | **默认 True**（未设 `DJANGO_DEBUG` 时）。后果：服务器上忘加载环境变量会静默连空 SQLite，报 `no such table` ⚠ |
| `SECRET_KEY` | :32-40 | 生产缺失直接 `RuntimeError` |
| `ALLOWED_HOSTS` | :43 | 默认 `*`；取 `DJANGO_ALLOWED_HOSTS`，逗号分隔 |
| 中间件 | :58-67 | 含 `ForcePasswordChangeMiddleware` :64 |
| 安全段 | :194-204 | `SECURE_SSL_REDIRECT`、Cookie Secure、HSTS、`SECURE_PROXY_SSL_HEADER` |
| 密码策略 | :136-159 | 最小长度 12 + `PasswordComplexityValidator` |
| 静态 | :177-178 | `STATIC_ROOT=staticfiles/`；无 `MEDIA_ROOT`、无 `LOGGING`（应用日志靠 gunicorn 与脚本落盘） |

### 10.2 路由分组（`ledger/urls.py`）

认证 :24-26｜导入 :28-31｜往来/账龄/对账 :32-40｜业绩/保证金 :41-44｜票据 :45-50｜检索/穿透 :51-55｜基础数据 CRUD :57-66｜占位 :67。

### 10.3 部署

**流水线**：`rsync -az --delete --exclude-from=deploy/rsync-exclude.txt ./ → /srv/ledger/`
→ 服务器 `sudo bash /srv/ledger/deploy/deploy.sh`。

`deploy.sh` 顺序：tee 日志到 `/var/log/ledger/deploy-<日期>.log` → source `/etc/ledger/env`
→ **`scripts/check_env.py` 门禁**（不合规即中止）→ `backup_db` 热备 → pip → `migrate`
→ `collectstatic` → `loaddata column_mappings` → PG `sqlsequencereset` → chown
→ `restart ledger` + nginx reload。

- systemd：`ledger.service`（`User=www-data`、`EnvironmentFile=/etc/ledger/env`、
  gunicorn `unix:/run/ledger/ledger.sock`、`--workers 2 --threads 2`）
- nginx：`ledger.conf`（80→301 HTTPS、`/static/` alias、`client_max_body_size 20m`、反代到 unix socket）
- CI：`.github/workflows/ci.yml` = validate(`check`) → test(`manage.py test books`) → deploy（**仅 master push**）
- 备份：`0 3 * * * /srv/ledger/scripts/run_backup.sh`（root；脚本内必须 source `/etc/ledger/env`，否则 cron 空环境会连错库）
- `rsync-exclude.txt` 已排除 `.git venv db.sqlite3* backups/ logs/ media/ staticfiles/ exports/ .env __pycache__`

**服务器手工执行命令前必须先加载环境**：`set -a; . /etc/ledger/env; set +a`，
否则 Django 回落到空 SQLite（这是本项目踩过两次的坑）。

---

## 11. 测试

`books/tests.py` 2605 行，**34 个 TestCase / 180 个 test**，按主题组织：

- 工具与清洗：`ToDateCompactTest`、`CleanHeadersTest`、`CleanVoidRecordsTest`、`CleanDupInvoicesTest`、`RelinkInvoiceDetailsTest`
- 导入与票据：`ImportRedInkTest`、`InvoiceGoodsDetailLinkTest`、`AcceptanceImportTest`、`BankImportDedupTest`、`AcceptanceSearchTest`、`AcceptanceReportTest`
- 业务员/权限：`HandlerAssignmentTest`、`CalibrateHandlersTest`、`ReminderOnlyTest`、`ConflictFlagTest`、`SalespersonEditCreditTermTest`、`PasswordPolicyTest`、`ForcePasswordChangeTest`
- 日志：`LoginLogTest`、`OperationLogTest`、`OperationLogLinkTest`、`OperationLogExportTest`
- 页面/导出：`MoneyFilterTest`、`StatementDefaultRangeTest`、`DrillDownTest`、`MarkAndInvoiceTest`、`SalesPerf*`、`ReconcileUnmatchedTest`、`GlobalSearchLinkTest`、`BackupDbCommandTest`、`BackupPruneTest`

---

## 12. 知识点与踩坑清单

**导入相关**
1. 数电票号 20 位超 float 精度 → 票号列必须 `dtype=str`；主表与明细取号顺序必须一致（数电优先）。
2. 源文件缺「信息汇总表」sheet → 该期永远没有货物明细，`relink` 救不了，只能重导带明细的文件。
3. 全量导入靠自然键去重，增量靠行哈希；**判重键含发票号**，历史票号异常时重导会翻倍插入（曾 455→910）。
4. 作废在导入环节就丢弃；红冲必须保留，否则余额/账期统计失真。
5. 表头泄漏防护：单位名列的值若等于某个表头列名，该行跳过。

**查询与业务**
6. 往来余额用 `Window(Sum(debit-credit))`，分页后每行余额仍正确；但与 `.count()` 混用需小心。
7. 金额区间查询是 **OR** 语义（借方或贷方任一落在区间），不是 AND。
8. 账龄用 FIFO 冲销；`collection` 用统一账期、`aging` 用每客户账期，两者口径不同。
9. 对账函编号并发分配靠 `unique_together(year, seq)` + 事务重试，预览不占号。

**部署运维**
10. `settings.py` 的 `DEBUG` 默认 True → 忘加载环境变量会静默连空 SQLite；服务器执行任何命令前先 `source /etc/ledger/env`。
11. `rsync --delete` 会删掉服务器上不在排除清单里的文件，新增数据目录必须同步补 `--exclude`。
12. 日志 IP 只信 `X-Real-IP`，不信 `X-Forwarded-For` 首段。
13. 本地跑测试必须关 `SECURE_SSL_REDIRECT` / HSTS，否则 GET 被 301。
14. 备份文件名必须匹配 `db-<YYYYMMDD>-<HHMMSS>.sqlite3`，`pre-restore-*` 永不清理。

---

## 附：与多账套版（ledger-multi）的差异

| 维度 | 本仓库（单账套） | ledger-multi |
|---|---|---|
| 账套 | 单库 `db.sqlite3` / 单个 PG 库 | 公共库 `public.sqlite3` + 每账套 `ledgers/<slug>.sqlite3`，`LedgerRouter` 硬隔离 |
| 未选账套 | — | 抛 `NoLedgerSelected`，渲染 `ledger_required.html` |
| 迁移命令 | `migrate` | **`migrate_all`**（公共库 + 全部账套库） |
| 部署目录 | `/srv/ledger` | `/srv/ledger-multi` |
| CI | validate + test + **自动部署** | validate + test（**不做自动部署**，防止覆盖单账套生产） |
| 域名 | `ledger.yanyinghuaerp.top` | `ledger-multi.yanyinghuaerp.top` |
