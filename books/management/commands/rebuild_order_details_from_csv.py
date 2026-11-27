"""从原始明细级 CSV 重建订单明细清单（修复合并导入时丢失的逐项数量/单价/小计）。

只处理原始行来自 CSV（raw_row.data 含「订单成本」）且订单id在文件中命中的订单；
先删除其自动生成的明细，再按 CSV 每行一条明细重建。事务执行，可用 --dry-run 预览。

用法：
  python3 manage.py rebuild_order_details_from_csv --file 订单_11-19_页1.csv --dry-run
  python3 manage.py rebuild_order_details_from_csv --file 订单_11-19_页1.csv
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import Order, OrderDetail, log_operation
from books.imports.utils import to_decimal


FIELD_MAP = {
    'detail_no': '明细Id',
    'goods_id': '商品Id',
    'sku_id': '规格Id',
    'sku': 'SKU编码',
    'barcode': '条形码',
    'goods_name': '商品名',
    'spec': '销售标题',
    'unit': '销售单位',
    'qty': '数量',
    'unit_price': '单价',
    'subtotal': '小计',
    'discount': '折扣',
    'discounted_subtotal': '扣后小计',
    'paid_amount': '实付额',
    'refund_amount': '退款额',
    'refund_qty': '退货数量',
    'owner_id': '货主Id',
}
DECIMAL_FIELDS = {'qty', 'unit_price', 'subtotal', 'discount',
                  'discounted_subtotal', 'paid_amount', 'refund_amount', 'refund_qty'}


class Command(BaseCommand):
    help = '从明细级 CSV 重建 CSV 来源订单的明细清单'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='原始订单明细 CSV')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--username', default='', help='记为操作人的用户名')

    @transaction.atomic
    def handle(self, *args, **options):
        from django.contrib.auth.models import User
        user = User.objects.filter(username=options['username']).first() if options['username'] else None
        if not user:
            user = User.objects.filter(is_superuser=True).first()

        grouped = {}
        try:
            with open(options['file'], encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    oid = str(row.get('订单Id') or '').strip()
                    if oid:
                        grouped.setdefault(oid, []).append(dict(row))
        except OSError as exc:
            raise CommandError(f'无法读取 CSV：{exc}')
        if not grouped:
            raise CommandError('CSV 中没有可识别的订单明细')

        orders = Order.objects.exclude(raw_row=None).select_related('raw_row').filter(
            external_order_no__in=list(grouped.keys()))
        rebuilt_orders = rebuilt_rows = 0
        for o in orders:
            raw = o.raw_row.data or {}
            if '订单成本' not in raw:  # 只处理 CSV 来源，xlsx 来源解析已正确
                continue
            rows = grouped[o.external_order_no]
            details = []
            for i, row in enumerate(rows):
                values = {}
                for field, source in FIELD_MAP.items():
                    v = str(row.get(source) or '').strip()
                    values[field] = to_decimal(v) if field in DECIMAL_FIELDS else v
                details.append(OrderDetail(order=o, row_index=i, raw_data=row, **values))
            if not details:
                continue
            if not options['dry_run']:
                o.details.all().delete()
                OrderDetail.objects.bulk_create(details, batch_size=500)
            rebuilt_orders += 1
            rebuilt_rows += len(details)

        if not options['dry_run'] and rebuilt_orders:
            log_operation(user, '重建订单明细', target_type='Order', target_id='',
                          detail=f'{rebuilt_orders} 单 / {rebuilt_rows} 行（来源 CSV）')
        self.stdout.write(self.style.SUCCESS(
            f'重建明细：{rebuilt_orders} 单 / {rebuilt_rows} 行'
            + ('（dry-run，未写库）' if options['dry_run'] else '')))
