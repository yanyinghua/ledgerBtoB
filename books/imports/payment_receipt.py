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


def _mapped_cols(mappings):
    return {m.source_column for m in mappings}


def _sheet_usable(df, mappings):
    cols = set(str(c) for c in df.columns)
    targets = {m.target_field for m in mappings if m.source_column in cols}
    has_payer = any(t in targets for t in ('payer_name', 'payer_account', 'order_no'))
    has_amount = 'payment_amount' in targets or 'total_amount' in targets
    return has_payer and has_amount


def _iter_rows(xls, mappings):
    mapped_cols = _mapped_cols(mappings)
    for sheet_name in xls.sheet_names:
        df = _read_sheet(xls, sheet_name, mapped_cols, mappings)
        if df is None or df.empty or not _sheet_usable(df, mappings):
            continue
        df = df.where(pd.notna(df), None)
        for _, row in df.iterrows():
            yield {str(k): _serialize(v) for k, v in row.items()}


def _build_receipt(raw, mappings, upload, stats=None):
    f = extract_fields(raw, mappings)
    external_ref = str(f.get('payment_ref', '')).strip()
    payer_name = str(f.get('payer_name', '')).strip()
    payer_account = str(f.get('payer_account', '')).strip()
    amount = to_decimal(f.get('payment_amount')) or to_decimal(f.get('total_amount'))
    if not external_ref and not payer_name and not amount:
        if stats is not None:
            stats['no_key'] = stats.get('no_key', 0) + 1
        return None

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

    # 货主归一
    orr = resolve_owner(platform_owner_id=platform_owner_id, owner_name=owner_name)
    if orr:
        r.owner = orr.obj

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
def run_import_payment_receipt(file_obj, user, incremental=False, tag=''):
    """导入收款明细，返回 SourceUpload。"""
    stats = {'skipped': 0, 'no_key': 0, 'created': 0}
    name = getattr(file_obj, 'name', 'payment_receipt.xlsx')
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

    mappings = load_active_mappings(DETAIL_SOURCE)
    if not mappings:
        upload.status = 'failed'
        upload.note = '未配置列映射，请为来源「收款明细」配置 ColumnMappingConfig。'
        upload.save(update_fields=['status', 'note'])
        return upload

    existing_refs = set(PaymentReceipt.objects.filter(source_type=DETAIL_SOURCE)
                        .values_list('external_ref', flat=True))
    existing_hash = set(RawImportRow.objects.filter(
        source_upload__source_type=DETAIL_SOURCE).exclude(row_hash='')
        .values_list('row_hash', flat=True)) if incremental else set()

    to_create = []
    for raw in _iter_rows(xls, mappings):
        rh = _canon_hash(raw)
        if incremental and rh in existing_hash:
            stats['skipped'] += 1
            continue
        row_obj = RawImportRow.objects.create(source_upload=upload, data=raw, row_hash=rh)
        r = _build_receipt(raw, mappings, upload, stats)
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
    if stats['no_key']:
        parts.append(f'无关键字段 {stats["no_key"]}')
    upload.note = '；'.join(parts)
    if stats['created'] == 0:
        upload.status = 'empty'
    upload.save(update_fields=['row_count', 'status', 'note'])
    return upload
