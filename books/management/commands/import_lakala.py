"""导入拉卡拉订单 Excel（.xlsx）到 B2B Order 主表。

用法：
  python3 manage.py import_lakala --src /path/to/lakala   # 目录批量
  python3 manage.py import_lakala --file one.xlsx          # 单文件
  python3 manage.py import_lakala --src ... --dry-run      # 仅预览
"""
import glob
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from books.imports.lakala import run_import_lakala, run_import_lakala_incremental


class Command(BaseCommand):
    help = '导入拉卡拉订单（Excel → Order + OrderPayment）'

    def add_arguments(self, parser):
        parser.add_argument('--src', default='', help='订单表所在目录')
        parser.add_argument('--file', default='', help='单个 xlsx 文件')
        parser.add_argument('--source-type', default='lakala_order',
                            choices=['lakala_order', 'lakala_order_api'])
        parser.add_argument('--incremental', action='store_true', help='增量模式（按行哈希去重）')
        parser.add_argument('--dry-run', action='store_true', help='仅预览将导入的文件')
        parser.add_argument('--tag', default='', help='（保留兼容）')

    def _user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        return user

    def handle(self, *args, **options):
        src = options['src']
        single = options['file']
        if single:
            files = [single]
        elif src:
            files = sorted(glob.glob(os.path.join(src, '*.xlsx')) +
                           glob.glob(os.path.join(src, '*.xls')) +
                           glob.glob(os.path.join(src, '*.csv')))
        else:
            self.stderr.write('请提供 --src 或 --file')
            return

        if options['dry_run']:
            self.stdout.write(f'将导入 {len(files)} 个文件：')
            for f in files:
                self.stdout.write('  ' + f)
            return

        user = self._user()
        fn = run_import_lakala_incremental if options['incremental'] else run_import_lakala
        for path in files:
            with open(path, 'rb') as f:
                f.name = os.path.basename(path)
                upload = fn(options['source_type'], f, user, tag=options['tag'])
            style = self.style.SUCCESS if upload.status in ('success', 'empty') else self.style.ERROR
            self.stdout.write(style(f'{os.path.basename(path)}: {upload.status} - {upload.note}'))
