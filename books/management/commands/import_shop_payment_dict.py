"""导入「店铺付款字典」：店铺=客户主体，户名/openid 为付款人锚点。

文件结构（Sheet1）：
  店铺 | 户名 | 账号/用户号(微信/支付宝)
- 店铺名 → 建/找 CustomerEntity（店铺即客户主体）
- 户名（公司/个人）→ CustomerAlias(付款人名称) 并指派该店铺主体
- 账号/openid → CustomerAlias(付款人账号) 并指派该店铺主体
- 店铺名为空的行（Excel 合并单元格）→ 只建锚点，entity 留空待人工指派

指派锚点时回填所有同归一值的 PaymentReceipt。幂等，可 --dry-run。

用法：
  python3 manage.py import_shop_payment_dict --file 店铺付款字典.xlsx
  python3 manage.py import_shop_payment_dict --file ... --dry-run
"""
import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import CustomerEntity, CustomerAlias, PaymentReceipt
from books.imports.utils import normalize_account_name


def _is_openid(v):
    """openid/微信用户号通常是 o 开头的长串或纯数字账号。"""
    s = str(v).strip()
    return s.startswith('o') and len(s) > 20


class Command(BaseCommand):
    help = '导入店铺付款字典（店铺=客户主体，付款人归一并回填）'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='店铺付款字典 xlsx')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--sheet', default='', help='指定 sheet（默认自动找含“店铺”表头的）')

    @transaction.atomic
    def handle(self, *args, **options):
        path = options['file']
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise CommandError(f'无法打开文件：{exc}')

        # 定位 sheet / 表头行
        target = None
        sheets = [options['sheet']] if options['sheet'] else wb.sheetnames
        for sn in sheets:
            if sn not in wb.sheetnames:
                continue
            ws = wb[sn]
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                cols = [str(c).strip() if c is not None else '' for c in row]
                if '店铺' in cols and any('账号' in c for c in cols):
                    target = (sn, idx, cols)
                    break
            if target:
                break
        if not target:
            raise CommandError('未找到含「店铺」「账号」表头的 sheet')
        sn, hdr_row, cols = target
        i_shop = cols.index('店铺')
        i_name = cols.index('户名') if '户名' in cols else 1
        i_acct = next(i for i, c in enumerate(cols) if '账号' in c)

        ws = wb[sn]
        rows = list(ws.iter_rows(values_only=True))
        dry = options['dry_run']
        created_entity = created_alias = assigned = skipped = 0
        receipt_backfill = 0
        dry_entities = set()  # dry 模式模拟已建主体，避免重复计数
        dry_aliases = set()   # dry 模式模拟锚点去重

        for row in rows[hdr_row + 1:]:
            shop = str(row[i_shop]).strip() if i_shop < len(row) and row[i_shop] else ''
            name = str(row[i_name]).strip() if i_name < len(row) and row[i_name] else ''
            acct = str(row[i_acct]).strip() if i_acct < len(row) and row[i_acct] else ''
            if not (shop or name or acct):
                continue

            # 店铺主体
            ent = None
            if shop:
                if dry:
                    ent = None
                    if shop not in dry_entities:
                        dry_entities.add(shop)
                        created_entity += 1
                else:
                    ent = CustomerEntity.objects.filter(name=shop).first()
                    if not ent:
                        ent = CustomerEntity.objects.create(name=shop)
                        created_entity += 1

            # 付款人锚点：户名（付款人名称）+ 账号（付款人账号）
            pairs = []
            if name:
                pairs.append(('付款人名称', name))
            if acct:
                pairs.append(('付款人账号', acct))
            for atype, value in pairs:
                norm = normalize_account_name(value)
                if not norm:
                    continue
                if dry:
                    key = (atype, norm)
                    if key not in dry_aliases:
                        dry_aliases.add(key)
                        created_alias += 1
                    if shop:
                        assigned += 1
                    else:
                        skipped += 1
                    continue
                a = CustomerAlias.objects.filter(alias_type=atype, normalized_value=norm).first()
                if not a:
                    a = CustomerAlias.objects.create(alias_type=atype, normalized_value=norm,
                                                     alias_value=value, is_active=True)
                    created_alias += 1
                if ent is not None:
                    a.entity = ent
                    a.is_manual = True
                    a.save(update_fields=['entity', 'is_manual'])
                    assigned += 1
                    # 回填同归一值收款明细
                    for r in PaymentReceipt.objects.all().only('id', 'payer_name', 'payer_account', 'entity', 'match_status'):
                        v = r.payer_name if atype == '付款人名称' else r.payer_account
                        if v and normalize_account_name(v) == norm:
                            r.entity = ent
                            r.match_status = '已人工确认'
                            r.save(update_fields=['entity', 'match_status'])
                            receipt_backfill += 1
                else:
                    skipped += 1  # 店铺为空，锚点留空待指派
        wb.close()

        msg = (f'店铺付款字典：新建主体 {created_entity}，新建锚点 {created_alias}，'
               f'指派锚点 {assigned}，回填收款明细 {receipt_backfill}，'
               f'留空待指派 {skipped}')
        self.stdout.write(self.style.WARNING(msg + '（预览）') if dry else self.style.SUCCESS(msg))
