"""增量导入 /mnt/win_share 下的往来数据。

与全量导入不同，本命令不按文件名去重，而是按「原始行内容哈希」去重：
对每个文件重读并逐行比对已有原始行，只写入哈希未出现过的新行。
适合定期把同一份全量导出文件重新跑一遍，自动跳过已存在的记录，
只追加新增/变化的行（同一发票行内容不变则被复用哈希跳过）。

销项 hrmxx2025.xlsx 与 hrmxx202501-202606.xlsx 在 2025 年重叠，默认跳过前者
（可用 --include-overlap 强制包含；即便包含也因哈希去重不会产生重复主表行）。
"""
import glob
import os

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from books.models import MasterRecord
from books.imports.engine import run_import_incremental

BASE = '/mnt/win_share'
SALES_DIR = os.path.join(BASE, '销项')
PURCHASE_DIR = os.path.join(BASE, '进项')
BANK_DIR = os.path.join(BASE, '工行')
OTHER_FILE = os.path.join(BASE, '业务员资金流.xlsx')

# 与「hrmxx202501-202606.xlsx」在 2025 年重叠的窄文件，默认跳过
OVERLAP_SALES = {'全量发票查询导出结果hrmxx2025.xlsx'}


class Command(BaseCommand):
    help = '增量导入 /mnt/win_share 下的往来数据（按行哈希去重，只追加新行）'

    def add_arguments(self, parser):
        parser.add_argument('--src', default=BASE,
                            help='数据源根目录（默认 /mnt/win_share；正式环境建议指向稳定的本地目录）')
        parser.add_argument('--include-overlap', action='store_true',
                            help='包含被默认跳过的重叠文件（如 hrmxx2025.xlsx）')

    def _user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        return user

    def _import_file(self, source_type, path, user):
        fname = os.path.basename(path)
        with open(path, 'rb') as f:
            if source_type == 'fund_handler':
                from books.imports.fund_handler import import_fund_handler
                up = import_fund_handler(f, user)
            else:
                up = run_import_incremental(source_type, f, user)
        self.stdout.write(
            f'  [{source_type}] {fname} -> {up.status} 新增={up.row_count} {up.note}')

    def handle(self, *args, **options):
        base = options['src']
        if not os.path.isdir(base):
            self.stderr.write(f'数据源目录不存在或未挂载：{base}')
            return
        sales_dir = os.path.join(base, '销项')
        purchase_dir = os.path.join(base, '进项')
        bank_dir = os.path.join(base, '工行')
        other_file = os.path.join(base, '业务员资金流.xlsx')

        include_overlap = options.get('include_overlap')
        user = self._user()

        plan = []
        for p in sorted(glob.glob(os.path.join(sales_dir, '*.xlsx'))):
            fn = os.path.basename(p)
            if fn in OVERLAP_SALES and not include_overlap:
                self.stdout.write(f'  跳过(重叠2025): {fn}')
                continue
            plan.append(('sales_invoice', p))
        for p in sorted(glob.glob(os.path.join(purchase_dir, '*.xlsx'))):
            plan.append(('purchase_invoice', p))
        for p in sorted(glob.glob(os.path.join(bank_dir, '*.xlsx'))):
            plan.append(('bank', p))
        if os.path.exists(other_file):
            plan.append(('fund_handler', other_file))

        self.stdout.write(f'待扫描文件数：{len(plan)}（按行哈希去重，只追加新行）')
        for st, p in plan:
            self._import_file(st, p, user)

        # 导入完成后，用资金明细业务员表匹配银行流水经手人（仅填空行、幂等）
        self.stdout.write('开始匹配银行流水经手人…')
        try:
            from django.core.management import call_command
            call_command('match_bank_handlers')
        except Exception as exc:  # 匹配失败不应阻断导入结果
            self.stderr.write(self.style.ERROR(f'银行流水经手人匹配失败：{exc}'))

        self.stdout.write(self.style.SUCCESS(
            f'增量导入完成。主表当前共 {MasterRecord.objects.count()} 条'))
