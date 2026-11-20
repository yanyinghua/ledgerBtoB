"""清理历史已入库的作废发票记录（主表 + 对应货物明细行）。

导入环节（books/imports/engine.py 的 _build_record）现已过滤作废发票，不再入库；
但历史导入的作废记录可能仍留在主表中。本命令用于清理这些历史遗留的作废记录，
使「往来 / 统计」与「导入即过滤」的行为保持一致。

作废的判定与导入过滤保持一致：标记 tag='作废'，或类别 category 属于『作废 / 空白作废』。

用法：
    python3 manage.py clean_void_records            # 仅预览待删记录，不删除（默认安全）
    python3 manage.py clean_void_records --apply    # 真正删除

说明：货物明细行（InvoiceLineItem）通过 master_record 外键关联，删除主表前先按外键
清理其明细行；原始导入行（RawImportRow）作为来源留痕保留，重跑导入时仍会被过滤，故不删除。
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from books.models import MasterRecord, InvoiceLineItem

VOID_CATEGORIES = ('作废', '空白作废')


class Command(BaseCommand):
    help = '清理历史已入库的作废发票记录（主表及对应货物明细行）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正执行删除；缺省仅预览待删记录')

    def handle(self, *args, **options):
        apply = options['apply']

        void_qs = MasterRecord.objects.filter(
            Q(tag='作废') | Q(category__in=VOID_CATEGORIES))
        void_ids = list(void_qs.values_list('id', flat=True))
        master_count = len(void_ids)

        # 对应货物明细行（按外键归属统计，删除主表前清理）
        line_qs = InvoiceLineItem.objects.filter(master_record_id__in=void_ids)
        line_count = line_qs.count()

        self.stdout.write(f'作废主表记录待清理：{master_count} 条')
        for r in void_qs.values('id', 'unit_name', 'tag', 'category',
                                'invoice_number')[:50]:
            self.stdout.write(
                f"   id={r['id']} 单位={r['unit_name']!r} tag={r['tag']!r} "
                f"类别={r['category']!r} 发票号={r['invoice_number']!r}")
        self.stdout.write(f'对应货物明细行待清理：{line_count} 条')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '（预览模式，未做任何删除）确认无误后加 --apply 执行。'))
            return

        if master_count == 0:
            self.stdout.write('没有需要清理的作废记录，主表已干净。')
            return

        deleted_lines, _ = line_qs.delete()
        deleted_masters, _ = void_qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'已清理：主表 {deleted_masters} 条，货物明细行 {deleted_lines} 条。'))
