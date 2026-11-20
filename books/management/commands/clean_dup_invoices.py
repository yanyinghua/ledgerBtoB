"""清理「发票号为空」的重复发票主表记录。

成因：早期导入（或线上列映射尚未覆盖『数电发票号码』时）入库的那批记录发票号为空；
后来重新导入同一文件时，判重自然键含发票号，空票号的历史行匹不上，于是同一批发票
在主表里留下两份——一份有票号（正常）、一份票号为空（重复计入往来与统计）。

判定（保守）：来源相同，且 (开票日期, 单位名称, 借方, 贷方, 摘要) 完全一致，
其中一条发票号为空、另一条有发票号 —— 只删发票号为空的那条，保留有票号的记录。

用法：
    python3 manage.py clean_dup_invoices                       # 预览（默认不删）
    python3 manage.py clean_dup_invoices --apply               # 真正删除
    python3 manage.py clean_dup_invoices --source-type sales_invoice --apply

说明：
- 删除前把待删记录导出 CSV 备份（默认 exports/dup_invoice_<时间戳>.csv）。
- 货物明细行按外键归属一并处理：幸存记录已有明细则删除重复记录的明细；
  若幸存记录没有明细，则把明细改挂到幸存记录，避免明细丢失。
- 原始导入行（RawImportRow）作为来源留痕，不删除。
"""
import csv
import os
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from books.models import MasterRecord, InvoiceLineItem

# 判定重复的比对字段（不含发票号：空票号行正是靠发票号以外的信息来配对）
TWIN_FIELDS = ('occur_date', 'unit_name', 'debit', 'credit', 'remark')

BACKUP_HEADERS = ['id', '期间', '开票日期', '单位名称', '借方', '贷方', '摘要',
                  '类别', '标记', '经手人', '来源类型']


class Command(BaseCommand):
    help = '清理发票号为空且与有票号记录重复的发票主表记录（默认预览，--apply 才删除）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正执行删除；缺省仅预览待删记录')
        parser.add_argument('--source-type', default='purchase_invoice',
                            help='来源类型（默认 purchase_invoice 进项；'
                                 'sales_invoice 销项）')
        parser.add_argument('--backup', default=None,
                            help='备份 CSV 路径（默认 exports/dup_invoice_<时间戳>.csv）')

    def _twin_map(self, source_type):
        """有票号记录的 (日期,单位,借贷,摘要) -> id 映射（同键取 id 最小的一条）。"""
        valid = (MasterRecord.objects.filter(source_type=source_type)
                 .exclude(invoice_number='').order_by('id')
                 .values_list('id', *TWIN_FIELDS))
        twin = {}
        for mid, *key in valid:
            twin.setdefault(tuple(key), mid)
        return twin

    def handle(self, *args, **options):
        apply = options['apply']
        source_type = options['source_type']

        twin = self._twin_map(source_type)
        dup_ids, id_to_twin = [], {}
        empty_qs = (MasterRecord.objects.filter(source_type=source_type, invoice_number='')
                    .order_by('id').values_list('id', *TWIN_FIELDS))
        for mid, *key in empty_qs:
            survivor = twin.get(tuple(key))
            if survivor:
                dup_ids.append(mid)
                id_to_twin[mid] = survivor

        dup_qs = MasterRecord.objects.filter(id__in=dup_ids)
        line_count = InvoiceLineItem.objects.filter(master_record_id__in=dup_ids).count()

        self.stdout.write(
            f'来源 {source_type}：有票号记录 {len(twin)} 组键，'
            f'发票号为空 {MasterRecord.objects.filter(source_type=source_type, invoice_number="").count()} 条，'
            f'其中可确认重复 {len(dup_ids)} 条')
        for r in (dup_qs.values('period').annotate(c=Count('id')).order_by('period')):
            self.stdout.write(f"   期间 {r['period']}：{r['c']} 条")
        for r in dup_qs.values('id', 'period', 'occur_date', 'unit_name', 'debit', 'credit')[:20]:
            self.stdout.write(
                f"   id={r['id']} {r['period']} {r['occur_date']} {r['unit_name']!r} "
                f"借{r['debit']} 贷{r['credit']}")
        self.stdout.write(f'关联货物明细行：{line_count} 条')

        if not dup_ids:
            self.stdout.write('没有可确认的重复记录，无需清理。')
            return

        if not apply:
            self.stdout.write(self.style.WARNING(
                '（预览模式，未做任何删除）确认无误后加 --apply 执行。'))
            return

        backup = options['backup'] or os.path.join(
            str(getattr(settings, 'BASE_DIR', '.')), 'exports',
            f"dup_invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(backup, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(BACKUP_HEADERS)
            for r in dup_qs.values_list('id', 'period', 'occur_date', 'unit_name', 'debit',
                                        'credit', 'remark', 'category', 'tag', 'handler',
                                        'source_type'):
                w.writerow(list(r))
        self.stdout.write(f'已备份 {len(dup_ids)} 条到 {backup}')

        moved = 0
        with transaction.atomic():
            for dup_id in dup_ids:
                lines = InvoiceLineItem.objects.filter(master_record_id=dup_id)
                if not lines.exists():
                    continue
                survivor = id_to_twin[dup_id]
                if InvoiceLineItem.objects.filter(master_record_id=survivor).exists():
                    lines.delete()
                else:
                    n = lines.count()
                    lines.update(master_record_id=survivor)
                    moved += n
            deleted, _ = dup_qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'已清理重复主表 {deleted} 条'
            + (f'；{moved} 条明细已改挂到保留记录' if moved else '')
            + '。'))
