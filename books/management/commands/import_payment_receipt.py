"""导入收款明细（Excel .xlsx）到 PaymentReceipt 并做往来单位归一。

用法：
  python3 manage.py import_payment_receipt --file detail.xlsx
  python3 manage.py import_payment_receipt --src /path/to/details --incremental
  python3 manage.py import_payment_receipt --file detail.xlsx --dry-run
"""
import glob
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from books.imports.payment_receipt import run_import_payment_receipt


class Command(BaseCommand):
    help = '导入收款明细（Excel → PaymentReceipt + 客户/货主归一）'

    def add_arguments(self, parser):
        parser.add_argument('--src', default='', help='收款明细表所在目录')
        parser.add_argument('--file', default='', help='单个 xlsx 文件')
        parser.add_argument('--incremental', action='store_true', help='增量模式（按行哈希去重）')
        parser.add_argument('--dry-run', action='store_true', help='仅预览将导入的文件')

    def _user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        return user

    def handle(self, *args, **options):
        if options['file']:
            files = [options['file']]
        elif options['src']:
            files = sorted(glob.glob(os.path.join(options['src'], '*.xlsx')))
        else:
            self.stderr.write('请提供 --file 或 --src')
            return

        if options['dry_run']:
            self.stdout.write(f'将导入 {len(files)} 个文件：')
            for f in files:
                self.stdout.write('  ' + f)
            return

        user = self._user()
        for path in files:
            with open(path, 'rb') as f:
                f.name = os.path.basename(path)
                upload = run_import_payment_receipt(f, user, incremental=options['incremental'])
            style = self.style.SUCCESS if upload.status in ('success', 'empty') else self.style.ERROR
            self.stdout.write(style(f'{os.path.basename(path)}: {upload.status} - {upload.note}'))
