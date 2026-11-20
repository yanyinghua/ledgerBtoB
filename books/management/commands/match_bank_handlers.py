"""银行流水经手人匹配：按「资金明细业务员表」回填。

匹配口径（业务确认）：
  - 来源是 FundReceiptHandler（独立表，源自「业务员资金流.xlsx」，员工手工登记银行收款对应的经手人），
    每行含 时间 / 单位 / 金额 / 经手人，导入时已分别归一化到 occur_date / unit_name / amount / handler。
  - 以 (occur_date, unit_name, 金额) 三列完全一致为匹配键。
  - 仅填充当前 handler 为空的 bank 行，不覆盖已有值（含管理员手动补填）。
  - 手工登记若把时间记错而三列无法对齐，则匹配不上，留待查询时手动补。

特性：幂等。重复运行只填新出现的空行；资金表后续修正（改对时间）后重跑即可补上。

参数：
  --dry-run  仅预览，不写库。
  --show-unmatched N  额外列出前 N 条未匹配的 bank 空行（供人工核查）。
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand

from books.models import MasterRecord, FundReceiptHandler

CENT = Decimal('0.01')


def _effective_amount(rec):
    """有效金额：收款=贷方，取绝对值并量到 2 位小数；无金额返回 None。"""
    credit = rec.credit or Decimal('0')
    debit = rec.debit or Decimal('0')
    amt = credit if credit > 0 else debit
    if amt == 0:
        return None
    return abs(amt).quantize(CENT, rounding=ROUND_HALF_UP)


def _build_source_index():
    """从 FundReceiptHandler(资金明细业务员表) 建 (日期,单位,金额) -> 经手人 索引。"""
    index = {}
    conflicts = 0
    for rec in FundReceiptHandler.objects.exclude(handler='').only(
            'occur_date', 'unit_name', 'amount', 'handler'):
        handler = (rec.handler or '').strip()
        if not handler:
            continue
        if not rec.occur_date or not rec.unit_name:
            continue
        amt = rec.amount
        if amt is None or amt == 0:
            continue
        key = (rec.occur_date, rec.unit_name, abs(amt).quantize(CENT, rounding=ROUND_HALF_UP))
        if key in index:
            if index[key] != handler:
                conflicts += 1
            # 同键不同经手人：保留首个，不覆盖
            continue
        index[key] = handler
    return index, conflicts


class Command(BaseCommand):
    help = '按资金明细业务员表(收款登记)匹配银行流水【收款】经手人，仅填空行'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--show-unmatched', type=int, default=0,
                            help='额外列出前 N 条未匹配的 bank 空行供核查')

    def handle(self, *args, **options):
        dry = options['dry_run']
        show_unmatched = options.get('show_unmatched') or 0

        index, conflicts = _build_source_index()
        self.stdout.write(f'来源(资金明细业务员表)有效索引条数：{len(index)}；'
                          f'同键冲突：{conflicts}')

        # 仅对银行收款行(credit>0)做匹配：资金明细业务员表登记的是收款，付款行不参与
        total_empty = MasterRecord.objects.filter(
            source_type='bank', handler='', credit__gt=0).count()
        matched, unmatched = [], 0
        samples = []

        qs = MasterRecord.objects.filter(
            source_type='bank', handler='', credit__gt=0).only(
                'occur_date', 'unit_name', 'debit', 'credit', 'handler')
        for rec in qs:
            if not rec.occur_date or not rec.unit_name:
                unmatched += 1
                if len(samples) < show_unmatched:
                    samples.append(rec)
                continue
            amt = _effective_amount(rec)
            if amt is None:
                unmatched += 1
                if len(samples) < show_unmatched:
                    samples.append(rec)
                continue
            key = (rec.occur_date, rec.unit_name, amt)
            handler = index.get(key)
            if handler:
                rec.handler = handler
                matched.append(rec)
            else:
                unmatched += 1
                if len(samples) < show_unmatched:
                    samples.append(rec)

        if dry:
            self.stdout.write(self.style.WARNING(
                f'[预览] bank 空行={total_empty} 可匹配={len(matched)} '
                f'未匹配={unmatched}；未写库。'))
        else:
            if matched:
                MasterRecord.objects.bulk_update(matched, ['handler'])
            self.stdout.write(self.style.SUCCESS(
                f'已回填 bank 经手人：{len(matched)} 条；未匹配：{unmatched} 条。'))

        if show_unmatched and samples:
            self.stdout.write(self.style.WARNING(
                f'\n未匹配样本（前 {len(samples)} 条，供人工补登）：'))
            for r in samples:
                amt = _effective_amount(r)
                self.stdout.write(
                    f'  id={r.id} 日期={r.occur_date} 单位={r.unit_name} '
                    f'金额={amt}')
