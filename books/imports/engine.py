"""往来账管理系统 —— Excel 导入与转换引擎

流程：读 Excel(多 sheet) -> 表头自动识别 -> 套用可配置列映射(算子管道) ->
       跨字段逻辑规则 -> 写主表 + 原始行暂存(bulk_create)。

列映射、转换算子、逻辑规则均在后台手工配置；本引擎只按"当前激活的配置"执行，
便于拿到真实文件后校准、重跑。
"""
import datetime
import hashlib
import json
from decimal import Decimal, InvalidOperation, InvalidOperation

import pandas as pd
from django.db import transaction

from books.models import (
    SourceUpload, RawImportRow, ColumnMappingConfig, MasterRecord, LogicRule,
    InvoiceLineItem,
)
from .utils import (
    to_date, to_decimal, fullwidth_brackets, extract_salesperson, extract_reviewer_name,
    normalize_unit_name, _isna,
)
from .operators import apply_operator
from .rules import apply_rule

# 来源类型 -> 主表业务类型
BIZ_TYPE_MAP = {
    'sales_invoice': '发票',
    'purchase_invoice': '发票',
    'bank': '银行',
    'wechat': '微信',
    'acceptance': '承兑',
    'other': '其他',
}

# 业务逻辑：经手人 = 销售与回款的业务员；客户-业务员映射以「最新业务」更新，
# 即 销项发票类复核人（2024-09 起规定 复核人=业务员）与 银行流水经手人。
# 开票员（开票人）也能当业务员——不再按"是否开票员"硬性排除任何人；
# 导入时若派生业务员与现任不一致，记入「业务员变更提醒」，由人工在后台确认。
# （历史上曾用 STAFF_EXCLUDE_HANDLER 硬性排除内部人员，现已取消。）

# 销项发票自 2024-09 起才规定「复核人即业务员(经手人)」；此前无此规定，
# 故 handler(经手人) 仅对 2024-09 及以后的销项发票派生，更早的忽略（置空）。
# 该规则在导入与重跑时一致生效，避免旧文件重跑把 复核人 重新填回 handler。
SALES_HANDLER_FROM = datetime.date(2024, 9, 1)

# 明细表特征列（用于跳过纯明细 sheet，避免主表明细重复计入）
DETAIL_MARKERS = ['货物或应税劳务名称', '货物名称', '商品名称', '规格型号', '税收分类编码']

# 货物明细 sheet（信息汇总表）中用于识别发票号的候选列（按优先级）
INVOICE_NO_CANDIDATES = ['数电发票号码', '数电票号码', '发票号码']


def _norm_invoice_no(v):
    """从原始单元格归一出发票号：去除 '--' / 'nan' / 无意义小数尾缀 / 空白。"""
    if v is None:
        return ''
    s = str(v).strip()
    if s in ('', '--', 'nan', 'None', 'NaN'):
        return ''
    # 科学计数法（如 1.09e+18）不是真实发票号
    if 'e' in s.lower():
        return ''
    if s.endswith('.0'):
        s = s[:-2]
    return s


def is_goods_detail_sheet(df):
    """判定是否为「货物明细 sheet」：含『货物或应税劳务名称』且存在金额类数值列。

    排除建筑服务/机动车销售统一发票等虽有该列但无货物金额行的 sheet。
    """
    cols = [str(c) for c in df.columns]
    if '货物或应税劳务名称' not in cols:
        return False
    needed = {'金额', '税额', '价税合计', '单价', '数量'}
    return bool(set(cols) & needed)


def _to_decimal_safe(v):
    """安全转 Decimal，失败 / 非有限值（NaN、inf）返回 None。"""
    if v is None or v == '':
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d.is_nan() or d.is_infinite():
        return None
    return d


def _iter_master_invoice_keys(queryset):
    """生成 (发票号, master_id)。

    一条主表记录可能有两个号码：映射取到的 invoice_number（可能是旧式短号）
    与原始行里的数电票号（20 位）。明细 sheet 用的是其中任意一个，故两者都收进索引，
    避免「主表存短号、明细存数电号」时对不上。
    """
    for m in queryset:
        seen = set()
        no = (m.invoice_number or '').strip()
        if no:
            seen.add(no)
            yield no, m.id
        data = m.raw_row.data if m.raw_row_id else None
        if not isinstance(data, dict):
            continue
        for c in INVOICE_NO_CANDIDATES:
            v = _norm_invoice_no(data.get(c))
            if v and v not in seen:
                seen.add(v)
                yield v, m.id


def _import_detail_sheets(xls, upload):
    """解析 xlsx 中的货物明细 sheet（信息汇总表），为 upload 下每张发票建 InvoiceLineItem。

    - 按发票号关联到 MasterRecord：本次 upload 优先，其次同来源类型的历史记录
      （增量导入常常不新增主表行，明细必须能挂到历史主表上，否则全是空关联）。
    - 同时存 invoice_number，供穿透查询直接按发票号取明细。
    - 为幂等，先删除本 upload 既有明细行，再重建。
    """
    InvoiceLineItem.objects.filter(source_upload=upload).delete()
    masters = (MasterRecord.objects.filter(source_type=upload.source_type)
               .select_related('raw_row')
               .only('id', 'invoice_number', 'raw_row', 'raw_row__data')
               .order_by('id'))
    master_by_inv = {}
    for no, mid in _iter_master_invoice_keys(masters):
        master_by_inv.setdefault(no, mid)
    for no, mid in _iter_master_invoice_keys(masters.filter(source_upload=upload)):
        master_by_inv[no] = mid
    items, seen_idx = [], 0
    inv_dtype = _invoice_no_dtype()
    for sn in xls.sheet_names:
        try:
            df = xls.parse(sn, header=0, dtype=inv_dtype)
        except Exception:
            continue
        if df is None or df.empty or not is_goods_detail_sheet(df):
            continue
        for _, r in df.iterrows():
            data = {str(k): r[k] for k in df.columns}
            inv = None
            for c in INVOICE_NO_CANDIDATES:
                inv = _norm_invoice_no(data.get(c))
                if inv:
                    break
            if not inv:
                continue
            goods = (str(data.get('货物或应税劳务名称') or '')).strip()
            if not goods:
                continue
            items.append(InvoiceLineItem(
                source_upload=upload,
                master_record_id=master_by_inv.get(inv),
                invoice_number=inv,
                goods_name=goods[:255],
                spec=(str(data.get('规格型号') or '')).strip()[:255],
                unit=(str(data.get('单位') or '')).strip()[:40],
                quantity=_to_decimal_safe(data.get('数量')),
                price=_to_decimal_safe(data.get('单价')),
                amount=_to_decimal_safe(data.get('金额')),
                tax_rate=(str(data.get('税率') or '')).strip()[:20],
                tax=_to_decimal_safe(data.get('税额')),
                row_index=seen_idx,
            ))
            seen_idx += 1
            if len(items) >= BULK:
                InvoiceLineItem.objects.bulk_create(items)
                items = []
    if items:
        InvoiceLineItem.objects.bulk_create(items)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _serialize(v):
    """把 Excel 单元格值转为可 JSON 序列化的原生类型（Decimal 存字符串保精度）。"""
    if _isna(v):
        return ''
    if isinstance(v, (datetime.datetime, datetime.date, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float):
        # 去掉无意义的 .0
        if v.is_integer():
            return int(v)
        return v
    return str(v)


def infer_category(source_type, debit, credit):
    """遗留的类别推断（已被 category_default 规则块替代，保留供参考）。"""
    if source_type == 'sales_invoice':
        return '销售'
    if source_type == 'purchase_invoice':
        return '采购'
    if source_type == 'bank':
        return '借方' if debit > 0 else ('贷方' if credit > 0 else '其他')
    if source_type == 'acceptance':
        return '申请' if debit > 0 else ('签收' if credit > 0 else '其他')
    if source_type == 'wechat':
        return '收入' if credit > 0 else ('支出' if debit > 0 else '其他')
    return '借方' if debit > 0 else ('贷方' if credit > 0 else '其他')


# ---------------------------------------------------------------------------
# 列映射加载与字段提取
# ---------------------------------------------------------------------------
def load_active_mappings(source_type):
    return list(ColumnMappingConfig.objects.filter(
        source_type=source_type, is_active=True).prefetch_related('steps').order_by('order'))


def _apply_legacy(transform, value):
    """兼容旧的单一 transform 字段（当某列映射尚未配置 MappingStep 时回退）。"""
    if transform == 'fullwidth_brackets':
        return fullwidth_brackets(str(value))
    if transform == 'regex_salesperson':
        return extract_salesperson(str(value))
    if transform == 'regex_reviewer':
        return extract_reviewer_name(str(value))
    return value


def extract_fields(raw, mappings):
    """按映射把原始行提取为主表字段；同字段首个非空映射生效（order 小优先）。
    优先走 MappingStep 算子管道，否则回退旧 transform。"""
    result = {}
    for m in mappings:
        col = m.source_column
        if col not in raw:
            continue
        val = raw[col]
        if val in (None, ''):
            continue
        steps = list(m.steps.all())
        if steps:
            for step in steps:
                val = apply_operator(step.op.key, val, step.params, {'raw': raw})
        else:
            val = _apply_legacy(m.transform, val)
        result.setdefault(m.target_field, val)
    return result


def is_detail_sheet(df):
    """含货物明细标记列的 sheet 视为明细 sheet（如发票「信息汇总表」），跳过。"""
    cols = [str(c) for c in df.columns]
    return any(any(mk in c for mk in DETAIL_MARKERS) for c in cols)


def _sheet_usable(df, mappings):
    """sheet 是否可用：需能定位单位名，且至少有一个金额列（debit/credit），
    避免建筑服务等无金额特殊 sheet 产生 0 金额垃圾行。"""
    cols = set(str(c) for c in df.columns)
    targets_covered = {m.target_field for m in mappings if m.source_column in cols}
    if 'unit_name' not in targets_covered:
        return False
    if not ('debit' in targets_covered or 'credit' in targets_covered):
        return False
    return True


def _invoice_no_dtype(mappings=None):
    """发票号列一律按文本读取的 dtype 映射。

    数电票号是 20 位数字，超过 int64/浮点精度：只要该列存在空单元格，pandas 就会
    把它整列读成 float64，取出来变成 2.6422000002354455e+19 —— 精度已丢失且与明细表
    （按文本归一的 20 位号码）对不上，货物明细就挂不上。故强制 str 读取。
    """
    cols = set(INVOICE_NO_CANDIDATES)
    for m in (mappings or []):
        if m.target_field == 'invoice_number':
            cols.add(m.source_column)
    return {c: str for c in cols}


def _read_sheet(xls, sheet_name, mapped_cols, mappings=None):
    """读取单个 sheet 并自动识别表头行。若第 0 行仅 1 个非空且等于 sheet 名，
    说明第 0 行是标题，真正表头在第 1 行。并对新表头做回退校验。"""
    dtype = _invoice_no_dtype(mappings)
    try:
        head = xls.parse(sheet_name, header=None, nrows=2)
    except Exception:
        return None
    use_header = 0
    if len(head) >= 2:
        r0 = head.iloc[0]
        nonnull = [x for x in r0.tolist() if not _isna(x)]
        # 标题单元格可能是 "HISTORYDETAIL" 或带方括号的 "[HISTORYDETAIL]"
        title = str(nonnull[0]).strip().strip('[]') if nonnull else ''
        if len(nonnull) == 1 and title == str(sheet_name):
            use_header = 1
    try:
        df = xls.parse(sheet_name, header=use_header, dtype=dtype)
    except Exception:
        return None
    # 回退校验：用 header=1 后新表头若不含任何已配置列，退回 header=0
    if use_header == 1 and mapped_cols:
        new_cols = set(str(c) for c in df.columns)
        if not (mapped_cols & new_cols):
            try:
                df = xls.parse(sheet_name, header=0, dtype=dtype)
            except Exception:
                return None
    return df


def load_active_rules(source_type):
    return list(LogicRule.objects.filter(
        source_type=source_type, is_active=True).select_related('rule_type').order_by('order'))


# ---------------------------------------------------------------------------
# 记录构建（含跨字段逻辑规则）
# ---------------------------------------------------------------------------
def _build_record(source_type, raw, mappings, upload, rules, stats=None):
    fields = extract_fields(raw, mappings)

    def _s(v):
        return '' if v in (None, '') else str(v).strip()

    unit_raw = _s(fields.get('unit_name'))
    if not unit_raw:
        if stats is not None:
            stats['no_unit'] = stats.get('no_unit', 0) + 1
        return None
    tax_no = _s(fields.get('tax_no'))
    unit_name = normalize_unit_name(unit_raw, tax_no)
    occur_date = to_date(fields.get('occur_date'))
    debit = to_decimal(fields.get('debit', 0))
    credit = to_decimal(fields.get('credit', 0))
    category = _s(fields.get('category'))
    handler = _s(fields.get('handler'))
    # 仅 2024-09 起销项发票才规定 复核人=业务员(经手人)；更早的按管理要求忽略（置空）。
    # 注意：不再按"是否开票员"排除——开票员也能当业务员，由人工在提醒中裁定。
    if source_type == 'sales_invoice' and occur_date and occur_date < SALES_HANDLER_FROM:
        handler = ''
    remark = _s(fields.get('remark'))
    # 发票号：与货物明细 sheet 保持同一取号顺序（数电发票号码优先，没有才用旧式发票号码），
    # 否则主表存短号、明细存 20 位数电号，两边永远对不上，货物明细挂不上。
    invoice_number = ''
    for c in INVOICE_NO_CANDIDATES:
        invoice_number = _norm_invoice_no(raw.get(c))
        if invoice_number:
            break
    if not invoice_number:
        # 原始行没有候选列（列名差异较大的老模板）时回退到映射结果
        invoice_number = _norm_invoice_no(fields.get('invoice_number'))

    rec = MasterRecord(
        source_type=source_type, source_upload=upload,
        unit_name=unit_name, biz_type=BIZ_TYPE_MAP.get(source_type, '其他'),
        year=occur_date.year if occur_date else 0,
        period=occur_date.strftime('%Y%m') if occur_date else '',
        occur_date=occur_date, category=category, debit=debit, credit=credit,
        tag='', handler=handler, remark=remark, tax_no=tax_no,
        invoice_number=invoice_number, blue_invoice_no='',
        package_no=_s(fields.get('package_no')),
    )
    context = {'raw': raw, 'source_type': source_type}
    for rule in rules:
        apply_rule(rule.rule_type.key, rec, rule.params, context)

    # 作废发票在导入环节即过滤，不写入主表（否则会污染往来与统计）。
    # 红冲（红字/负数）必须保留，以保证借贷余额等统计完整。
    if rec.tag == '作废' or rec.category in ('作废', '空白作废'):
        if stats is not None:
            stats['void'] = stats.get('void', 0) + 1
        return None
    return rec


# ---------------------------------------------------------------------------
# 主导入入口
# ---------------------------------------------------------------------------
BULK = 2000


def _iter_rows(xls, mappings, mapped_cols):
    """生成该文件解析后的原始行 dict（已做表头识别、跳过明细/无关 sheet）。

    额外防护：若某行的「单位名称」列取值等于某个表头列名（如文件表头行被误当数据行，
    值为「购买方名称」「销方名称」「对方户名」等），直接跳过，避免表头泄漏进主表。
    """
    unit_cols = [m.source_column for m in mappings if m.target_field == 'unit_name']
    for sheet_name in xls.sheet_names:
        df = _read_sheet(xls, sheet_name, mapped_cols, mappings)
        if df is None or df.empty:
            continue
        if is_detail_sheet(df) or not _sheet_usable(df, mappings):
            continue
        df = df.where(pd.notna(df), None)
        for _, row in df.iterrows():
            raw = {str(k): _serialize(v) for k, v in row.items()}
            # 表头行泄漏特征：单位名单元格的值恰好是某个表头列名
            if unit_cols and any(str(raw.get(uc, '')) in mapped_cols for uc in unit_cols):
                continue
            yield raw


def _canon_hash(d):
    """对原始行做规范化哈希，用于增量去重（与序列化方式一致）。"""
    return hashlib.md5(
        json.dumps(d, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _natural_key(rec):
    """主表记录的自然键：用于跨文件格式去重。
    同来源 + 同日期 + 同单位 + 同借贷金额 + 同摘要 + 同发票号 + 同票据包号 => 视为同一笔交易。
    票据包号必须参与：承兑汇票没有发票号、备注常为空，同一天同一对手同金额的多张票
    包号/子票区间各不相同，少了包号会被误判为重复而丢票。"""
    return (rec.source_type, rec.occur_date, rec.unit_name,
            rec.debit, rec.credit, rec.remark, rec.invoice_number, rec.package_no)


def _load_existing_keys(source_type):
    """已存在主表记录的自然键集合，导入时据此跳过重复行（比整行哈希更稳健，跨格式也有效）。"""
    qs = MasterRecord.objects.filter(source_type=source_type).values_list(
        'source_type', 'occur_date', 'unit_name', 'debit', 'credit', 'remark',
        'invoice_number', 'package_no')
    return {(st, d, u, dec, cre, rem, inv, pkg) for st, d, u, dec, cre, rem, inv, pkg in qs}


@transaction.atomic
def run_import(source_type, file_obj, user, tag=''):
    """全量导入：读取 Excel，按当前激活列映射(算子管道)+逻辑规则转换。
    每次生成新的 SourceUpload（原始行已暂存，可重跑）。返回 SourceUpload。
    tag: 可选标记，整批打在该次导入的记录上（如个人银行流水 tag='个人'）；
          仅当记录本身未被规则设置 tag 时生效（不覆盖迪链等）。"""
    file_name = getattr(file_obj, 'name', 'upload.xlsx')
    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=file_name, uploaded_by=user, status='success')

    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as e:  # noqa: BLE001
        upload.status = 'failed'
        upload.note = f'读取 Excel 失败：{e}（若为 .xls 文件请另存为 .xlsx）'
        upload.save()
        return upload

    mappings = load_active_mappings(source_type)
    if not mappings:
        upload.status = 'failed'
        upload.note = '该来源类型尚未配置列映射，请先在「列映射配置」中设置。'
        upload.save()
        return upload
    mapped_cols = set(m.source_column for m in mappings)
    rules = load_active_rules(source_type)
    # 去重：加载已存在（任意历史导入）的自然键集合，避免重叠导入产生重复主表记录
    existing_keys = _load_existing_keys(source_type)
    skipped = 0

    # 第一遍：先落原始行（bulk_create 后对象带 pk），记录 (raw, raw_obj) 配对
    raw_objs, pairs, count = [], [], 0
    for raw in _iter_rows(xls, mappings, mapped_cols):
        raw_obj = RawImportRow(source_upload=upload, data=raw, row_hash=_canon_hash(raw))
        raw_objs.append(raw_obj)
        pairs.append((raw, raw_obj))
        if len(raw_objs) >= BULK:
            RawImportRow.objects.bulk_create(raw_objs)
            raw_objs = []
    if raw_objs:
        RawImportRow.objects.bulk_create(raw_objs)
        raw_objs = []

    # 第二遍：用已分配 pk 的原始行回填 raw_row，再建主表记录
    rec_batch = []
    stats = {}
    for raw, raw_obj in pairs:
        rec = _build_record(source_type, raw, mappings, upload, rules, stats=stats)
        if rec is None:
            continue
        key = _natural_key(rec)
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        if tag and not rec.tag:
            rec.tag = tag
        rec.raw_row = raw_obj
        rec_batch.append(rec)
        count += 1
        if len(rec_batch) >= BULK:
            MasterRecord.objects.bulk_create(rec_batch)
            rec_batch = []
    if rec_batch:
        MasterRecord.objects.bulk_create(rec_batch)

    # 发票类来源：解析货物明细 sheet（信息汇总表），按发票号关联到主表记录
    if source_type in ('sales_invoice', 'purchase_invoice'):
        _import_detail_sheets(xls, upload)

    upload.row_count = count
    if count == 0 and upload.status == 'success':
        upload.status = 'partial'
        if skipped:
            upload.note = f'全部为重复记录，已跳过（共 {skipped} 条）。'
        else:
            upload.note = '未从文件中解析出有效记录，请检查列映射或文件格式。'
    else:
        parts = []
        if skipped:
            parts.append(f'跳过重复 {skipped} 条')
        void_n = stats.get('void', 0)
        if void_n:
            parts.append(f'已过滤作废发票 {void_n} 条（导入环节已排除，不入库）')
        no_unit = stats.get('no_unit', 0)
        if no_unit:
            parts.append(f'无对方户名跳过 {no_unit} 行')
        if parts:
            upload.note = (upload.note or '') + '；' + '；'.join(parts)
    upload.save()
    return upload


@transaction.atomic
def run_import_incremental(source_type, file_obj, user, tag=''):
    """增量导入：仅写入尚未导入过的行（按原始行内容哈希去重）。
    已存在的行（含之前全量导入的）会被跳过，适合定期刷新同一份全量导出文件。返回 SourceUpload。
    tag: 可选整批标记（如个人银行流水 tag='个人'），仅当记录未被规则设置 tag 时生效。"""
    file_name = getattr(file_obj, 'name', 'upload.xlsx')
    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as e:  # noqa: BLE001
        return SourceUpload.objects.create(
            source_type=source_type, file_name=file_name, uploaded_by=user,
            status='failed', note=f'读取 Excel 失败：{e}')

    mappings = load_active_mappings(source_type)
    if not mappings:
        return SourceUpload.objects.create(
            source_type=source_type, file_name=file_name, uploaded_by=user,
            status='failed', note='该来源类型尚未配置列映射，请先在「列映射配置」中设置。')
    mapped_cols = set(m.source_column for m in mappings)
    rules = load_active_rules(source_type)

    # 已存在原始行的哈希集合（取索引列，避免整行 JSON 反序列化）
    existing = set(RawImportRow.objects.filter(
        source_upload__source_type=source_type).exclude(row_hash='').values_list('row_hash', flat=True))

    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=file_name, uploaded_by=user,
        status='success', mode='incremental')
    raw_objs, pairs, added, skipped = [], [], 0, 0
    for raw in _iter_rows(xls, mappings, mapped_cols):
        sig = _canon_hash(raw)
        if sig in existing:
            skipped += 1
            continue
        existing.add(sig)
        raw_obj = RawImportRow(source_upload=upload, data=raw, row_hash=sig)
        raw_objs.append(raw_obj)
        pairs.append((raw, raw_obj))
        if len(raw_objs) >= BULK:
            RawImportRow.objects.bulk_create(raw_objs)
            raw_objs = []
    if raw_objs:
        RawImportRow.objects.bulk_create(raw_objs)
        raw_objs = []

    rec_batch = []
    stats = {}
    for raw, raw_obj in pairs:
        rec = _build_record(source_type, raw, mappings, upload, rules, stats=stats)
        if rec is None:
            continue
        if tag and not rec.tag:
            rec.tag = tag
        rec.raw_row = raw_obj
        rec_batch.append(rec)
        added += 1
        if len(rec_batch) >= BULK:
            MasterRecord.objects.bulk_create(rec_batch)
            rec_batch = []
    if rec_batch:
        MasterRecord.objects.bulk_create(rec_batch)

    void_n = stats.get('void', 0)

    if source_type in ('sales_invoice', 'purchase_invoice'):
        _import_detail_sheets(xls, upload)

    upload.row_count = added
    note = f'增量导入：新增 {added} 条，跳过已存在 {skipped} 条'
    if void_n:
        note += f'；已过滤作废发票 {void_n} 条（不入库）'
    upload.note = note
    if added == 0 and upload.status == 'success':
        upload.status = 'empty'
    upload.save()
    return upload


def rerun_import(upload_id, user):
    """按当前映射/规则删除旧主表行并重新生成（用于校准后重跑）。"""
    upload = SourceUpload.objects.get(id=upload_id)
    MasterRecord.objects.filter(source_upload=upload).delete()
    mappings = load_active_mappings(upload.source_type)
    rules = load_active_rules(upload.source_type)
    count = 0
    rec_batch = []
    for raw_row in upload.raw_rows.all():
        rec = _build_record(upload.source_type, raw_row.data, mappings, upload, rules)
        if rec is not None:
            rec.raw_row = raw_row
            rec_batch.append(rec)
            count += 1
            if len(rec_batch) >= BULK:
                MasterRecord.objects.bulk_create(rec_batch)
                rec_batch = []
    if rec_batch:
        MasterRecord.objects.bulk_create(rec_batch)
    upload.row_count = count
    upload.status = 'success' if count > 0 else 'partial'
    upload.save()
    return upload
