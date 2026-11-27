"""从原始导入行回填订单的经营字段与明细清单（用于历史数据升级）。

- 金额/折扣/退款/店铺/区域：仅在订单当前值为空/0 时回填，不覆盖人工修改与抹零。
- 明细清单：仅为尚无 OrderDetail 的订单补建；按原始行拆分复合列（订单中心 xlsx
  一行含多个明细，CSV 明细级合并行按商品名拆分）。

用法：
  python3 manage.py backfill_order_raw_fields --dry-run
  python3 manage.py backfill_order_raw_fields --username admin
  python3 manage.py backfill_order_raw_fields --csv 订单.csv --xlsx 订单中心.xlsx
"""
import csv

import openpyxl
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import Order, OrderDetail, log_operation
from books.imports.utils import to_decimal
from books.imports.lakala import split_detail_values


def _load_source_heads(csv_path, xlsx_path):
    """从原始文件按订单id读取表头字段，供无 raw_row 的历史订单回填。"""
    heads = {}
    if csv_path:
        with open(csv_path, encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                oid = str(row.get('订单Id') or '').strip()
                if oid:
                    heads.setdefault(oid, dict(row))
    if xlsx_path:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        hdr = [str(c).strip() if c is not None else '' for c in next(it)]
        i_id = hdr.index('id') if 'id' in hdr else None
        if i_id is not None:
            for row in it:
                oid = str(row[i_id]).strip() if i_id < len(row) and row[i_id] else ''
                if not oid:
                    continue
                oid = oid.split('.')[0]
                heads.setdefault(oid, {h: row[i] for i, h in enumerate(hdr)
                                       if i < len(row) and h})
        wb.close()
    return heads


class Command(BaseCommand):
    help = '从 raw_row 回填订单成本/折扣/退款/店铺/区域与明细清单'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--username', default='', help='记为操作人的用户名（默认首个超管）')
        parser.add_argument('--repair', action='store_true',
                            help='修复历史回填中「无明细id的拆分行沿用首行金额」的错误')
        parser.add_argument('--csv', default='', help='原始明细级 CSV，用于无 raw_row 的历史订单')
        parser.add_argument('--xlsx', default='', help='订单中心 xlsx，用于无 raw_row 的历史订单')

    @transaction.atomic
    def handle(self, *args, **options):
        user = None
        if options['username']:
            user = User.objects.filter(username=options['username']).first()
            if not user:
                raise CommandError(f'用户不存在：{options["username"]}')
        if not user:
            user = User.objects.filter(is_superuser=True).first()

        dry = options['dry_run']
        source_heads = _load_source_heads(options['csv'], options['xlsx'])
        field_count = detail_count = repair_count = 0
        orders = Order.objects.all().select_related('raw_row')
        if options['repair']:
            for o in orders:
                ds = list(o.details.order_by('row_index'))
                if len(ds) < 2:
                    continue
                first = ds[0]
                for d in ds[1:]:
                    if (not d.detail_no and d.qty == first.qty and
                            d.unit_price == first.unit_price and
                            d.subtotal == first.subtotal and
                            (d.qty or d.unit_price or d.subtotal)):
                        if not dry:
                            d.qty = d.unit_price = d.subtotal = 0
                            d.save(update_fields=['qty', 'unit_price', 'subtotal'])
                        repair_count += 1
            self.stdout.write(self.style.SUCCESS(
                f'修复重复金额明细行：{repair_count} 行'
                + ('（dry-run，未写库）' if dry else '')))
            if dry:
                return
        for o in orders:
            from_source = not o.raw_row_id
            raw = (o.raw_row.data or {}) if o.raw_row_id else source_heads.get(o.external_order_no, {})
            if not raw:
                continue
            changed = []
            goods = to_decimal(raw.get('商品总额'))
            if not o.goods_amount and goods:
                o.goods_amount = goods; changed.append('goods_amount')
            cost = to_decimal(raw.get('订单成本')) or to_decimal(raw.get('总成本'))
            if not o.cost_amount and cost:
                o.cost_amount = cost; changed.append('cost_amount')
            discount = to_decimal(raw.get('总折扣'))
            if not o.discount_amount and discount:
                o.discount_amount = discount; changed.append('discount_amount')
            refund = (to_decimal(raw.get('线上支付-退款额')) +
                      to_decimal(raw.get('到货付款-退货额')))
            if not refund:
                refund = to_decimal(raw.get('退货额'))
            if not o.refund_amount and refund:
                o.refund_amount = refund; changed.append('refund_amount')
            shop = str(raw.get('客户名称') or raw.get('客户Id') or '').strip()
            if not o.shop_name and shop:
                o.shop_name = shop; changed.append('shop_name')
            region = str(raw.get('片区') or '').strip()
            if not o.region and region:
                o.region = region; changed.append('region')
            if changed and not dry:
                o.save(update_fields=changed + ['updated_at'])
            if changed:
                field_count += 1

            # 无 raw_row 的历史订单明细已由 rebuild_order_details_from_csv 重建，不再补建
            if o.details.exists() or from_source:
                continue
            base = {
                'detail_no': str(raw.get('明细id') or raw.get('明细Id') or '').strip(),
                'goods_name': str(raw.get('商品名') or '').strip(),
                'sku': str(raw.get('规格编码') or raw.get('SKU编码') or '').strip(),
                'barcode': str(raw.get('条码') or raw.get('条形码') or '').strip(),
                'qty': to_decimal(raw.get('数量')),
                'unit_price': to_decimal(raw.get('单价')),
                'subtotal': to_decimal(raw.get('小计')),
                'sales_title': str(raw.get('销售标题') or ''),
            }
            parts = split_detail_values(base)
            for p in parts:
                p.pop('sales_title', None)
            if parts and not dry:
                rows = [OrderDetail(order=o, row_index=i, raw_data=raw, **p)
                        for i, p in enumerate(parts)]
                OrderDetail.objects.bulk_create(rows, batch_size=500)
            if parts:
                detail_count += 1

        if not dry and (field_count or detail_count):
            log_operation(user, '回填订单原始字段', target_type='Order', target_id='',
                          detail=f'字段 {field_count} 单；明细 {detail_count} 单')
        self.stdout.write(self.style.SUCCESS(
            f'回填完成：字段 {field_count} 单，明细 {detail_count} 单'
            + ('（dry-run，未写库）' if dry else '')))
