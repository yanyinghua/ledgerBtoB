"""按时间段清理操作日志：先导出 CSV 备份，再删除旧记录。

用法：
  # 删除 2026-01-01 之前（含）的全部操作日志，删除前自动导出备份到 CSV
  python manage.py clean_oplog --before 2026-01-01

  # 删除某时间段（含两端日期）
  python manage.py clean_oplog --after 2025-01-01 --before 2026-01-01

  # 指定备份文件路径
  python manage.py clean_oplog --before 2026-01-01 --output /tmp/oplog-backup.csv

  # 仅预览待删除数量，不写文件也不删
  python manage.py clean_oplog --before 2026-01-01 --dry-run

  # 只导出备份、不删除（适合“备份/导出记录”诉求，不影响线上数据）
  python manage.py clean_oplog --before 2099-01-01 --export-only --output /tmp/oplog-backup.csv

说明：
  操作日志在 admin 中禁止删除以防篡改；本命令是运维侧的“受控清理/备份”入口。
  默认行为：先把匹配记录导出 CSV 备份，再执行删除，确保可恢复。
  加 --export-only 则只导出不删除（用于单纯备份/导出）；--dry-run 只统计不落盘也不删。
"""
import csv
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from books.models import OperationLog

FIELDS = ['id', 'created_at', 'username', 'action',
          'target_type', 'target_id', 'detail', 'ip']


class Command(BaseCommand):
    help = '按时间段导出操作日志备份(CSV)并删除旧记录'

    def add_arguments(self, parser):
        parser.add_argument('--before', default=None,
                            help='删除 created_at 日期早于等于该日的记录，格式 YYYY-MM-DD')
        parser.add_argument('--after', default=None,
                            help='可选：删除 created_at 日期晚于等于该日的记录，格式 YYYY-MM-DD')
        parser.add_argument('--output', default=None,
                            help='备份 CSV 路径（默认写到当前目录 oplog-clean-<日期>.csv）')
        parser.add_argument('--export-only', action='store_true',
                            help='只导出 CSV 备份，不删除记录')
        parser.add_argument('--dry-run', action='store_true',
                            help='仅预览待删除数量，不写文件也不删库')

    def handle(self, *args, **options):
        before, after = options['before'], options['after']
        if not before and not after:
            raise CommandError('请至少提供 --before 或 --after 之一')

        q = OperationLog.objects.all()
        if before:
            bd = self._parse(before, '--before')
            q = q.filter(created_at__date__lte=bd)
        if after:
            ad = self._parse(after, '--after')
            q = q.filter(created_at__date__gte=ad)

        qs = q.order_by('created_at')
        count = qs.count()
        self.stdout.write(f'匹配记录数：{count}')

        if options['dry_run']:
            self.stdout.write('[dry-run] 仅预览，未导出/未删除。')
            return
        if count == 0:
            self.stdout.write('无匹配记录，退出。')
            return

        path = options['output'] or os.path.join(
            os.getcwd(), f'oplog-clean-{before or ("after-" + after)}.csv')
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            for log in qs:
                w.writerow([getattr(log, field) for field in FIELDS])
        self.stdout.write(f'已备份 {count} 条到 {path}')

        if options['export_only']:
            self.stdout.write('[export-only] 仅导出备份，未删除任何记录。')
            return

        qs.delete()
        self.stdout.write(f'已删除 {count} 条操作日志。')

    @staticmethod
    def _parse(value, flag):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            raise CommandError(f'{flag} 格式应为 YYYY-MM-DD，收到：{value!r}')
