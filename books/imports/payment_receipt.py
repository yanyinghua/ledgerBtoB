"""收款明细导入（独立 Excel/API）。

复用通用引擎的列映射/算子抽取层，构建 PaymentReceipt 并做归一：
  付款人名称/付款人账号 → CustomerEntity（客户归一字典）
  货主ID → Owner
  收款户名/收款账户 → ReceivingAccount
  收款方式+线上/线下+货主 → 默认收款账户（OwnerAccountMap）
  订单号 → Order（关联）

去重：external_ref 唯一（source_type + external_ref）；增量模式按原始行哈希。
"""
import pandas as pd
from django.db import transaction

from books.models import (
    SourceUpload, RawImportRow, PaymentReceipt, Order,
)
from books.imports.engine import (
    load_active_mappings, extract_fields, _read_sheet, _serialize, _canon_hash, BULK,
)
from books.imports.utils import to_date, to_decimal
from books.matching.normalization import (
    resolve_customer, resolve_owner, resolve_owner_account, resolve_receiving_account,
)

DETAIL_SOURCE = 'payment_detail'

# ---------------------------------------------------------------------------
# 内置表头模板：同一 source_type 下多套真实导出格式，按表头自动识别。
# 每套是 (source_column, target_field, [(op_key, params), ...]) 列表。
# ---------------------------------------------------------------------------
class SimpleStep:
    def __init__(self, op, params):
        self.op = type('Op', (), {'key': op})()
        self.params = params or {}


class _StepMgr:
    """让 m.steps.all() 可用（engine.extract_fields 的接口）。"""
    def __init__(self, steps):
        self._steps = steps

    def all(self):
        return self._steps


class SimpleMapping:
    """轻量映射对象，接口兼容 engine.extract_fields 期望的 Mapping。"""

    def __init__(self, source_column, target_field, steps):
        self.source_column = source_column
        self.target_field = target_field
        self.steps = _StepMgr([SimpleStep(op, params) for op, params in steps])


def _mk_mappings(rows):
    return [SimpleMapping(sc, tf, steps) for sc, tf, steps in rows]


# 招行「交易查询」：0701-1109 sheet，38 列，交易金额自带正负（收正付负）
CMB_TEMPLATE = _mk_mappings([
    ('流水号', 'payment_ref', [('none', {})]),
    ('交易日', 'payment_date', [('to_date', {})]),
    ('交易时间', 'payment_time', [('none', {})]),
    ('交易金额', 'payment_amount', [('to_decimal', {})]),  # 自带正负
    ('收(付)方名称', 'payer_name', [('trim', {})]),
    ('收(付)方账号', 'payer_account', [('none', {})]),
    ('摘要', 'remark', [('none', {})]),
    ('交易类型', 'pay_method', [('none', {})]),
    ('账号名称', 'receiving_account_name_raw', [('trim', {})]),
    ('账号', 'receiving_account_no', [('none', {})]),
])

# 富掌柜「交易明细」：07-10 sheet，47 列，平台扫码收款
FZG_TEMPLATE = _mk_mappings([
    ('交易单号', 'payment_ref', [('none', {})]),
    ('富友内部流水号', 'payment_ref', [('none', {})]),
    ('交易时间', 'payment_date', [('to_date', {})]),
    ('交易金额', 'payment_amount', [('to_decimal', {})]),
    ('交易备注3(用户账号)', 'payer_account', [('trim', {})]),  # 微信openid
    ('姓名', 'payer_name', [('trim', {})]),
    ('商户名称', 'receiving_account_name_raw', [('trim', {})]),
    ('商户号', 'receiving_account_no', [('none', {})]),
    ('支付方式', 'pay_method', [('none', {})]),
    ('业务类型', 'remark', [('none', {})]),
])


def detect_template(columns):
    """按表头识别模板：返回 (名称, mappings)；未识别返回 (None, None)。"""
    cols = set(str(c).strip() for c in columns)
    if {'交易日', '流水号', '收(付)方名称'} & cols and '交易金额' in cols:
        return '招行交易查询', CMB_TEMPLATE
    if {'交易单号', '商户名称', '支付方式'} & cols and '交易金额' in cols:
        return '富掌柜交易明细', FZG_TEMPLATE
    return None, None


def _mapped_cols(mappings):
    return {m.source_column for m in mappings}


def _sheet_usable(df, mappings):
    cols = set(str(c) for c in df.columns)
    targets = {m.target_field for m in mappings if m.source_column in cols}
    has_payer = any(t in targets for t in ('payer_name', 'payer_account', 'order_no'))
    has_amount = 'payment_amount' in targets or 'total_amount' in targets
    return has_payer and has_amount


def _iter_rows(xls, mappings=None, detect=True, sheet_hint=''):
    """按表头自动识别模板；一个文件只导一张最佳明细 sheet，避免多表重复。

    yield (raw, used_mappings, template_name)
    """
    candidates = []
    for sheet_name in xls.sheet_names:
        try:
            head_df = xls.parse(sheet_name, header=None, nrows=6)
        except Exception:
            continue
        hdr_row, tname, tmpl = None, None, None
        for idx in range(len(head_df)):
            cols = [str(c).strip() for c in head_df.iloc[idx].tolist()]
            n, t = detect_template(cols)
            if n:
                hdr_row, tname, tmpl = idx, n, t
                break
        if tmpl is None:
            # 未识别到内置模板：回退到 DB 的 payment_detail 自定义映射（向后兼容）
            if mappings:
                cols = [str(c).strip() for c in head_df.iloc[0].tolist()] if len(head_df) else []
                targets = {m.target_field for m in mappings if m.source_column in cols}
                if ('payment_ref' in targets or 'payer_name' in targets) and \
                   ('payment_amount' in targets or 'total_amount' in targets):
                    tname, tmpl = '自定义映射', mappings
                    hdr_row = 0
            if tmpl is None:
                continue
        # 估算数据行数（该 sheet 总行数 - 表头行）
        try:
            nrows = len(xls.parse(sheet_name, header=None)) - hdr_row - 1
        except Exception:
            nrows = 0
        candidates.append((sheet_name, hdr_row, tname, tmpl, max(nrows, 0)))

    if not candidates:
        return

    # 优先 sheet_hint 命中的；否则取数据行最多的一张（通常是完整明细主表）
    if sheet_hint:
        hits = [c for c in candidates if sheet_hint in c[0]]
        if hits:
            chosen = hits[0]
        else:
            chosen = max(candidates, key=lambda c: c[4])
    else:
        chosen = max(candidates, key=lambda c: c[4])
    sheet_name, hdr_row, tname, tmpl, _ = chosen

    try:
        df = xls.parse(sheet_name, header=hdr_row)
        df = df.loc[:, [c for c in df.columns if str(c).strip() and 'Unnamed' not in str(c)]]
    except Exception:
        return
    if df.empty:
        return
    df = df.where(pd.notna(df), None)
    for _, row in df.iterrows():
        yield {str(k).strip(): _serialize(v) for k, v in row.items()}, tmpl, tname


def _build_receipt(raw, mappings, upload, stats=None, default_owner=None, ref_prefix=''):
    f = extract_fields(raw, mappings)
    external_ref = str(f.get('payment_ref', '')).strip()
    payer_name = str(f.get('payer_name', '')).strip()
    payer_account = str(f.get('payer_account', '')).strip()
    amount = to_decimal(f.get('payment_amount')) or to_decimal(f.get('total_amount'))
    if not external_ref and not payer_name and not payer_account and not amount:
        if stats is not None:
            stats['no_key'] = stats.get('no_key', 0) + 1
        return None
    if not external_ref:
        # 无流水号时以行内容哈希作稳定 ref，避免空 ref 撞唯一约束
        external_ref = 'ROW-' + _canon_hash(raw)[:24]
    if ref_prefix:
        external_ref = f'{ref_prefix}:{external_ref}'

    channel_type = str(f.get('channel_type', '')).strip()
    pay_method = str(f.get('pay_method', '')).strip()
    account_name_raw = str(f.get('receiving_account_name_raw', '') or
                           f.get('receiving_account_name', '')).strip()
    platform_owner_id = str(f.get('platform_owner_id', '')).strip()
    owner_name = str(f.get('owner_name', '')).strip()
    platform_customer_id = str(f.get('platform_customer_id', '')).strip()
    platform_shop_id = str(f.get('platform_shop_id', '')).strip()
    order_no = str(f.get('order_no', '')).strip()

    r = PaymentReceipt(
        source_type=DETAIL_SOURCE, source_upload=upload,
        external_ref=external_ref, channel_type=channel_type, pay_method=pay_method,
        pay_date=to_date(f.get('payment_date') or f.get('order_date')),
        payer_name=payer_name, payer_account=payer_account, amount=amount,
        receiving_account_name=account_name_raw,
        match_status='未匹配',
    )

    # 关联订单（按外部订单号）
    if order_no:
        r.order = Order.objects.filter(external_order_no=order_no).first()

    # 客户归一：付款人名称/账号 → 主体（优先付款人名称，其次账号）
    cr = resolve_customer(payer_name, alias_type='付款人名称')
    if not cr and payer_account:
        cr = resolve_customer(payer_account, alias_type='付款人账号')
    if not cr and platform_shop_id:
        cr = resolve_customer(platform_shop_id, alias_type='平台店铺ID')
    if cr:
        r.entity = cr.obj

    # 店铺名称：优先归一到的客户主体名，其次关联订单的店铺名
    if r.entity_id:
        r.shop_name = r.entity.name
    elif r.order_id and r.order.shop_name:
        r.shop_name = r.order.shop_name

    # 货主归一：行内ID/名称优先，否则用导入时指定的默认货主
    orr = resolve_owner(platform_owner_id=platform_owner_id, owner_name=owner_name)
    if orr:
        r.owner = orr.obj
    elif default_owner is not None:
        r.owner = default_owner

    # 收款账户归一（户名/账号），再经货主字典补默认账户
    ar = resolve_receiving_account(account_name=account_name_raw, account_no=None)
    if ar:
        r.receiving_account = ar.obj
    if r.owner is None and r.receiving_account is not None:
        # 反查：该账户在 OwnerAccountMap 里对应的货主
        m = r.receiving_account.owner_maps.filter(is_active=True).first()
        if m:
            r.owner = m.owner

    if r.owner is not None and r.receiving_account is None:
        ar2 = resolve_owner_account(r.owner, channel_type, pay_method)
        if ar2:
            r.receiving_account = ar2.obj

    if r.entity or r.owner or r.receiving_account or r.order:
        r.match_status = '自动已匹配'
    return r


@transaction.atomic
def run_import_payment_receipt(file_obj, user, incremental=False, tag='',
                               default_owner=None, sheet_hint=''):
    """导入收款明细，返回 SourceUpload。

    :param default_owner: Owner 实例或 id，整份文件归属的默认货主
    :param sheet_hint: 指定 sheet 名（含该子串的优先），如 '0701-1109' / '07-10'
    """
    from books.models import Owner as _Owner
    if default_owner is not None and not hasattr(default_owner, 'pk'):
        default_owner = _Owner.objects.filter(pk=default_owner).first()

    stats = {'skipped': 0, 'no_key': 0, 'created': 0, 'templates': set()}
    name = getattr(file_obj, 'name', 'payment_receipt.xlsx')
    # 用文件名作 external_ref 前缀，避免两份不同格式文件的流水号撞车
    ref_prefix = name.replace('.xlsx', '').replace('.xls', '').replace('.csv', '')[:20]
    upload = SourceUpload.objects.create(
        source_type=DETAIL_SOURCE, file_name=name, uploaded_by=user,
        status='success', mode='incremental' if incremental else 'full', note=tag)
    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as exc:
        upload.status = 'failed'
        upload.note = f'无法解析文件（请用 .xlsx）：{exc}'
        upload.save(update_fields=['status', 'note'])
        return upload

    existing_refs = set(PaymentReceipt.objects.filter(source_type=DETAIL_SOURCE)
                        .values_list('external_ref', flat=True))
    existing_hash = set(RawImportRow.objects.filter(
        source_upload__source_type=DETAIL_SOURCE).exclude(row_hash='')
        .values_list('row_hash', flat=True)) if incremental else set()

    to_create = []
    fallback_mappings = load_active_mappings(DETAIL_SOURCE)
    for raw, used, tname in _iter_rows(xls, mappings=fallback_mappings, sheet_hint=sheet_hint):
        stats['templates'].add(tname)
        rh = _canon_hash(raw)
        if incremental and rh in existing_hash:
            stats['skipped'] += 1
            continue
        row_obj = RawImportRow.objects.create(source_upload=upload, data=raw, row_hash=rh)
        # 内置模板（多来源）加文件名前缀区分流水号；自定义映射保持原 ref
        prefix = ref_prefix if tname in ('招行交易查询', '富掌柜交易明细') else ''
        r = _build_receipt(raw, used, upload, stats,
                           default_owner=default_owner, ref_prefix=prefix)
        if r is None:
            stats['no_key'] += 1
            continue
        if r.external_ref and r.external_ref in existing_refs:
            stats['skipped'] += 1
            continue
        if r.external_ref:
            existing_refs.add(r.external_ref)
        r.raw_row = row_obj
        to_create.append(r)
        existing_hash.add(rh)

    created = PaymentReceipt.objects.bulk_create(to_create, batch_size=BULK)
    stats['created'] = len(created)
    upload.row_count = stats['created']
    parts = [f'新增 {stats["created"]} 条', f'跳过重复 {stats["skipped"]}']
    if stats['templates']:
        parts.append('识别模板：' + '/'.join(sorted(stats['templates'])))
    if stats['no_key']:
        parts.append(f'无关键字段 {stats["no_key"]}')
    upload.note = '；'.join(parts)
    if stats['created'] == 0:
        upload.status = 'empty'
    upload.save(update_fields=['row_count', 'status', 'note'])
    return upload
