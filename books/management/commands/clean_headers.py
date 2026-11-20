"""清理被误当数据导入的「表头行」。

表头行泄漏的特征：单位名称单元格的取值等于某个表头列名（如
「购买方名称」「销方名称」「对方户名」「单位名称」等），且通常 year=0、借贷为 0。
本命令删除主表中 unit_name 命中表头列名的伪记录，并同步删除对应的原始行暂存
（RawImportRow 中单位名列取值命中表头列名的行），保持主表与原始行一致。

用法：
    python3 manage.py clean_headers            # 执行清理
    python3 manage.py clean_headers --dry-run  # 仅预览待删记录，不真正删除

说明：引擎（books/imports/engine.py 的 _iter_rows）已加表头防护，后续导入不会再产生
此类伪记录；本命令用于清理历史遗留。
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from books.models import MasterRecord, RawImportRow, ColumnMappingConfig


class Command(BaseCommand):
    help = '清理被误当数据导入的表头行（单位名等于表头列名）'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='只预览待删记录，不真正删除')

    def handle(self, *args, **options):
        dry = options['dry_run']

        # 全部表头列名（来自后台列映射配置）
        labels = {str(c) for c in ColumnMappingConfig.objects.values_list('source_column', flat=True)}
        if not labels:
            self.stderr.write('未找到任何列映射配置，无法判定表头列名，已中止。')
            return

        # 各来源的单位名对应原始列
        unit_cols_by_src = {}
        for st in set(ColumnMappingConfig.objects.values_list('source_type', flat=True)):
            unit_cols_by_src[st] = [m.source_column for m in
                                    ColumnMappingConfig.objects.filter(source_type=st, target_field='unit_name')]

        # ---- 主表伪记录 ----
        m_qs = MasterRecord.objects.filter(unit_name__in=list(labels))
        m_count = m_qs.count()
        self.stdout.write(f'主表待清理（单位名命中表头列名）：{m_count} 条')
        for r in m_qs.values('id', 'source_type', 'unit_name', 'year')[:50]:
            self.stdout.write(f"   id={r['id']} {r['source_type']} unit={r['unit_name']!r} year={r['year']}")

        # ---- 原始行伪记录 ----
        raw_ids = []
        for r in RawImportRow.objects.select_related('source_upload').all():
            st = r.source_upload.source_type
            ucs = unit_cols_by_src.get(st, [])
            if not ucs:
                continue
            data = r.data if isinstance(r.data, dict) else {}
            vals = {str(data.get(uc, '')) for uc in ucs}
            if vals & labels:
                raw_ids.append(r.id)
        r_count = len(raw_ids)
        self.stdout.write(f'原始行待清理（单位名命中表头列名）：{r_count} 条')

        if dry:
            self.stdout.write(self.style.WARNING('（dry-run）未做任何删除。'))
            return

        if m_count == 0 and r_count == 0:
            self.stdout.write(self.style.SUCCESS('没有需要清理的表头伪记录，主表已干净。'))
            return

        n_m = m_qs.delete()[0]
        n_r = RawImportRow.objects.filter(id__in=raw_ids).delete()[0]
        self.stdout.write(self.style.SUCCESS(
            f'已清理：主表 {n_m} 条，原始行 {n_r} 条。'
            f'当前主表共 {MasterRecord.objects.count()} 条。'))
