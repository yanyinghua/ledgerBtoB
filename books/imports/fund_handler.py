"""资金明细业务员表导入：把「业务员资金流.xlsx」导入独立表 FundReceiptHandler。

为何独立：该表是员工手工登记的银行收款经手人，其每行与银行流水(收款)是同一笔交易，
若作为主表(source_type='other')导入会与银行流水重复。故单独存，供
match_bank_handlers(银行流水经手人匹配) 与 salesperson(客户-业务员映射) 使用。

列解析复用「other」的列映射配置(ColumnMappingConfig)，避免重复维护列名。
每次导入为整表替换语义（该文件即最新登记全集）。
"""
import pandas as pd
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction

from books.models import SourceUpload, FundReceiptHandler
from books.imports.engine import load_active_mappings, _iter_rows, extract_fields
from books.imports.utils import normalize_unit_name

CENT = Decimal('0.01')


def _effective_amount(debit, credit):
    """有效金额：收款=贷方，取绝对值并量到 2 位小数；无金额返回 None。"""
    credit = credit or Decimal('0')
    debit = debit or Decimal('0')
    amt = credit if credit > 0 else debit
    if amt == 0:
        return None
    return abs(amt).quantize(CENT, rounding=ROUND_HALF_UP)


@transaction.atomic
def import_fund_handler(file_obj, user, clear=True):
    """读取 xlsx 写入 FundReceiptHandler。返回 SourceUpload。"""
    file_name = getattr(file_obj, 'name', '业务员资金流.xlsx')
    try:
        xls = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception as e:  # noqa: BLE001
        return SourceUpload.objects.create(
            source_type='fund_handler', file_name=file_name, uploaded_by=user,
            status='failed', note=f'读取 Excel 失败：{e}')

    mappings = load_active_mappings('other')
    if not mappings:
        return SourceUpload.objects.create(
            source_type='fund_handler', file_name=file_name, uploaded_by=user,
            status='failed', note='尚未配置「other」列映射，无法解析资金明细业务员表。')
    mapped_cols = {m.source_column for m in mappings}

    upload = SourceUpload.objects.create(
        source_type='fund_handler', file_name=file_name, uploaded_by=user, status='success')

    rows = []
    for raw in _iter_rows(xls, mappings, mapped_cols):
        fields = extract_fields(raw, mappings)
        unit_raw = (fields.get('unit_name') or '').strip()
        if not unit_raw:
            continue
        unit_name = normalize_unit_name(unit_raw, fields.get('tax_no'))
        handler = (fields.get('handler') or '').strip()
        date = fields.get('occur_date')
        amt = _effective_amount(fields.get('debit'), fields.get('credit'))
        if amt is None:
            continue
        rows.append(FundReceiptHandler(
            source_upload=upload, occur_date=date, unit_name=unit_name,
            amount=amt, handler=handler, remark=(fields.get('remark') or '')))

    if clear:
        FundReceiptHandler.objects.all().delete()
    FundReceiptHandler.objects.bulk_create(rows)
    upload.row_count = len(rows)
    upload.note = f'资金明细业务员表导入 {len(rows)} 行'
    upload.save()
    return upload
