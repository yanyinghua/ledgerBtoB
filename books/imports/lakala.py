"""B2B 拉卡拉订单导入。

复用通用引擎的列映射/算子抽取层（engine.load_active_mappings / extract_fields、
_read_sheet / _serialize / _canon_hash），但构建层改为 Order + OrderPayment，
不走 MasterRecord。全程配置驱动：真实列名到位后只需维护 ColumnMappingConfig。

去重：优先 external_order_no（同 source_type 唯一），缺失时回退
(order_date, total_amount, counterparty_raw_name, direction) 自然键。
"""
import pandas as pd
from django.db import transaction

from books.models import (
    SourceUpload, RawImportRow, Order, OrderPayment, Counterparty,
)
from books.imports.engine import (
    load_active_mappings, extract_fields, _read_sheet, _serialize, _canon_hash, BULK,
)
from books.imports.utils import to_date, to_decimal, normalize_account_name
from books.matching import engine as matching_engine

LAKALA_SOURCES = ('lakala_order', 'lakala_order_api')


def _mapped_cols(mappings):
    return {m.source_column for m in mappings}


def _sheet_usable_lakala(df, mappings):
    """订单 sheet 门槛：映射出「原始户名」或「订单号」+ 至少一个金额列。"""
    cols = set(str(c) for c in df.columns)
    targets = {m.target_field for m in mappings if m.source_column in cols}
    has_identity = 'counterparty_raw_name' in targets or 'order_no' in targets
    has_amount = any(t in targets for t in ('total_amount', 'paid_amount', 'payment_amount'))
    return has_identity and has_amount


def _iter_lakala_rows(xls, mappings):
    mapped_cols = _mapped_cols(mappings)
    for sheet_name in xls.sheet_names:
        df = _read_sheet(xls, sheet_name, mapped_cols, mappings)
        if df is None or df.empty:
            continue
        if not _sheet_usable_lakala(df, mappings):
            continue
        df = df.where(pd.notna(df), None)
        for _, row in df.iterrows():
            yield {str(k): _serialize(v) for k, v in row.items()}


def _infer_direction(fields, raw):
    d = str(fields.get('direction_raw', '')).strip()
    if '采购' in d or '进货' in d or ('付款' in d and '销售' not in d):
        return '采购'
    if '销售' in d or '出货' in d or '收款' in d:
        return '销售'
    return '销售'


def _build_order(source_type, raw, mappings, upload, stats=None):
    fields = extract_fields(raw, mappings)
    raw_name = str(fields.get('counterparty_raw_name', '')).strip()
    order_no = str(fields.get('order_no', '')).strip()
    if not raw_name and not order_no:
        if stats is not None:
            stats['no_key'] = stats.get('no_key', 0) + 1
        return None

    total = to_decimal(fields.get('total_amount'))
    paid = to_decimal(fields.get('paid_amount'))
    payment_amount = to_decimal(fields.get('payment_amount'))
    if total == 0 and paid == 0 and payment_amount == 0:
        if stats is not None:
            stats['no_amount'] = stats.get('no_amount', 0) + 1
        return None

    if total == 0 and payment_amount != 0:
        total = payment_amount
    if paid == 0 and payment_amount != 0:
        paid = payment_amount

    order = Order(
        source_type=source_type,
        source_upload=upload,
        external_order_no=order_no,
        sub_order_no=str(fields.get('sub_order_no', '')).strip(),
        direction=_infer_direction(fields, raw),
        order_date=to_date(fields.get('order_date')),
        counterparty_raw_name=raw_name,
        currency='CNY',
        total_amount=total,
        paid_amount=paid,
        goods_summary=str(fields.get('goods_summary', '')).strip(),
        order_status=str(fields.get('order_status', '')).strip(),
        payment_status=str(fields.get('payment_status', '')).strip(),
        # 归一扩展（0028）：平台原始 ID
        platform_owner_id=str(fields.get('platform_owner_id', '')).strip(),
        platform_customer_id=str(fields.get('platform_customer_id', '')).strip(),
        match_status='未匹配',
    )
    # 原始业务员名（供人工/后续匹配参考）
    sp_raw = str(fields.get('salesperson_name', '')).strip()
    if sp_raw:
        order.salesperson_raw = sp_raw

    # 商户：优先按映射的商户名查找/自动建（谨慎：仅在名称唯一时）
    merchant_name = str(fields.get('merchant_name', '')).strip()
    if merchant_name:
        from books.models import Merchant
        m = Merchant.objects.filter(name=merchant_name).first()
        if m:
            order.merchant = m

    # 收付款账户：按映射的账户名查找
    acct_name = str(fields.get('receiving_account_name', '')).strip() or \
                str(fields.get('payment_account_name', '')).strip()
    if acct_name:
        from books.models import ReceivingAccount
        acct = ReceivingAccount.objects.filter(account_name=acct_name).first()
        if acct:
            order.receiving_account = acct

    # 户名匹配（4 目标），在行构建阶段即尝试解析
    if raw_name:
        result = matching_engine.resolve_account_name(
            raw_name, merchant=order.merchant_id,
            account_scope=order.receiving_account_id, direction=order.direction,
            auto_create=False)
        if result.counterparty:
            order.counterparty = result.counterparty
        if result.salesperson and not order.salesperson_id:
            order.salesperson = result.salesperson
        if result.department and not order.department_id:
            order.department = result.department
        if result.merchant and not order.merchant_id:
            order.merchant = result.merchant
        if result.receiving_account and not order.receiving_account_id:
            order.receiving_account = result.receiving_account
        order.match_status = result.status
        order.match_confidence = result.confidence

    # ---- 0028 归一：货主ID → Owner；店铺名/平台店铺ID → CustomerEntity ----
    from books.matching.normalization import NormalizeResult, resolve_owner, resolve_customer
    owner_res = resolve_owner(
        platform_owner_id=order.platform_owner_id,
        owner_name=str(fields.get('owner_name', '')).strip())
    if owner_res:
        order.owner = owner_res.obj

    # 客户归一：优先平台店铺ID（可靠锚点），其次订单户名（店铺名）
    cust_res = NormalizeResult()
    platform_shop_id = str(fields.get('platform_shop_id', '')).strip()
    if platform_shop_id:
        cust_res = resolve_customer(platform_shop_id, alias_type='平台店铺ID')
    if not cust_res and raw_name:
        cust_res = resolve_customer(raw_name, alias_type='店铺名')
    if cust_res:
        order.customer_entity = cust_res.obj

    # 付款子行：订单内收款列
    pay_date = to_date(fields.get('payment_date')) or order.order_date
    pay_ref = str(fields.get('payment_ref', '')).strip()
    if payment_amount != 0 or paid != 0:
        p = OrderPayment(
            order=order, pay_date=pay_date,
            amount=payment_amount if payment_amount != 0 else paid,
            account=order.receiving_account,
            direction='收款' if order.direction == '销售' else '付款',
            external_ref=pay_ref,
        )
        # 暂存，待 order 保存后回填 order FK（build 阶段先记到临时属性）
        order._pending_payment = p
    else:
        order._pending_payment = None

    return order


def _existing_natural_keys(source_type):
    return set(Order.objects.filter(source_type=source_type).values_list(
        'order_date', 'total_amount', 'counterparty_raw_name', 'direction'))


def _persist(orders, stats):
    """批量写库：先 bulk_create Order（需回填 pk 再建 Payment）。"""
    to_create = []
    existing_order_nos = set(Order.objects.values_list('external_order_no', flat=True))
    natural = _existing_natural_keys('lakala_order')
    for o in orders:
        if o.external_order_no and o.external_order_no in existing_order_nos:
            stats['skipped'] = stats.get('skipped', 0) + 1
            continue
        nk = (o.order_date, o.total_amount, o.counterparty_raw_name, o.direction)
        if not o.external_order_no and nk in natural:
            stats['skipped'] = stats.get('skipped', 0) + 1
            continue
        to_create.append(o)
        if o.external_order_no:
            existing_order_nos.add(o.external_order_no)
        natural.add(nk)

    created = Order.objects.bulk_create(to_create, batch_size=BULK)
    payments = []
    for o in created:
        p = getattr(o, '_pending_payment', None)
        if p is not None:
            p.order = o
            payments.append(p)
    if payments:
        OrderPayment.objects.bulk_create(payments, batch_size=BULK)
    stats['created'] = stats.get('created', 0) + len(created)
    return created


@transaction.atomic
def run_import_lakala(source_type, file_obj, user, tag=''):
    """全量导入（按外部订单号/自然键去重），返回 SourceUpload。"""
    stats = {'skipped': 0, 'no_key': 0, 'no_amount': 0, 'created': 0}
    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
        uploaded_by=user, status='success')
    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as exc:
        upload.status = 'failed'
        upload.note = f'无法解析文件（请用 .xlsx）：{exc}'
        upload.save(update_fields=['status', 'note'])
        return upload

    mappings = load_active_mappings(source_type)
    if not mappings:
        upload.status = 'failed'
        upload.note = '未配置列映射，请在后台为来源「拉卡拉订单」配置 ColumnMappingConfig。'
        upload.save(update_fields=['status', 'note'])
        return upload

    orders = []
    for raw in _iter_lakala_rows(xls, mappings):
        row_obj = RawImportRow.objects.create(source_upload=upload, data=raw,
                                              row_hash=_canon_hash(raw))
        o = _build_order(source_type, raw, mappings, upload, stats)
        if o is not None:
            o.raw_row = row_obj
            orders.append(o)

    _persist(orders, stats)
    upload.row_count = stats['created']
    note_parts = [f'新增 {stats["created"]} 单', f'跳过重复 {stats["skipped"]}']
    if stats['no_key']:
        note_parts.append(f'无户名/单号 {stats["no_key"]}')
    if stats['no_amount']:
        note_parts.append(f'无金额 {stats["no_amount"]}')
    upload.note = '；'.join(note_parts)
    if stats['created'] == 0:
        upload.status = 'empty'
    upload.save(update_fields=['row_count', 'status', 'note'])
    return upload


@transaction.atomic
def run_import_lakala_incremental(source_type, file_obj, user, tag=''):
    """增量导入（按原始行哈希去重）。"""
    stats = {'skipped': 0, 'no_key': 0, 'no_amount': 0, 'created': 0}
    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as exc:
        return SourceUpload.objects.create(
            source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
            uploaded_by=user, status='failed', note=f'无法解析文件：{exc}')

    mappings = load_active_mappings(source_type)
    existing = set(RawImportRow.objects.filter(
        source_upload__source_type=source_type).exclude(row_hash='').values_list('row_hash', flat=True))

    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
        uploaded_by=user, status='success', mode='incremental')

    orders = []
    for raw in _iter_lakala_rows(xls, mappings):
        rh = _canon_hash(raw)
        if rh in existing:
            stats['skipped'] += 1
            continue
        existing.add(rh)
        row_obj = RawImportRow.objects.create(source_upload=upload, data=raw, row_hash=rh)
        o = _build_order(source_type, raw, mappings, upload, stats)
        if o is not None:
            o.raw_row = row_obj
            orders.append(o)

    _persist(orders, stats)
    upload.row_count = stats['created']
    upload.note = f'新增 {stats["created"]} 单；跳过重复行 {stats["skipped"]}'
    if stats['created'] == 0:
        upload.status = 'empty'
    upload.save(update_fields=['row_count', 'status', 'note'])
    return upload
