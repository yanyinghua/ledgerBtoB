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
    SourceUpload, RawImportRow, Order, OrderPayment, OrderDetail, Counterparty,
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


def _iter_csv_rows(file_obj, mappings):
    """CSV（含 UTF-8 BOM）读取：逐行产出原始 dict，兼容 xlsx 的 sheet 抽象。"""
    try:
        file_obj.seek(0)
    except Exception:
        pass
    try:
        df = pd.read_csv(file_obj, dtype=str, keep_default_na=False)
    except Exception:
        try:
            file_obj.seek(0)
        except Exception:
            pass
        df = pd.read_csv(file_obj, dtype=str, keep_default_na=False, encoding='gbk')
    if df.empty or not _sheet_usable_lakala(df, mappings):
        return
    for _, row in df.iterrows():
        yield {str(k): _serialize(v) for k, v in row.items()}


def iter_raw_rows(file_obj, mappings):
    """按文件扩展名选择读取方式：.csv → CSV；其余 → Excel。"""
    name = getattr(file_obj, 'name', '').lower()
    if name.endswith('.csv'):
        yield from _iter_csv_rows(file_obj, mappings)
    else:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
        yield from _iter_lakala_rows(xls, mappings)


def merge_detail_rows(raws, mappings):
    """明细级 CSV：按订单号分组，产出 (订单头, 明细行列表)。

    订单头字段（order_no/客户/金额/日期等）在明细行重复，取第一行；
    goods_summary 仍收集所有行的商品名去重拼接（兼容旧导出/API）；
    明细行完整保留，供 OrderDetail 落库与按订单id穿透。
    """
    order_cols = [m.source_column for m in mappings if m.target_field == 'order_no']
    goods_cols = [m.source_column for m in mappings if m.target_field == 'goods_summary']
    merged = {}
    order_seq = []
    for raw in raws:
        key = ''
        for c in order_cols:
            if str(raw.get(c, '')).strip():
                key = str(raw[c]).strip()
                break
        if not key:
            key = _canon_hash(raw)  # 无订单号则各行独立
        if key not in merged:
            merged[key] = (dict(raw), [])
            order_seq.append(key)
        else:
            head, details = merged[key]
            for gc in goods_cols:
                g = str(raw.get(gc, '')).strip()
                if g and g not in str(head.get(gc, '')):
                    head[gc] = (str(head.get(gc, '')).strip() + '，' + g).strip('，')
        merged[key][1].append(dict(raw))
    for key in order_seq:
        head, details = merged[key]
        yield head, details


def _infer_direction(fields, raw):
    d = str(fields.get('direction_raw', '')).strip()
    if '采购' in d or '进货' in d or ('付款' in d and '销售' not in d):
        return '采购'
    if '销售' in d or '出货' in d or '收款' in d:
        return '销售'
    return '销售'


def _build_order(source_type, raw, mappings, upload, stats=None, details=None):
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
    cost = to_decimal(fields.get('cost_amount'))
    # 零单价赠品/促销单：金额与实收均为 0，但有真实成本，应入库并在毛利中体现。
    if total == 0 and paid == 0 and payment_amount == 0 and cost == 0:
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
        # 店铺往来账：店铺与经营口径字段
        shop_name=str(fields.get('shop_name', '')).strip() or raw_name,
        goods_amount=to_decimal(fields.get('goods_amount')),
        discount_amount=to_decimal(fields.get('discount_amount')),
        refund_amount=to_decimal(fields.get('refund_amount')),
        cost_amount=to_decimal(fields.get('cost_amount')),
        region=str(fields.get('region', '')).strip(),
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

    # 业务主管：按订单归属部门的负责人快照（导入后仍可在订单上手改）
    if order.department_id and order.department.manager_id:
        mgr = order.department.manager
        order.supervisor = (mgr.get_full_name() or mgr.username).strip()

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

    # 明细行：逐行抽取 detail_* 字段，暂存待订单落库后回填 FK
    pending_details = []
    for idx, drow in enumerate(details or []):
        df = extract_fields(drow, mappings)
        pending_details.append(OrderDetail(
            detail_no=str(df.get('detail_no', '')).strip(),
            goods_id=str(df.get('detail_goods_id', '')).strip(),
            sku_id=str(df.get('detail_sku_id', '')).strip(),
            sku=str(df.get('detail_sku', '')).strip(),
            barcode=str(df.get('detail_barcode', '')).strip(),
            goods_name=str(df.get('detail_goods_name', '')).strip(),
            spec=str(df.get('detail_spec', '')).strip(),
            unit=str(df.get('detail_unit', '')).strip(),
            qty=to_decimal(df.get('detail_qty')),
            unit_price=to_decimal(df.get('detail_unit_price')),
            subtotal=to_decimal(df.get('detail_subtotal')),
            discount=to_decimal(df.get('detail_discount')),
            discounted_subtotal=to_decimal(df.get('detail_discounted_subtotal')),
            paid_amount=to_decimal(df.get('detail_paid')),
            refund_amount=to_decimal(df.get('detail_refund')),
            refund_qty=to_decimal(df.get('detail_refund_qty')),
            owner_id=str(df.get('detail_owner_id', '')).strip(),
            row_index=idx,
            raw_data=drow,
        ))
    order._pending_details = pending_details

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
    details = []
    for o in created:
        for d in getattr(o, '_pending_details', []):
            d.order = o
            details.append(d)
    if details:
        # 同一订单内按 (明细Id 或行序) 去重，避免重复行落库
        seen = set()
        unique = []
        for d in details:
            key = (d.order_id, d.detail_no or f'row-{d.row_index}')
            if key in seen:
                continue
            seen.add(key)
            unique.append(d)
        OrderDetail.objects.bulk_create(unique, batch_size=BULK)
        stats['details'] = stats.get('details', 0) + len(unique)
    stats['created'] = stats.get('created', 0) + len(created)
    return created


@transaction.atomic
def run_import_lakala(source_type, file_obj, user, tag=''):
    """全量导入（按外部订单号/自然键去重），返回 SourceUpload。"""
    stats = {'skipped': 0, 'no_key': 0, 'no_amount': 0, 'created': 0}
    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
        uploaded_by=user, status='success')

    mappings = load_active_mappings(source_type)
    if not mappings:
        upload.status = 'failed'
        upload.note = '未配置列映射，请在后台为来源「拉卡拉订单」配置 ColumnMappingConfig。'
        upload.save(update_fields=['status', 'note'])
        return upload

    try:
        raws = list(iter_raw_rows(file_obj, mappings))
    except Exception as exc:
        upload.status = 'failed'
        upload.note = f'无法解析文件（请用 .xlsx/.csv）：{exc}'
        upload.save(update_fields=['status', 'note'])
        return upload

    orders = []
    for head, details in merge_detail_rows(raws, mappings):
        row_obj = RawImportRow.objects.create(source_upload=upload, data=head,
                                              row_hash=_canon_hash(head))
        o = _build_order(source_type, head, mappings, upload, stats, details=details)
        if o is not None:
            o.raw_row = row_obj
            orders.append(o)

    _persist(orders, stats)
    upload.row_count = stats['created']
    note_parts = [f'新增 {stats["created"]} 单', f'明细 {stats.get("details", 0)} 行',
                  f'跳过重复 {stats["skipped"]}']
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
    mappings = load_active_mappings(source_type)
    try:
        raws = list(iter_raw_rows(file_obj, mappings))
    except Exception as exc:
        return SourceUpload.objects.create(
            source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
            uploaded_by=user, status='failed', note=f'无法解析文件：{exc}')

    existing = set(RawImportRow.objects.filter(
        source_upload__source_type=source_type).exclude(row_hash='').values_list('row_hash', flat=True))

    upload = SourceUpload.objects.create(
        source_type=source_type, file_name=getattr(file_obj, 'name', 'lakala.xlsx'),
        uploaded_by=user, status='success', mode='incremental')

    orders = []
    for head, details in merge_detail_rows(raws, mappings):
        rh = _canon_hash(head)
        if rh in existing:
            stats['skipped'] += 1
            continue
        existing.add(rh)
        row_obj = RawImportRow.objects.create(source_upload=upload, data=head, row_hash=rh)
        o = _build_order(source_type, head, mappings, upload, stats, details=details)
        if o is not None:
            o.raw_row = row_obj
            orders.append(o)

    _persist(orders, stats)
    upload.row_count = stats['created']
    upload.note = (f'新增 {stats["created"]} 单；明细 {stats.get("details", 0)} 行；'
                   f'跳过重复行 {stats["skipped"]}')
    if stats['created'] == 0:
        upload.status = 'empty'
    upload.save(update_fields=['row_count', 'status', 'note'])
    return upload
