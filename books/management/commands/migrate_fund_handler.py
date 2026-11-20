"""一次性迁移：把主表中 source_type='other'（资金明细业务员表）行迁到独立表 FundReceiptHandler，
并删除主表重复行。

背景：资金明细业务员表（业务员资金流.xlsx）之前被当作主表交易行(source_type='other')导入，
与银行流水(收款)重复。现独立为 FundReceiptHandler，匹配/派生逻辑已改读新表，故把存量数据迁过去后删主表重复行。

幂等：先清空 FundReceiptHandler 再全量写入，删除主表 other 行。
参数：--dry-run 仅预览不写库。
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand

from books.models import MasterRecord, FundReceiptHandler
from books.imports.utils import normalize_unit_name

CENT = Decimal('0.01')


class Command(BaseCommand):
    help = '把主表 other(资金明细业务员表) 迁到 FundReceiptHandler 并删除主表重复行'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')

    def handle(self, *args, **options):
        dry = options['dry_run']
        src = MasterRecord.objects.filter(source_type='other')
        total = src.count()

        rows = []
        for r in src.only('occur_date', 'unit_name', 'debit', 'credit', 'handler', 'remark', 'source_upload'):
            unit_raw = (r.unit_name or '').strip()
            if not unit_raw:
                continue
            unit_name = normalize_unit_name(unit_raw, None)
            credit = r.credit or Decimal('0')
            debit = r.debit or Decimal('0')
            amt = credit if credit > 0 else debit
            if amt == 0:
                continue
            rows.append(FundReceiptHandler(
                source_upload=r.source_upload,
                occur_date=r.occur_date,
                unit_name=unit_name,
                amount=abs(amt).quantize(CENT, rounding=ROUND_HALF_UP),
                handler=(r.handler or '').strip(),
                remark=(r.remark or '')))

        if dry:
            self.stdout.write(self.style.WARNING(
                f'[预览] 主表 other 行={total}，可迁移={len(rows)}；不写库。'))
            return

        FundReceiptHandler.objects.all().delete()
        FundReceiptHandler.objects.bulk_create(rows)
        deleted, _ = src.delete()
        self.stdout.write(self.style.SUCCESS(
            f'已迁移 {len(rows)} 条到 FundReceiptHandler；删除主表 other 重复行 {deleted} 条。'))
