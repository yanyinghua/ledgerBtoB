"""导入「店铺工商字典」：店铺名 / 工商登记名称 / 统一社会信用代码。

表头需含「店铺」「工商登记名称」「统一社会信用代码」（后两者可缺省）。
按 credit_code 或 shop_name upsert，幂等；支持 --dry-run。

用法：
  python3 manage.py import_shop_registry --file 店铺字典.xlsx --dry-run
  python3 manage.py import_shop_registry --file 店铺字典.xlsx
"""
import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import ShopBusinessRegistry, CustomerAlias
from books.imports.utils import normalize_account_name


class Command(BaseCommand):
    help = '导入店铺名与工商登记名称/信用代码字典'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='字典 xlsx 文件')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--sheet', default='', help='指定 sheet（默认自动找表头）')

    @transaction.atomic
    def handle(self, *args, **options):
        path = options['file']
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise CommandError(f'无法打开文件：{exc}')

        target = None
        for sn in ([options['sheet']] if options['sheet'] else wb.sheetnames):
            if sn not in wb.sheetnames:
                continue
            for idx, row in enumerate(wb[sn].iter_rows(values_only=True)):
                cols = [str(c).strip() if c is not None else '' for c in row]
                if '店铺' in cols:
                    target = (sn, idx, cols)
                    break
            if target:
                break
        if not target:
            raise CommandError('未找到含「店铺」表头的 sheet')

        sn, hdr_row, cols = target
        i_shop = cols.index('店铺')
        i_reg = next((i for i, c in enumerate(cols) if '工商' in c), None)
        i_code = next((i for i, c in enumerate(cols) if '信用' in c or '代码' in c), None)
        dry = options['dry_run']
        created = updated = skipped = 0

        for row in list(wb[sn].iter_rows(values_only=True))[hdr_row + 1:]:
            def val(i):
                return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ''
            shop = val(i_shop)
            if not shop:
                skipped += 1
                continue
            registered = val(i_reg)
            code = val(i_code)
            if dry:
                exists = ShopBusinessRegistry.objects.filter(
                    shop_name=shop).exists() or (code and ShopBusinessRegistry.objects.filter(credit_code=code).exists())
                if exists:
                    updated += 1
                else:
                    created += 1
                continue

            # 优先信用代码命中，其次店铺名
            obj = ShopBusinessRegistry.objects.filter(credit_code=code).first() if code else None
            if not obj:
                obj = ShopBusinessRegistry.objects.filter(shop_name=shop).first()
            if not obj:
                obj = ShopBusinessRegistry(shop_name=shop)
                created += 1
            else:
                updated += 1
            obj.registered_name = registered
            if code:
                obj.credit_code = code
            if not obj.entity_id:
                norm = normalize_account_name(shop)
                alias = CustomerAlias.objects.filter(normalized_value=norm, entity__isnull=False).first()
                if alias:
                    obj.entity = alias.entity
            obj.source = obj.source or '导入'
            obj.save()

        self.stdout.write(self.style.SUCCESS(
            f'店铺字典导入完成：新增 {created}，更新 {updated}，跳过 {skipped}'
            + ('（dry-run，未写库）' if dry else '')))
