"""重新生成销项/进项发票主表记录（按当前列映射从已存储的原始行重跑）。

用途：在列映射（column_mappings）增补了发票号码列（数电票号码/数电发票号码）
之后，调用 engine.rerun_import 让每个 SourceUpload 用「当前激活映射」从已暂存的
RawImportRow 重新提取 invoice_number（以及其它字段），从而补全此前漏填的发票号。

注意：
- 仅针对 sales_invoice / purchase_invoice 两类来源（发票主表）。
- rerun_import 会删除该 upload 下的旧主表行并按最新映射重建，结果确定且幂等。
- 原始数据已在各 SourceUpload 的 RawImportRow 中存留，无需重新提供 xlsx 文件。
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from books.models import SourceUpload, MasterRecord
from books.imports.engine import rerun_import


class Command(BaseCommand):
    help = '用当前列映射重跑所有销项/进项发票导入，补全发票号等字段'

    def add_arguments(self, parser):
        parser.add_argument('--source-type', default=None,
                            help='仅重跑指定来源类型(sales_invoice/purchase_invoice)，'
                                 '默认两类都跑')
        parser.add_argument('--limit', type=int, default=0,
                            help='只重跑前 N 个 upload（调试用，0=全部）')

    def handle(self, *args, **options):
        st = options.get('source_type')
        limit = options.get('limit') or 0
        types = ([st] if st else ['sales_invoice', 'purchase_invoice'])

        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')

        uploads = SourceUpload.objects.filter(
            source_type__in=types).order_by('id')
        if limit:
            uploads = uploads[:limit]

        total = uploads.count()
        self.stdout.write(f'待重跑 upload 数：{total}')
        for up in uploads:
            try:
                res = rerun_import(up.id, user)
            except Exception as exc:  # 单个文件失败不应阻断其余
                self.stderr.write(self.style.ERROR(
                    f'  [FAIL] upload#{up.id} {up.file_name}: {exc}'))
                continue
            self.stdout.write(
                f'  [OK] upload#{up.id} {up.file_name} -> '
                f'状态={res.status} 行数={res.row_count}')
        self.stdout.write(self.style.SUCCESS('重跑完成。'))
