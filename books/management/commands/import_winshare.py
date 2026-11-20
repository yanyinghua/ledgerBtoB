"""全量导入 /mnt/win_share 下的往来数据。

- 销项/*.xlsx -> sales_invoice
- 进项/*.xlsx -> purchase_invoice
- 工行/*.xlsx -> bank
- 业务员资金流.xlsx -> other

按文件名去重（已导入则跳过）；销项 hrmxx2025.xlsx 与 hrmxx202501-202606.xlsx
在 2025 年重叠，默认跳过前者（可用 --include-overlap 强制包含）。
"""
import glob
import os

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from books.models import SourceUpload, MasterRecord
from books.imports.engine import run_import

BASE = os.environ.get('WINSHARE_BASE', '/mnt/win_share')
SALES_DIR = os.path.join(BASE, '销项')
PURCHASE_DIR = os.path.join(BASE, '进项')
BANK_DIR = os.path.join(BASE, '工行')
OTHER_FILE = os.path.join(BASE, '业务员资金流.xlsx')

# 与「hrmxx202501-202606.xlsx」在 2025 年重叠的窄文件，默认跳过
OVERLAP_SALES = {'全量发票查询导出结果hrmxx2025.xlsx'}


class Command(BaseCommand):
    help = '全量导入 /mnt/win_share 下的往来数据（按来源类型，去重，跳过重叠）'

    def add_arguments(self, parser):
        parser.add_argument('--include-overlap', action='store_true',
                            help='包含被默认跳过的重叠文件（如 hrmxx2025.xlsx）')
        parser.add_argument('--force', action='store_true',
                            help='对已导入的文件也删除旧记录并重新导入')

    def _user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        return user

    def _import_file(self, source_type, path, user, force):
        fname = os.path.basename(path)
        if source_type == 'fund_handler':
            with open(path, 'rb') as f:
                from books.imports.fund_handler import import_fund_handler
                up = import_fund_handler(f, user)
            self.stdout.write(f'  [{source_type}] {fname} -> {up.status} 行数={up.row_count} {up.note}')
            return
        exists = SourceUpload.objects.filter(file_name=fname, mode='full').exists()
        if exists and not force:
            self.stdout.write(f'  跳过(已导入): {fname}')
            return
        if exists and force:
            old = SourceUpload.objects.filter(file_name=fname)
            MasterRecord.objects.filter(source_upload__in=old).delete()
            old.delete()
        with open(path, 'rb') as f:
            up = run_import(source_type, f, user)
        self.stdout.write(f'  [{source_type}] {fname} -> {up.status} 行数={up.row_count} {up.note}')

    def handle(self, *args, **options):
        if not os.path.isdir(BASE):
            self.stderr.write(f'数据源目录不存在：{BASE}')
            return
        include_overlap = options.get('include_overlap')
        force = options.get('force')
        user = self._user()

        plan = []
        for p in sorted(glob.glob(os.path.join(SALES_DIR, '*.xlsx'))):
            fn = os.path.basename(p)
            if fn in OVERLAP_SALES and not include_overlap:
                self.stdout.write(f'  跳过(重叠2025): {fn}')
                continue
            plan.append(('sales_invoice', p))
        for p in sorted(glob.glob(os.path.join(PURCHASE_DIR, '*.xlsx'))):
            plan.append(('purchase_invoice', p))
        for p in sorted(glob.glob(os.path.join(BANK_DIR, '*.xlsx'))):
            plan.append(('bank', p))
        if os.path.exists(OTHER_FILE):
            plan.append(('fund_handler', OTHER_FILE))

        self.stdout.write(f'待导入文件数：{len(plan)}')
        for st, p in plan:
            self._import_file(st, p, user, force)

        # 导入完成后，用资金明细业务员表匹配银行流水经手人（仅填空行、幂等）
        self.stdout.write('开始匹配银行流水经手人…')
        try:
            from django.core.management import call_command
            call_command('match_bank_handlers')
        except Exception as exc:  # 匹配失败不应阻断导入结果
            self.stderr.write(self.style.ERROR(f'银行流水经手人匹配失败：{exc}'))

        self.stdout.write(self.style.SUCCESS(
            f'导入完成。主表当前共 {MasterRecord.objects.count()} 条'))
