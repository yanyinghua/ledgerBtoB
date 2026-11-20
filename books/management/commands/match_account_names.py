"""批量户名匹配：对未匹配/待复核订单跑匹配引擎并回写 4 目标。

  python3 manage.py match_account_names --dry-run
  python3 manage.py match_account_names              # 实际回写
"""
from django.core.management.base import BaseCommand

from books.models import Order, log_operation
from books.matching.engine import resolve_account_name


class Command(BaseCommand):
    help = '批量解析未匹配/待复核订单的户名并回填匹配结果'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0, help='最多处理 N 单（0=不限）')

    def handle(self, *args, **options):
        qs = Order.objects.filter(match_status__in=['未匹配', '待复核']).order_by('id')
        if options['limit']:
            qs = qs[:options['limit']]
        updated = 0
        for order in qs:
            result = resolve_account_name(
                order.counterparty_raw_name,
                merchant=order.merchant_id,
                account_scope=order.receiving_account_id,
                direction=order.direction, bump_usage=False)
            if options['dry_run']:
                if result.status != '未匹配':
                    updated += 1
                continue
            changed = False
            if result.counterparty and order.counterparty_id != result.counterparty.pk:
                order.counterparty = result.counterparty; changed = True
            if result.salesperson and order.salesperson_id != result.salesperson.pk:
                order.salesperson = result.salesperson; changed = True
            if result.department and order.department_id != result.department.pk:
                order.department = result.department; changed = True
            if result.merchant and order.merchant_id != result.merchant.pk:
                order.merchant = result.merchant; changed = True
            if result.receiving_account and order.receiving_account_id != result.receiving_account.pk:
                order.receiving_account = result.receiving_account; changed = True
            if order.match_status != result.status or order.match_confidence != result.confidence:
                order.match_status = result.status
                order.match_confidence = result.confidence
                changed = True
            if changed:
                order.save()
                updated += 1
                log_operation(None, '户名匹配', target_type='Order', target_id=str(order.pk),
                              detail=f'{order.counterparty_raw_name} → {result.status} ({result.source})',
                              username='系统(match_account_names)')
        prefix = '[预览] ' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}匹配更新 {updated} 单（共扫描 {qs.count()} 单）'))
