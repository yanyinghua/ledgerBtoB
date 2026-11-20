"""对账未匹配清单：导出「银行收款但经手人为空」的三份 CSV 工作清单。

用法：
  python manage.py reconcile_unmatched
  python manage.py reconcile_unmatched --output ~/ledger-reconciliation/对账报告
  python manage.py reconcile_unmatched --output /tmp/recon

说明：
  针对主表中 source_type='bank'、经手人(handler)为空、贷方金额(credit)>0 的收款记录
  （默认排除 tag='作废'，与系统「不参与往来统计」规则一致），导出三份用于人工对齐
  经手人/业务员的工作清单：

    1. 未匹配_待对齐清单.csv
       逐笔：银行日期 / 单位名称 / 银行金额(贷) / 业务员表登记笔数 /
             登记候选(日期/金额/经手人)
       「登记候选」取自「资金明细业务员表」(FundReceiptHandler) 中同单位的记录。

    2. 未匹配_缺失单位清单.csv
       按单位汇总：银行空经手人收款笔数 / 合计金额。

    3. 未匹配_仍需手工补登单位.csv
       在 #2 基础上，剔除「已建立现任客户-业务员映射」(CustomerSalesperson.is_current) 的单位，
       剩余即仍需人工补登经手人的单位（状态=需手工补登经手人）。

  输出 UTF-8 BOM（Excel 友好）。--output 指定输出目录，默认写到当前目录 reconcile_unmatched/。
  命令仅读取数据库，不修改任何数据。
"""
import csv
import os
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum

from books.models import MasterRecord, FundReceiptHandler, CustomerSalesperson


class Command(BaseCommand):
    help = '导出「银行收款但经手人为空」的未匹配对账清单（三份 CSV），仅读取不修改'

    def add_arguments(self, parser):
        parser.add_argument('--output', default=None,
                            help='输出目录（默认 ./reconcile_unmatched）')

    def handle(self, *args, **options):
        out_dir = options['output'] or os.path.join(os.getcwd(), 'reconcile_unmatched')
        os.makedirs(out_dir, exist_ok=True)

        # 基础集合：银行收款、经手人为空、贷方>0（作废不计入往来，排除）
        bank_empty = (
            MasterRecord.objects
            .filter(source_type='bank', handler='', credit__gt=0)
            .exclude(tag='作废')
            .order_by('unit_name', 'occur_date')
        )

        # 候选：资金明细业务员表（按单位分组，仅取有经手人的记录才有对齐价值）
        cand = defaultdict(list)
        for unit, od, amt, h in (
            FundReceiptHandler.objects
            .values_list('unit_name', 'occur_date', 'amount', 'handler')
            .order_by('unit_name', 'occur_date')
        ):
            h = h or ''
            if h:
                cand[unit].append((od, amt, h))

        # 1) 待对齐清单（逐笔）
        path1 = os.path.join(out_dir, '未匹配_待对齐清单.csv')
        n1 = 0
        with open(path1, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['银行日期', '单位名称', '银行金额(贷)',
                        '业务员表登记笔数', '登记候选(日期/金额/经手人)'])
            for rec in bank_empty:
                entries = cand.get(rec.unit_name, [])
                cand_str = '; '.join(
                    f'{d.isoformat() if d else ""}/{amt:.2f}/{h}' for d, amt, h in entries
                )
                w.writerow([
                    rec.occur_date.isoformat() if rec.occur_date else '',
                    rec.unit_name,
                    f'{rec.credit:.2f}',
                    len(entries),
                    cand_str,
                ])
                n1 += 1

        # 2) 缺失单位清单（按单位汇总）
        agg = list(
            bank_empty.values('unit_name')
            .annotate(cnt=Count('id'), total=Sum('credit'))
            .order_by('-total')
        )
        path2 = os.path.join(out_dir, '未匹配_缺失单位清单.csv')
        with open(path2, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['单位名称', '银行空经手人收款笔数', '合计金额'])
            for a in agg:
                w.writerow([a['unit_name'], a['cnt'], f'{a["total"]:.2f}'])

        # 3) 仍需手工补登单位（无现任客户-业务员映射者）
        current_units = set(
            CustomerSalesperson.objects.filter(is_current=True)
            .values_list('unit_name', flat=True)
        )
        path3 = os.path.join(out_dir, '未匹配_仍需手工补登单位.csv')
        n3 = 0
        with open(path3, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['单位名称', '银行空经手人收款笔数', '状态'])
            for a in agg:
                if a['unit_name'] in current_units:
                    continue
                w.writerow([a['unit_name'], a['cnt'], '需手工补登经手人'])
                n3 += 1

        self.stdout.write(
            f'已生成 3 份清单到 {out_dir}\n'
            f'  待对齐笔数={n1}，缺失单位数={len(agg)}，仍需手工补登单位数={n3}'
        )
