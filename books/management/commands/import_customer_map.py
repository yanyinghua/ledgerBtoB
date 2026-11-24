"""批量导入客户主体清单与「付款人→客户主体」对照表。

客户主体表目前为空、付款人锚点有 937 个待指派。本命令用于对照表完成后一次性指派：

1) 主体清单（--entities）：列含 主体名称/税号/平台客户ID（列序不限，按列名识别）
   python3 manage.py import_customer_map --entities 客户主体清单.xlsx

2) 对照表（--map）：列含 付款人/账号、锚点类型(可选)、目标主体名称(或税号)
   python3 manage.py import_customer_map --map 付款人对照表.xlsx
   - 按 normalized_value 匹配 CustomerAlias 并指派 entity
   - 同时回填所有同归一值的 PaymentReceipt
   - 主体不存在时默认自动创建（--no-auto-create 可关闭）

列名识别（含常见别名）：
  主体名称：主体名称/客户主体/公司名称/名称/主体
  税号：税号/识别号/统一社会信用代码
  平台客户ID：平台客户ID/客户ID
  付款人：付款人/付款人名称/付款方/账号/openid/付款人账号
  锚点类型：锚点类型/类型
  目标主体：目标主体/主体名称/客户主体/归属主体/主体/税号

用法：
  python3 manage.py import_customer_map --entities 主体.xlsx --dry-run
  python3 manage.py import_customer_map --map 对照.xlsx --dry-run
"""
import pandas as pd
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from books.models import CustomerEntity, CustomerAlias, PaymentReceipt
from books.imports.utils import normalize_account_name

# 列名别名（归一化后匹配：去空格标点、大写）
HDR = {
    'entity_name': ['主体名称', '客户主体', '公司名称', '名称', '主体', '目标主体', '归属主体'],
    'tax_no': ['税号', '识别号', '统一社会信用代码', '纳税识别号'],
    'platform_id': ['平台客户id', '客户id'],
    'payer': ['付款人名称', '付款人', '付款方名称', '付款方', '付款人账号',
              'openid', '用户账号', '账号', '原始值', '付款人/账号'],
    'alias_type': ['锚点类型', '类型'],
    'target': ['目标主体', '归属主体', '客户主体', '主体名称', '主体', '税号'],
}


def _norm_hdr(s):
    return ''.join(c for c in str(s).strip().upper() if c.isalnum() or '\u4e00' <= c <= '\u9fff')


def _find_col(columns, keys):
    """返回匹配到的原始列名（优先精确，再包含）。"""
    norm_map = {_norm_hdr(c): c for c in columns}
    for k in keys:
        nk = _norm_hdr(k)
        if nk in norm_map:
            return norm_map[nk]
    for k in keys:
        nk = _norm_hdr(k)
        for nc, orig in norm_map.items():
            if nk and nk in nc:
                return orig
    return None


def _read_table(path):
    if path.lower().endswith('.csv'):
        for enc in ('utf-8-sig', 'gbk'):
            try:
                return pd.read_csv(path, dtype=str, encoding=enc, keep_default_na=False)
            except UnicodeDecodeError:
                continue
        raise CommandError('CSV 编码无法识别')
    xls = pd.ExcelFile(path, engine='openpyxl')
    # 取第一张有数据的 sheet
    for sn in xls.sheet_names:
        df = xls.parse(sn, dtype=str, keep_default_na=False)
        if not df.empty and len(df.columns) >= 2:
            return df
    raise CommandError('未找到有效数据表')


class Command(BaseCommand):
    help = '导入客户主体清单与付款人→主体对照表（批量指派归一字典）'

    def add_arguments(self, parser):
        parser.add_argument('--entities', default='', help='客户主体清单 xlsx/csv')
        parser.add_argument('--map', dest='map_file', default='', help='付款人→主体对照表 xlsx/csv')
        parser.add_argument('--no-auto-create', action='store_true',
                            help='对照表中主体不存在时不自动创建（跳过该行）')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')

    def handle(self, *args, **options):
        if not options['entities'] and not options['map_file']:
            raise CommandError('请提供 --entities 或 --map')

        if options['entities']:
            self._import_entities(options['entities'], options['dry_run'])
        if options['map_file']:
            self._import_map(options['map_file'], options['dry_run'],
                             auto_create=not options['no_auto_create'])

    def _import_entities(self, path, dry_run):
        df = _read_table(path)
        cols = list(df.columns)
        cname = _find_col(cols, HDR['entity_name'])
        ctax = _find_col(cols, HDR['tax_no'])
        cpid = _find_col(cols, HDR['platform_id'])
        if not cname:
            raise CommandError(f'主体清单未找到「主体名称」列，实际列：{cols}')
        created = updated = 0
        for _, row in df.iterrows():
            name = str(row[cname]).strip()
            if not name or name.lower() == 'nan':
                continue
            tax = str(row[ctax]).strip() if ctax else ''
            pid = str(row[cpid]).strip() if cpid else ''
            tax = '' if tax.lower() == 'nan' else tax
            pid = '' if pid.lower() == 'nan' else pid
            if dry_run:
                self.stdout.write(f'  [预览] 主体 {name} 税号={tax} 平台ID={pid}')
                continue
            e = CustomerEntity.objects.filter(tax_no=tax).first() if tax else None
            if not e and pid:
                e = CustomerEntity.objects.filter(platform_customer_id=pid).first()
            if not e:
                e = CustomerEntity.objects.filter(name=name).first()
            if e:
                # 补全缺失字段
                changed = []
                for field, val in (('tax_no', tax), ('platform_customer_id', pid)):
                    if val and not getattr(e, field):
                        setattr(e, field, val); changed.append(field)
                if changed:
                    e.save(update_fields=changed)
                updated += 1
            else:
                CustomerEntity.objects.create(name=name, tax_no=tax, platform_customer_id=pid)
                created += 1
        self.stdout.write(self.style.SUCCESS(
            f'主体清单：新增 {created}，更新 {updated}' + ('（预览）' if dry_run else '')))

    @transaction.atomic
    def _import_map(self, path, dry_run, auto_create=True):
        df = _read_table(path)
        cols = list(df.columns)
        cpayer = _find_col(cols, HDR['payer'])
        ctype = _find_col(cols, HDR['alias_type'])
        ctarget = _find_col(cols, HDR['target'])
        if not cpayer or not ctarget:
            raise CommandError(f'对照表需「付款人」与「目标主体」列，实际列：{cols}')
        assigned = created_entity = skipped = 0
        for _, row in df.iterrows():
            payer = str(row[cpayer]).strip()
            target = str(row[ctarget]).strip()
            if not payer or payer.lower() == 'nan' or not target or target.lower() == 'nan':
                continue
            atype = str(row[ctype]).strip() if ctype else ''
            if atype.lower() == 'nan':
                atype = ''
            # 目标主体：先按名称，再按税号（兼容对照表填的是税号）
            ent = CustomerEntity.objects.filter(name=target).first()
            if not ent:
                ent = CustomerEntity.objects.filter(tax_no=target).first()
            if not ent:
                if not auto_create:
                    skipped += 1
                    self.stdout.write(self.style.WARNING(f'  跳过（主体不存在）：{payer} → {target}'))
                    continue
                if dry_run:
                    self.stdout.write(f'  [预览] 将新建主体 {target}，指派 {payer}')
                    created_entity += 1
                    continue
                ent = CustomerEntity.objects.create(name=target)
                created_entity += 1

            norm = normalize_account_name(payer)
            # 找到锚点：按类型+归一值（未指定类型时两种都试）
            aliases = CustomerAlias.objects.filter(normalized_value=norm)
            if atype:
                aliases = aliases.filter(alias_type=atype)
            aliases = list(aliases)
            if dry_run:
                self.stdout.write(f'  [预览] {payer} → {ent.name}（命中 {len(aliases)} 个锚点）')
                continue
            if not aliases:
                skipped += 1
                continue
            for a in aliases:
                a.entity = ent
                a.is_manual = True
                a.save(update_fields=['entity', 'is_manual'])
                assigned += 1
                # 回填同归一值的收款明细
                n = 0
                for r in PaymentReceipt.objects.all().only('id', 'payer_name', 'payer_account', 'entity'):
                    val = r.payer_name if a.alias_type == '付款人名称' else r.payer_account
                    if val and normalize_account_name(val) == norm:
                        r.entity = ent
                        r.match_status = '已人工确认'
                        r.save(update_fields=['entity', 'match_status'])
                        n += 1
                if n:
                    self.stdout.write(f'  {payer} → {ent.name}，回填 {n} 笔')
        self.stdout.write(self.style.SUCCESS(
            f'对照表：指派锚点 {assigned}，新建主体 {created_entity}，跳过 {skipped}'
            + ('（预览）' if dry_run else '')))
