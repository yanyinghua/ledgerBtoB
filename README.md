# 往来账管理系统（MVP）

基于 Web 的往来账管理系统，支持多来源 Excel（发票/银行/承兑/微信等）批量导入、字段统一转换成主表、客户往来明细查询与 Excel 导出。响应式设计，手机浏览器可用。

技术栈：Django 4.2 + SQLite + Bootstrap 5 + pandas/openpyxl

---

## 一、环境要求

- Python 3.10+
- 依赖见 [`requirements.txt`](requirements.txt)（Django、pandas、openpyxl）

安装依赖：

```bash
pip install -r requirements.txt
```

> 若使用系统 Python 且提示 "externally managed"，请加 `--user` 或自行创建虚拟环境。

---

## 二、初始化与启动

```bash
# 1. 建表
python3 manage.py migrate

# 2. 预置列映射配置 + 角色分组（管理员/查询员/业务员），可重复执行
python3 manage.py init_books

# 3. 创建管理员账号（按提示输入用户名、密码、邮箱）
python3 manage.py createsuperuser

# 4. 启动（0.0.0.0 便于手机/局域网访问）
python3 manage.py runserver 0.0.0.0:8000
```

访问：

- 系统首页：http://<服务器IP>:8000/
- 后台管理：http://<服务器IP>:8000/console-9a2k/

手机访问：同一局域网下，手机浏览器打开 `http://<电脑IP>:8000/` 即可，页面已做响应式适配。

---

## 三、角色与权限

| 角色 | 权限 |
| --- | --- |
| 管理员 | 全部权限：导入、编辑、删除、导出、用户管理、后台配置 |
| 查询员 | 仅查询/导出，**不能编辑/导入/删除** |
| 业务员 | 仅查询/导出，且**只能看自己名下的客户**（按「客户-业务员映射」过滤） |

- 账号创建/分组在 Django 后台（`/console-9a2k/auth/user/` 与 `Groups`）完成。
- 业务员账号需在「用户档案」中绑定 **业务员姓名**（`salesperson_name`），系统据此过滤其可见客户。
- 修改密码：登录后点导航「修改密码」（当前密码校验；手机验证码改密为后续功能）。

演示账号（已随初始化脚本创建，可删改）：

- `admin` / `admin123`（管理员）
- `viewer` / `viewer123`（查询员）
- `sales1` / `sales123`（业务员，绑定业务员「张三」）

---

## 四、数据导入流程

1. 管理员进入「导入数据」，选择**来源类型**（销项发票/进项发票/银行流水/微信/承兑汇票/其他），上传 `.xlsx` 文件。
2. 引擎按**当前激活的列映射**把原始表头映射到主表字段，并执行规范化，生成主表记录。
3. 导入完成后可在「导入记录」查看结果；支持**删除**与**重跑**（按最新映射重建）。

### 规范化规则（对应需求说明）

- **括号全角**：单位名称中的 `()` 自动转为 `（）`。
- **名称归一**：发票有税号时优先按税号、否则按原始名称，从「名称映射库」取规范名；查不到则保留原值，待人工补全映射库。
- **业务员提取**：销项发票从「复核人」、收入流水从「经手人」用正则提取业务员，并写入「客户-业务员映射」（以最新发生日期者为现任业务员）。
- **红冲识别**：发票备注含「红冲/红字/负数/蓝字」时标记为「红冲」，并尝试解析备注中的蓝字数电发票号码。
- **借贷方向**：销项/进项取价税合计入借/贷；银行取借/贷金额；承兑取申请/签收金额；微信按收/支方向。

---

## 五、拿到真实 Excel 后如何校准列映射（重要）

系统采用「**算子库 + 后台选块填参**」的可组合逻辑引擎：列映射与基础数据由你在后台手工维护，
转换与跨字段逻辑则由可复用的**算子（积木）**与**规则块（积木）**拼装而成，不再写死在代码里。

### 1. 初始化积木与默认规则

```bash
python3 manage.py init_operators      # 播种算子清单、规则块清单、各来源默认逻辑规则（红冲/借贷互换/业务员映射/类别默认）
python3 manage.py init_books          # 仅预置角色分组（管理员/查询员/业务员）
```

`init_operators` 会把代码中的算子/规则注册表同步到后台（`Operator` / `LogicRuleType` 仅作清单镜像，不可改）。

### 2. 配置列映射（手工）

进入后台 `/console-9a2k/books/columnmappingconfig/`，为每种来源类型新增映射行（`原始表头` → `映射到主表字段`），
并通过其下方的 **映射步骤（MappingStep）** 选算子、填参数，串联成该列的转换管道。可用算子包括：
`原样 / 去空白 / 半角括号转全角 / 文本替换 / 正则提取 / 提取业务员姓名 / 日期解析 / 金额解析 / 取负 / 绝对值 / 名称查表归一 / 置常量`。
例如工行流水可配：`对方户名 → 单位名称（半角括号转全角）`、`借方发生额 → 借方金额（金额解析）`、`交易时间 → 发生时间（日期解析）`。

> 也可一键载入依据 `/mnt/win_share` 实表头准备的样例映射：
> `python3 manage.py init_books --seed-winshare-mappings`（默认不执行；载入后可在后台增删改）。

### 3. 配置逻辑规则（跨字段，积木式）

进入后台 `/admin/books/logicrule/`，按来源类型启用/排序规则块并填参，例如：
- `红冲/作废识别`：扫描备注/标志列关键字，或「是否正数发票=否」标识红字票。
- `借贷方向互换`：微信等收/支方向调整。
- `客户-业务员映射`：按业务员字段维护映射，可用 `min_year` 限定「自某年起」（如销项发票 2025 年起复核人=业务员）。
- `默认类别`：当类别为空时按借贷方向赋值。

### 4. 导入与重跑

在「导入数据」上传文件即按当前**激活的映射(算子管道)+逻辑规则**执行。导入后可在「导入记录」点**重跑**，
按最新配置重建主表（原始行已暂存，无需重新上传）。

也可直接维护：

- **名称映射库**（`NameMapping`）：把更名/括号不一致的客户名归一到同一规范名。
- **客户-业务员映射**（`CustomerSalesperson`）：修正/补充客户与业务员的归属。
- **期初余额**（`OpeningBalance`）：录入各单位期初借方/贷方。

### 5. 命令行批量导入

```bash
python3 manage.py import_winshare        # 全量导入 /mnt/win_share（按文件名去重，重跑同一文件会跳过）
python3 manage.py import_incremental     # 增量导入（按原始行内容哈希去重，只追加新行）
python3 manage.py clean_headers          # 清理被误当数据导入的表头行（单位名=表头列名，如 购买方名称/销方名称/对方户名）
python3 manage.py export_master          # 导出主表为 Excel（明细 + 汇总），用于核对
```

- `clean_headers`：删除主表中 `unit_name` 命中表头列名的伪记录，并同步删除对应 `RawImportRow`。加 `--dry-run` 仅预览。引擎 `_iter_rows` 已加表头防护，后续导入不会再产生此类伪记录，本命令用于清理历史遗留。
- `export_master`：支持 `--output <xlsx>`、`--source <类型>`、`--year <年份>` 过滤导出。

- `import_winshare`：逐文件读取，文件名已导入过则整体跳过，适合首次/修正后整盘重导。
- `import_incremental`：**不按文件名去重**，而是对每条原始行计算稳定哈希（`md5(json, sort_keys)`）并与已存 `RawImportRow.row_hash` 比对，已存在则跳过、仅写入新行。适合定期把同一份全量导出文件再跑一遍，自动只追加新增/变化的行。

  - 销项 `hrmxx2025.xlsx` 与 `hrmxx202501-202606.xlsx` 在 2025 年重叠，默认跳过前者；加 `--include-overlap` 强制包含（因哈希去重仍不会产生重复主表行）。
  - 历史 `RawImportRow` 的 `row_hash` 已由迁移 `0006` 回填，故增量去重对已导入数据同样有效。

### 6. 自动增量同步（仅变动时触发 + 失败通知）

数据源需手动把新 Excel 拷入 `/mnt/win_share`。除每日 03:00 的 cron 兜底外，还常驻一个**监听进程**：轮询 `/mnt/win_share` 的指纹，**仅当目录内容变动时才跑增量导入**（等价于 inotify 触发，但纯 bash 零依赖），靠 systemd 用户服务 + linger 在注销/重启后持续运行。

```bash
# 监听脚本（每 60s 比对一次指纹，变了才调用下面的导入脚本）
scripts/watch_incremental.sh

# 导入包装脚本：结果写 logs/incremental_YYYY-MM.log；失败时另写 logs/incremental_failed.log 并退出非 0
scripts/run_incremental.sh

# systemd 用户服务（已 enable --now，开机/注销后自启）
systemctl --user status watch-incremental
journalctl --user -u watch-incremental -f      # 实时看监听日志
```

**失败通知**：导入异常时 `run_incremental.sh` 会在 [`logs/incremental_failed.log`](logs/incremental_failed.log) 追加错误摘要并退出非 0；若有图形会话还会弹桌面通知。查看告警：
```bash
tail -f logs/incremental_failed.log
```

**日志位置**：
- `logs/incremental_YYYY-MM.log`：每次导入的常规输出。
- [`logs/incremental_failed.log`](logs/incremental_failed.log)：仅记录失败事件。

**管理**：
- 暂停监听：`systemctl --user stop watch-incremental`
- 恢复监听：`systemctl --user start watch-incremental`
- 改轮询间隔：编辑 [`scripts/watch_incremental.sh`](scripts/watch_incremental.sh) 里的 `INTERVAL`（秒），再 `systemctl --user restart watch-incremental`

> **数据源路径（重要）**：`/mnt/win_share` 为临时挂载点，**不建议正式环境直接依赖**——未挂载时任务会静默空跑或误判「无新数据」。
> 两个脚本均通过环境变量 `WINSHARE_DIR` 指定数据源根目录（默认 `/mnt/win_share`），`import_incremental` 也支持 `--src` 参数。
> 正式用法二选一：
> 1. 指向**稳定的本地目录**，需要时把文件拷进去（推荐，最简单）；
> 2. 让系统在开机时**自动挂载**该共享，并保持 `WINSHARE_DIR=/mnt/win_share`。
> 指定方式示例：`WINSHARE_DIR=/srv/books/incoming systemctl --user restart watch-incremental`；或写入 service 的 `Environment=WINSHARE_DIR=...`。
> 源目录不存在/未挂载时，脚本会跳过本轮并在 [`logs/incremental_failed.log`](logs/incremental_failed.log) 记录告警，而不会崩溃循环或误报成功。

> 说明：cron 每日 03:00 作为兜底仍保留，与监听进程互不冲突（增量去重保证幂等）。如不需要可 `crontab -e` 删掉对应行。

---

## 六、经手人（现任业务员）校准与管理

业务规则：往来账客户的**现任业务员**以「最近一次发生的收款经手人或开票复核人」为准。销项发票自 2024-09 起规定「复核人即业务员（经手人）」，故仅 2024-09 及以后的销项发票才派生经手人，更早的忽略（置空）；银行流水经手人同样计入。

> **关于「开票员能否当业务员」**：开票员（开票人）也能作为业务员。系统**不再按"是否开票员"硬性排除任何人**（历史上曾用 `STAFF_EXCLUDE_HANDLER` 拦截，现已取消）。当一次导入 / 校准派生的业务员与后台「现任」不一致时，记入 **「业务员变更提醒」**（`SalespersonChangeReminder`），由管理员在后台人工裁定是否采纳，从而兼顾「按最新业务自动更新」与「避免把内部人员误写入业务员」。

### 1. 派生最新经手人逻辑（[`books/salesperson.py`](books/salesperson.py)）
- 取每个客户 `occur_date` 最新、且 `handler` 非空的**主表记录**（含银行流水经手人表 `FundReceiptHandler`），作为该客户的**派生最新经手人**。不再剔除任何内部人员——最终是否成为「现任」由人工在提醒中裁定。
- 每次校准命令会据此刷新 `CustomerSalesperson.is_current`，并输出「派生最新 ≠ 后台现任」的**冲突清单**。

### 2. 校准历史数据（一次性 / 可重复执行）
```bash
python3 manage.py calibrate_handlers            # 校准：按历史最新经手人刷新现任（管理员主动校正，直接翻现任）
python3 manage.py calibrate_handlers --dry-run  # 仅预览，不做任何写入
```
- 校准会按派生最新经手人刷新 `is_current`，并把被本次采纳的「业务员变更提醒」标记为「已采纳」。
> 说明：导入引擎对**新导入**采用「只提醒、不自动改」（`auto_current=False`）；此命令是管理员显式校正入口（`auto_current=True`），会直接翻现任。

### 3. 后台手动管理（Django admin → 客户-业务员映射）
- **手动锁定（manual_override）**：勾选后，导入 / 校准命令**不再自动改写**该客户的现任业务员（新增的历史行仍会保留，但 `is_current` 保持锁定值）。
- **批量操作**（`CustomerSalespersonAdmin` actions）：
  - `make_current`：将选中行设为现任（其余清零）；
  - `apply_derived_latest`：按派生最新经手人回填现任；
  - `set_manual_override` / `clear_manual_override`：批量锁定 / 解锁。
- **冲突提示**：列表实时显示 `⚠ 冲突(派生:X)` 标记（派生最新 ≠ 后台现任时）；手动锁定的客户额外标注 `[锁定]`。编辑存在冲突的现任行时，页面顶部给出 `messages.warning` 提示。
- 主表 `MasterRecordAdmin` 亦提供 `clear_handler`（批量清空经手人）、`apply_customer_current`（回填各客户现任业务员）批量动作。

### 4. 增量导入的协同
增量导入触发的 `upsert_customer_salesperson(unit, salesperson, source, date, auto_current=False)` 会跳过 `manual_override=True` 的客户；当派生业务员与「现任」不一致时**只写入「业务员变更提醒」、不翻现任**（`auto_current=False`），保证后台锁定与人工裁定不被覆盖。可在后台「业务员变更提醒」列表中批量 **采纳**（设为现任并标记已采纳）或 **忽略**。「客户-业务员映射」页对存在待处理提醒的客户显示 `⚠` 徽章。

---

## 七、客户往来明细

「客户往来明细」页：选择单位名称（可联想）、年份、期间、业务类型、标记后查询，展示含**期初余额**、逐笔借贷与**期末余额**的明细账，并支持**导出 Excel**。

> 余额计算：期末 = 期初 + Σ借方 − Σ贷方。对供应商（应付）语义相反，可在 Phase 2 按应收/应付分别处理。

---

## 八、类别取发票状态 / 标记重设计 / 发票检索

### 1. 主表「类别」取发票「发票状态」列
- 导入时，发票来源（`sales_invoice` / `purchase_invoice`）的「类别」改为直接取 Excel **「发票状态」列**原值（如 正常 / 作废 / 红冲 / 空白作废 等），不再按借贷方向生成 销售/采购。
- 默认样例映射（`init_books --seed-winshare-mappings`）已为发票来源加入 `发票状态 → category`；若实际表头列名不同，可在后台「列映射」里调整 source_column。
- 非发票来源（银行/微信/承兑/其他）的类别仍由「默认类别」规则按借贷方向填充。
- `sales_perf`（开票回款明细）的开票额/红冲改按 `source_type='sales_invoice'` 判定，兼容新旧数据。
- **历史数据回填**：已导入记录的「发票状态」未持久化，需**重新导入**（或增量导入）原 Excel 才会写入类别；增量导入按行哈希去重，不会重复。

### 2. 标记（标注）重设计
- **设计初衷**：把非正常往来记录（红冲/作废）区别出去，**不参与往来记录与统计**。
- **统一排除**：`红冲`、`作废` 两类标记现在统一从「客户往来明细」「分业务员催款」「账期/账龄」「对账单」的余额与统计中排除（开票回款此前已排除红冲，现已补齐全）。综合查询仍默认展示全部，便于查找与编辑。
- **内联编辑（替代原重型页）**：原「基础数据 → 主表标记编辑」页（全量加载+逐行更新，偏重）已移除；改为在**综合查询**与**客户往来明细**查询页，由管理员点击「编辑模式」开关，就地用下拉框修改当前记录的「标记」，保存时仅更新变动行。
  - 往来明细页提供「含红冲/作废」勾选，便于管理员审阅/编辑被排除的记录。
- 标记可选项：无 / 红冲 / 已红冲 / 作废 / 保证金（保证金为独立统计，不受影响）。

### 3. 发票检索 / 穿透明细
- 导航「更多功能 → 发票检索」：按**发票号 / 单位 / 备注 / 发票状态(类别) / 经手人**关键字检索发票来源记录（可勾选「全部来源」扩大范围）。
- 每行提供「往来明细」穿透按钮，直达该客户的**客户往来明细**页，便于从一张发票下钻到其全部往来流水。
- **列表内联点击穿透（综合查询 / 保证金 / 对账 / 客户-业务员映射）**：上述各列表的结果表中，**「单位名称」可直接点击**跳转到该客户的**客户往来明细**页，**「发票号」可直接点击**跳转到该张发票的**发票货物明细**页（`invoice_detail`，展示归一字段 + 原始表头 + 货物明细行）。链接均在新标签页打开，便于保留原查询结果。综合查询、客户往来明细、账龄、业务员开票回款等页此前已支持同类穿透，现已统一覆盖到保证金、对账与客户映射列表。

---

## 九、已实现 / 后续（Phase 2）

**已实现（MVP）**：主表/原始行暂存/列映射/期初余额/名称映射库/客户-业务员映射等数据模型；6 类来源可配置导入与重跑；三类角色与行级权限；客户往来明细查询与 Excel 导出；其余 6 项功能占位页。

**基础数据可视化编辑（已补充）**：导航「基础数据」下提供图形化页面，无需进 Django 后台即可维护：
- **名称映射库**：新增/编辑/删除 原始名称→规范名称 的映射。
- **客户-业务员映射**：新增/编辑/删除，标记现任/历史。

> 主表「标记（标注）」的编辑已移至查询页内联完成（见第八章）：综合查询 / 客户往来明细 中管理员开启「编辑模式」即可就地修改，原独立的「主表标记编辑」重型页已移除。

权限：管理员可增删改；查询员/业务员仅可查看（业务员在客户-业务员映射中仅见自己客户），任何写操作对非管理员拦截。

**分业务员客户催款管理（已实现）**：导航「更多功能 → 分业务员客户催款管理」。按客户维度 **FIFO 匹配发票(借)与回款(贷)** 计算应收余额，并按账龄分桶（30天内 / 31-60 / 61-90 / 90天以上）与逾期金额（账龄超过所设账期天数）。按现任业务员归属聚合、可导出 Excel。业务员仅见自己客户的催款数据。

**账期管理 / 账龄分析（已实现）**：导航「更多功能 → 账期管理」。按客户出具账龄分析表（应收余额、逾期金额、30天内/31-60/61-90/90天以上），可导出 Excel。每个客户的「账期天数」在「客户-业务员映射」中维护（新增/编辑页含该字段），逾期据此判断；未设置时用查询页「默认账期」兜底。该账期同时被催款管理复用。

**对账单管理（已实现）**：导航「更多功能 → 对账单管理」。按客户+期间（年份/起止月）出具正式「往来对账函」：期初余额、本期借贷发生额与逐笔明细、期末余额，并含「客户名称（盖章）/核对人/日期」确认区。支持**导出 Excel** 与**打印**（打印样式自动隐藏导航与按钮，可直接打印或存 PDF）。业务员仅能为名下客户出具对账单。

**业务员开票回款明细（已实现）**：导航「更多功能 → 业务员开票回款明细」。按业务员（可勾选「展开到客户」下钻到每个客户）汇总 **开票额（销项发票借、剔除红冲）、红冲额、净开票、回款额（贷方/收入/承兑签收）、未回款额**，支持年份筛选与 Excel 导出。管理员/查询员可按业务员筛选；业务员仅见本人数据。

**保证金管理（已实现）**：导航「更多功能 → 保证金管理」。列出主表中标记为「保证金」的记录，按单位汇总**收保证金 / 退保证金 / 结余 / 笔数**，并附明细列表，支持按单位/年份/业务类型筛选与 Excel 导出（含汇总+明细两个工作表）。保证金标记可在「主表标记编辑」页设置；业务员仅见自己客户的保证金。

**综合查询 / 全局检索（已实现）**：导航「更多功能 → 综合查询」。跨所有来源检索主表，支持**关键字（单位/备注/经手人/发票号）、单位、业务类型、来源类型、标记、日期区间、金额区间**组合筛选，结果分页显示并支持导出 Excel。结果表中「单位名称」可点击直达该客户**往来明细**、「发票号」可点击直达**发票货物明细**。业务员仅见自己客户的数据。

**后续（Phase 2）**：标记的可视化编辑界面（已完成基础数据页，可继续扩展）；手机验证码改密；按需迁移至 PostgreSQL/MySQL 服务端数据库。

---

## 十、Docker 部署（可选，替代 deploy/ 的 systemd+宝塔 方案）

适用于希望用容器化方式部署的场景。仓库已提供 [`Dockerfile`](Dockerfile) + [`docker-compose.yml`](docker-compose.yml)（web = gunicorn，nginx = 反代），与 [`deploy/DEPLOY.md`](deploy/DEPLOY.md) 的「systemd + 宝塔」方案二选一，不要在同一台服务器混用（端口/进程冲突）。

### 1. 准备环境变量

复制并填写 [`deploy/env.template`](deploy/env.template)：

```bash
cp deploy/env.template .env
# 编辑 .env：把 DJANGO_SECRET_KEY 换成下方生成值，DJANGO_ALLOWED_HOSTS 填你的域名
```

生成 `SECRET_KEY`：

```bash
python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits+'-_') for _ in range(64)))"
```

### 2. 构建并启动

```bash
docker compose up -d --build
```

- `web` 服务启动时会自动 `migrate` + `collectstatic` 再拉起 gunicorn；
- SQLite 数据库通过绑定挂载 `./db_data` 持久化；静态文件通过 `static_volume` 共享给 nginx；
- `nginx` 监听 80，反代到 `web:8000`。

### 3. 访问与管理

- 应用：`http://<服务器IP>/`
- 创建管理员：`docker compose exec web python manage.py createsuperuser`
- 加载映射 fixture：`docker compose exec web python manage.py loaddata column_mappings`
- 查看日志：`docker compose logs -f web`

### 4. 启用 HTTPS

把证书挂到 `nginx` 服务的 `/etc/nginx/certs`，并在 [`docker-compose.yml`](docker-compose.yml) 取消 `443` 端口与 certs 卷的注释；同时取消 [`deploy/nginx/docker.conf`](deploy/nginx/docker.conf) 中 443 server 段的注释（或改用 certbot 容器）。最后把 `.env` 的 `DJANGO_SECURE_SSL_REDIRECT` 设为 `True`、`DJANGO_HSTS_SECONDS=31536000`。

### 5. 说明

- 生产建议把 SQLite 换成 PostgreSQL（[`ledger/settings.py`](ledger/settings.py) 改 `DATABASES` + `.env` 注入连接信息）。
- 容器入口见 [`docker-entrypoint.sh`](docker-entrypoint.sh)；构建忽略清单见 [`.dockerignore`](.dockerignore)。

---

## 十一、目录结构

```
ledger/         Django 项目配置（settings / urls / wsgi）
books/          业务应用
  models.py             8 个数据模型
  views.py              登录/导入/明细/改密/占位 视图
  decorators.py         角色与行级数据过滤
  imports/engine.py     Excel 解析 + 列映射 + 规范化 核心引擎
  management/commands/  init_books 初始化脚本
  templates/books/      Bootstrap 响应式模板
  admin.py              后台管理注册
requirements.txt
deploy/         生产部署（部署脚本 / systemd / nginx 模板 / env 模板）
  deploy.sh             服务器侧一键部署（备份/迁移/collectstatic/重载 nginx）
  local-deploy.sh       本机一键推送并触发部署（rsync + ssh）
  systemd/ledger.service  gunicorn systemd 单元（用户 www）
  nginx/                  nginx 配置模板（Ubuntu 风格 + 默认站点 catch-all）
  env.template           /etc/ledger/env 变量模板
  DEPLOY.md              完整部署手册（含 宝塔/OpenCloudOS 差异说明）
.gitlab-ci.yml   GitLab CI 可选（validate / test / deploy 三阶段）
```

---

## 十一、生产环境部署（腾讯云轻量 + 宝塔）

本系统在腾讯云轻量应用服务器（OpenCloudOS 9.6，已预装 **宝塔面板**）上完成了生产部署，对外提供 HTTPS 访问：

- 域名：`ledger.yanyinghuaerp.top`
- 访问地址：https://ledger.yanyinghuaerp.top/
- 运行方式：**gunicorn（systemd，用户 `www`）+ 宝塔 nginx 反向代理 + Let's Encrypt 证书**

### 1. 服务器环境要点（与标准 Ubuntu 不同，务必注意）

| 项 | 标准 Ubuntu | 本服务器（OpenCloudOS + 宝塔） |
| --- | --- | --- |
| 包管理器 | `apt` | `dnf` |
| nginx 位置 | `/etc/nginx` | `/www/server/nginx`（`/etc/nginx` 不存在） |
| 站点配置 | `/etc/nginx/sites-enabled` | `/www/server/panel/vhost/nginx/*.conf` |
| 运行用户 | `www-data` | `www` |
| 注意 | — | `dnf` 配置 `exclude=...nginx...`，**不要再用 dnf 安装/重装 nginx**，证书与站点均在**宝塔面板**操作 |

> 完整差异与排错见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md)。

### 2. 本机到服务器的 SSH 密钥

本机已生成专用密钥对并配置 `~/.ssh/config` 别名，部署脚本通过 `ssh ledger` 连接：

```bash
# ~/.ssh/config
Host ledger
    HostName 124.223.184.190
    User root
    IdentityFile ~/.ssh/id_ed25519_ledger
```

服务器公钥已写入 `/root/.ssh/authorized_keys`，可免密登录。

### 3. 生产环境变量 `/etc/ledger/env`

服务器上的 Django 运行参数集中放在 `/etc/ledger/env`（systemd 单元 `EnvironmentFile` 读取，权限 `640 root:www`，使 `www` 用户可读）：

```bash
DJANGO_SECRET_KEY=<64 位随机串>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=ledger.yanyinghuaerp.top,124.223.184.190
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_HSTS_SECONDS=31536000
```

模板见 [`deploy/env.template`](deploy/env.template)。**生产密钥不在 Git 仓库、也不进 GitLab**，安全性由服务器侧 [`deploy.sh`](deploy/deploy.sh) 的 `check_env` 校验门控保证。

### 4. 一键部署

两步完成「本机推送 → 服务器部署」：

```bash
# 本机：rsync 同步代码（不含 venv/db/staticfiles 等）到 /srv/ledger，并触发服务器侧部署
bash deploy/local-deploy.sh
```

`local-deploy.sh` 默认连接 `DEPLOY_HOST=ledger`、`DEPLOY_USER=root`（可用环境变量覆盖）。
到达服务器后由 [`deploy/deploy.sh`](deploy/deploy.sh)（`sudo bash /srv/ledger/deploy/deploy.sh`）完成：

1. 读取 `/etc/ledger/env`；若 `DJANGO_SECURE_SSL_REDIRECT=True` 则执行 `check_env` 严格校验（含 HSTS）。
2. 在线一致性热备数据库（执行 `manage.py backup_db`，备份到 `/var/backups/ledger` 并完成完整性校验；**不再用 `cp` 直接拷运行中的库**）。
3. `pip install -r requirements.txt`（含 gunicorn）。
4. `migrate --noinput` → `collectstatic --noinput` → `loaddata column_mappings`。
5. 将 `staticfiles/`、[`db.sqlite3/`](db.sqlite3)、`media/` 属主改为运行用户 `www`。
6. `systemctl restart ledger` 重启 gunicorn。
7. 重载 nginx（兼容宝塔：`systemctl reload nginx` 失败则回退到 `/www/server/nginx/sbin/nginx -s reload`）。

### 4.1 备份与恢复
数据库为单文件 SQLite。一键部署已自动做**在线一致性热备**（执行 `manage.py backup_db`，不用 `cp` 直接拷运行中的库）。手动备份/恢复/定时任务/完整性校验等完整流程见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md) 的「备份与恢复」一节，核心命令：

```bash
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db                         # 热备 + 完整性校验
sudo -u www /srv/ledger/venv/bin/python manage.py backup_db --restore <备份文件>   # 恢复（自动先安全备份当前库）
```

### 5. DNS 与 HTTPS

- **DNS**：在域名解析处添加 A 记录 `ledger → 124.223.184.190`。
- **证书**：在**宝塔面板**对该站点申请 Let's Encrypt 证书（文件/HTTP-01 验证），自动部署。
- **反向代理**：证书下发后，把反向代理规则写入宝塔的扩展 include 目录
  `/www/server/panel/vhost/nginx/extension/ledger.yanyinghuaerp.top/proxy.conf`（该文件**在证书续期/重签后会被保留**，因此比直接改 vhost 主配置更耐用）：

  ```nginx
  location ^~ /static/ {
      alias /srv/ledger/staticfiles/;
      expires 30d;
      access_log off;
  }
  location / {
      proxy_pass http://unix:/run/ledger/ledger.sock;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```

  > 关键点：静态文件用 `location ^~ /static/`（前缀 + `^~` 优先级高于宝塔默认的正则 `location ~ .*\.(js|css)?$`），否则静态资源会被 PHP 正则 location 抢走而 404。

- **多站点共存**：该服务器已运行多个网站并占用 80/443。额外提供一个 catch-all 默认站点（[`deploy/nginx/default.conf`](deploy/nginx/default.conf)，`listen 80 default_server; server_name _;`，仅放行 `.well-known/acme-challenge/` 验证目录，其余返回 `444`），避免未匹配域名的请求落到其他站点。

- 验证结果：HTTP 访问 `301 → https`；HTTPS 证书有效、启用 HSTS（`max-age=31536000; includeSubDomains; preload`）；登录页标题「登录 - 往来账管理系统」；静态资源 `200`。

### 6. GitLab CI（可选）

[`.gitlab-ci.yml`](.gitlab-ci.yml) 提供 `validate / test / deploy` 三阶段：

- `env-check`：运行 `check_env.py --strict-hsts`（`allow_failure: true`）。
- `django-test`：测试时覆盖 `DJANGO_SECURE_SSL_REDIRECT=False`、`DJANGO_HSTS_SECONDS=0`，确保 `DEBUG=False` 环境下测试不再因 301 重定向失败（实测 18 项通过）。
- `deploy-prod`：仅 `master` 分支，用 `rsync` + `ssh deploy.sh` 推送；生产密钥在服务器侧，GitLab 无需配置任何 `DJANGO_*` 密钥。

> 注：gitlab.com 在国内访问不稳定，当前采用「本机手动跑 `local-deploy.sh`」的方式部署；[`.gitlab-ci.yml`](.gitlab-ci.yml) 作为极狐 GitLab（gitlab.cn）等可直连时的自动部署方案保留。

### 7. 创建生产管理员

证书就绪后，以 `www` 身份通过 `manage.py shell` 创建超级用户（直接 `python script.py` 会因缺少 Django 上下文而 `ImproperlyConfigured`）：

```bash
su -s /bin/bash www -c 'cd /srv/ledger && \
  python manage.py shell < /tmp/mkadmin.py'
# mkadmin.py 内容：
# from django.contrib.auth import get_user_model
# U = get_user_model()
# if not U.objects.filter(username="admin").exists():
#     U.objects.create_superuser("admin", "admin@example.com", "herman333")
```

当前生产账号：`admin` / `herman333`（已验证 `is_superuser=True`）。

---

## 十二、相关文档索引

| 文档 | 说明 |
| --- | --- |
| [`deploy/DEPLOY.md`](deploy/DEPLOY.md) | 完整生产部署手册（宝塔/OpenCloudOS 差异、备份恢复、排错） |
| [`deploy/OPS.md`](deploy/OPS.md) | 运维备忘（鉴权、CI、安全验证、常用命令）；其中 [`§7 操作日志`](deploy/OPS.md#7-操作日志operationlog覆盖范围) 含 `trace_oplog` / `export_oplog` / `clean_oplog` 用法与 admin 点击定位 |
| [`操作日志导出功能使用说明.md`](操作日志导出功能使用说明.md) | 操作日志的时间段查询、CSV 导出、按时间段备份/清理独立使用文档 |
| [`穿透功能使用说明.md`](穿透功能使用说明.md) | 发票检索 / 往来明细穿透（从单张发票下钻到全部往来流水） |
| [`账套管理设计方案.md`](账套管理设计方案.md) | 多独立法人账套隔离方案设计（文件级/PG schema 多租户选型与落地阶段） |
| [`业务员筛选接口文档.md`](业务员筛选接口文档.md) | 分业务员筛选与权限接口说明 |
| [`票据包号码汇总与明细一致性对账报告.md`](票据包号码汇总与明细一致性对账报告.md) | 票据包号码汇总与明细一致性对账说明 |
| [`ENV.md`](ENV.md) | 环境变量与配置项清单 |

---

> 安全提醒：若部署过程中曾在命令行/对话中明文出现过服务器 root 密码，请及时在服务器上修改（`passwd root`），并确认 `/etc/ledger/env` 仅 `root:www` 可读（640）。
