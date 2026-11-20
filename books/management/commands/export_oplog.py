"""按时间段导出操作日志为 CSV 备份（纯导出，不影响线上数据）。

用法：
  # 导出 2026-01-01 之前（含）的全部操作日志
  python manage.py export_oplog --before 2026-01-01

  # 导出某时间段（含两端日期）
  python manage.py export_oplog --after 2025-01-01 --before 2026-01-01

  # 指定输出路径
  python manage.py export_oplog --before 2026-01-01 --output /tmp/oplog.csv

说明：
  仅读取并写出 CSV，不修改任何记录；适合定期备份 / 审计导出。
  时间段用 created_at 的日期部分判断（--before/--after 格式 YYYY-MM-DD，含当日）。
  若两个参数都不给，则导出全部记录。
"""
import csv
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from books.models import OperationLog

FIELDS = ['id', 'created_at', 'username', 'action',
          'target_type', 'target_id', 'detail', 'ip']


class Command(BaseCommand):
    help = '按时间段导出操作日志为 CSV（不影响线上数据）'

    def add_arguments(self, parser):
        parser.add_argument('--before', default=None,
                            help='导出 created_at 日期早于等于该日的记录，格式 YYYY-MM-DD')
        parser.add_argument('--after', default=None,
                            help='可选：导出 created_at 日期晚于等于该日的记录，格式 YYYY-MM-DD')
        parser.add_argument('--output', default=None,
                            help='输出 CSV 路径（默认写到当前目录 oplog-export-<日期>.csv）')

    def handle(self, *args, **options):
        before, after = options['before'], options['after']

        q = OperationLog.objects.all()
        if before:
            bd = self._parse(before, '--before')
            q = q.filter(created_at__date__lte=bd)
        if after:
            ad = self._parse(after, '--after')
            q = q.filter(created_at__date__gte=ad)

        qs = q.order_by('created_at')
        count = qs.count()
        if count == 0:
            self.stdout.write('无匹配记录，未生成文件。')
            return

        path = options['output'] or os.path.join(
            os.getcwd(), f'oplog-export-{before or ("after-" + after) or "all"}.csv')
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            for log in qs:
                w.writerow([getattr(log, field) for field in FIELDS])
        self.stdout.write(f'已导出 {count} 条操作日志到 {path}')

    @staticmethod
    def _parse(value, flag):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            raise CommandError(f'{flag} 格式应为 YYYY-MM-DD，收到：{value!r}')
