"""从原始导入行回填订单的经营字段与明细清单（用于历史数据升级）。

- 金额/折扣/退款/店铺/区域：仅在订单当前值为空/0 时回填，不覆盖人工修改与抹零。
- 明细清单：仅为尚无 OrderDetail 的订单补建；按原始行拆分复合列（订单中心 xlsx
  一行含多个明细，CSV 明细级合并行按商品名拆分）。

用法：
  python3 manage.py backfill_order_raw_fields --dry-run
  python3 manage.py backfill_order_raw_fields --username admin
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import Order, OrderDetail, log_operation
from books.imports.utils import to_decimal
from books.imports.lakala import split_detail_values


class Command(BaseCommand):
    help = '从 raw_row 回填订单成本/折扣/退款/店铺/区域与明细清单'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--username', default='', help='记为操作人的用户名（默认首个超管）')
        parser.add_argument('--repair', action='store_true',
                            help='修复历史回填中「无明细id的拆分行沿用首行金额」的错误')

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
        field_count = detail_count = repair_count = 0
        orders = Order.objects.exclude(raw_row=None).select_related('raw_row')
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
            raw = o.raw_row.data or {}
            changed = []
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

            if o.details.exists():
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
