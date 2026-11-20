"""从原始 xlsx 的货物明细 sheet（信息汇总表）重建 InvoiceLineItem。

用途：部署后，用本地/服务器的原始发票 xlsx（含「信息汇总表」明细 sheet）为每张发票
解析货物明细行（货物名称/规格型号/单位/数量/单价/金额/税率/税额），落到 InvoiceLineItem，
供「发票检索」穿透查看发票明细。

- 仅处理 sales_invoice(销项) / purchase_invoice(进项) 两类来源。
- xlsx 定位：生产环境 /mnt/win_share 未挂载，需把原始文件同步到某目录后用 --src 指定；
  默认 --src=/mnt/win_share，文件在 <src>/销项/ 或 <src>/进项/ 子目录下，按
  SourceUpload.file_name(仅文件名) 匹配。
- 幂等：每个 upload 先删后建（_import_detail_sheets 内部已按 source_upload 删除重建）。

示例：
  python manage.py import_invoice_details --src /srv/ledger/invoice_src
"""
import os

from django.core.management.base import BaseCommand

from books.models import SourceUpload, InvoiceLineItem
from books.imports.engine import _import_detail_sheets

SUBDIR = {
    'sales_invoice': '销项',
    'purchase_invoice': '进项',
}


class Command(BaseCommand):
    help = '从原始 xlsx 明细 sheet 重建发票货物明细行（InvoiceLineItem）'

    def add_arguments(self, parser):
        parser.add_argument('--src', default='/mnt/win_share',
                            help='原始发票 xlsx 所在根目录（含 销项/ 进项/ 子目录），'
                                 '默认 /mnt/win_share')
        parser.add_argument('--source-type', default=None,
                            help='仅处理指定来源(sales_invoice/purchase_invoice)，默认两类都处理')
        parser.add_argument('--limit', type=int, default=0,
                            help='只处理前 N 个 upload（调试用，0=全部）')
        parser.add_argument('--file', default=None,
                            help='直接指定一个 xlsx 补建明细（不遍历 upload）。'
                                 '用于原始文件未登记为 upload、或某份文件的明细 sheet 比'
                                 '现有导入更全的场景；需配合 --source-type')
        parser.add_argument('--upload', type=int, default=0,
                            help='配合 --file：明细挂到指定 upload 名下（缺省新建一个导入记录）')

    def _import_file(self, path, source_type, upload_id, stdout):
        """用指定 xlsx 重建明细；未指定 upload 时新建一条导入记录承载这批明细。"""
        import pandas as pd
        from django.contrib.auth.models import User
        from books.models import SourceUpload

        if upload_id:
            up = SourceUpload.objects.get(id=upload_id)
        else:
            up = SourceUpload.objects.create(
                source_type=source_type, file_name=path, status='success', mode='incremental',
                uploaded_by=User.objects.filter(is_superuser=True).first(),
                note='仅补建货物明细（import_invoice_details --file）')
        xls = pd.ExcelFile(path, engine='openpyxl')
        _import_detail_sheets(xls, up)
        lines = up.line_items
        stdout.write(self.style.SUCCESS(
            f'  [OK] {path} -> upload#{up.id} 明细={lines.count()} 条，'
            f'已挂主表={lines.exclude(master_record__isnull=True).count()} 条'))
        return up

    def handle(self, *args, **options):
        src = options.get('src')
        st = options.get('source_type')
        limit = options.get('limit') or 0

        if options.get('file'):
            self._import_file(options['file'], st or 'sales_invoice',
                              options['upload'], self.stdout)
            self.stdout.write(self.style.SUCCESS(
                f'完成；InvoiceLineItem 当前共 {InvoiceLineItem.objects.count()} 条'))
            return

        types = ([st] if st else ['sales_invoice', 'purchase_invoice'])
        uploads = SourceUpload.objects.filter(
            source_type__in=types, row_count__gt=0).order_by('id')
        if limit:
            uploads = uploads[:limit]

        total = uploads.count()
        self.stdout.write(f'待处理 upload 数：{total}（src={src}）')

        ok, skip, fail = 0, 0, 0
        for up in uploads:
            sub = SUBDIR[up.source_type]
            fname = os.path.basename(up.file_name)
            # 优先：<src>/<子目录>/<文件名>（生产同步后的目录结构）
            # 回退：原始 file_name 本身（若本地该路径仍可直接访问）
            candidates = [
                os.path.join(src, sub, fname),
                os.path.join(src, fname),
                up.file_name,
            ]
            path = next((p for p in candidates if os.path.isfile(p)), None)
            if not path:
                self.stderr.write(self.style.WARNING(
                    f'  [SKIP] upload#{up.id} {up.file_name}: 未在 {src}/{sub}/ 找到 {fname}'))
                skip += 1
                continue
            try:
                import pandas as pd
                xls = pd.ExcelFile(path, engine='openpyxl')
                _import_detail_sheets(xls, up)
                n = up.line_items.count()
                self.stdout.write(
                    f'  [OK] upload#{up.id} {up.file_name} -> 明细行={n}')
                ok += 1
            except Exception as exc:  # 单个文件失败不应阻断其余
                self.stderr.write(self.style.ERROR(
                    f'  [FAIL] upload#{up.id} {up.file_name}: {exc}'))
                fail += 1

        self.stdout.write(self.style.SUCCESS(
            f'完成：成功={ok} 跳过(未找到文件)={skip} 失败={fail}，'
            f'InvoiceLineItem 当前共 {InvoiceLineItem.objects.count()} 条'))
