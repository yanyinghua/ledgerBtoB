"""API 订单注入脚手架：与 Excel 共用 _build_order 构建逻辑。

将来接拉卡拉 webhook 时，视图只需解析 JSON payload 后调用 ingest_order_payload。
"""
import json

from django.db import transaction

from books.models import SourceUpload, RawImportRow, Order, OrderPayment
from books.imports.engine import load_active_mappings, _canon_hash
from books.imports.lakala import _build_order


@transaction.atomic
def ingest_order_payload(payload, user, source='lakala_order_api'):
    """把单个订单 payload dict 注入为 Order，返回 Order（或 None）。

    payload 的 key 应与 ColumnMappingConfig.source_column 一致。
    """
    if not isinstance(payload, dict):
        raise ValueError('payload 必须是 dict')

    upload = SourceUpload.objects.create(
        source_type=source, file_name='api-payload',
        uploaded_by=user, status='success')
    raw = {str(k): ('' if v is None else v) for k, v in payload.items()}
    row_obj = RawImportRow.objects.create(source_upload=upload, data=raw,
                                          row_hash=_canon_hash(raw))
    mappings = load_active_mappings(source)
    if not mappings:
        # 没有 API 专用映射时，回退到 Excel 订单映射
        mappings = load_active_mappings('lakala_order')

    stats = {}
    order = _build_order(source, raw, mappings, upload, stats)
    if order is None:
        upload.status = 'failed'
        upload.note = f'payload 缺少关键字段：{stats}'
        upload.save(update_fields=['status', 'note'])
        return None

    order.raw_row = row_obj
    # API 场景同样按外部单号去重
    if order.external_order_no and Order.objects.filter(
            source_type=source, external_order_no=order.external_order_no).exists():
        upload.status = 'empty'
        upload.note = '外部订单号已存在，跳过'
        upload.save(update_fields=['status', 'note'])
        return None

    order.save()
    p = getattr(order, '_pending_payment', None)
    if p is not None:
        p.order = order
        p.save()
    upload.row_count = 1
    upload.save(update_fields=['row_count'])
    return order
