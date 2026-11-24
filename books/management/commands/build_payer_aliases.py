"""批量建档付款人归一锚点：从收款明细/原始文件提取付款人，写入 CustomerAlias。

创建的锚点 entity 留空（待人工在「归一待复核」页指派到客户主体）。
幂等：按 (alias_type, normalized_value) 去重，重复运行不产生新记录，只累加笔数/金额统计到 note。

用法：
  # 从原始文件提取（招行交易查询 / 富掌柜交易明细，自动识别表头）
  python3 manage.py build_payer_aliases --file /path/to/招行.xlsx
  python3 manage.py build_payer_aliases --file /path/to/富掌柜.xlsx
  # 从已导入的收款明细表提取
  python3 manage.py build_payer_aliases --from-db
  # 只预览不写库
  python3 manage.py build_payer_aliases --file ... --dry-run
"""
from collections import defaultdict

import openpyxl
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from books.models import CustomerAlias, PaymentReceipt, SourceUpload
from books.imports.utils import normalize_account_name


# 各文件中付款人所在的列（按模板识别）
PAYER_COLUMNS = {
    '招行交易查询': ['收(付)方名称'],
    '富掌柜交易明细': ['交易备注3(用户账号)'],
}


def _detect_sheet(wb):
    """识别文件类型与付款人列，返回 (类型, sheet名, 列索引, 表头行)。"""
    for sn in wb.sheetnames:
        ws = wb[sn]
        for idx, row in enumerate(ws.iter_rows(values_only=True)):
            cols = [str(c).strip().strip('\\t') if c is not None else '' for c in row]
            cset = set(cols)
            if {'交易日', '流水号', '收(付)方名称'} & cset and '交易金额' in cset:
                return '招行交易查询', sn, cols.index('收(付)方名称'), idx
            if {'交易单号', '商户名称', '支付方式'} & cset and '交易金额' in cset:
                return '富掌柜交易明细', sn, cols.index('交易备注3(用户账号)'), idx
    return None, None, None, None


class Command(BaseCommand):
    help = '批量建档付款人归一锚点（entity 留空待人工指派）'

    def add_arguments(self, parser):
        parser.add_argument('--file', default='', help='原始收款明细 xlsx（自动识别表头）')
        parser.add_argument('--from-db', action='store_true', help='从已导入的 PaymentReceipt 提取')
        parser.add_argument('--alias-type', default='', help='强制锚点类型（付款人名称/付款人账号）')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')

    def handle(self, *args, **options):
        pairs = []  # (alias_type, value, count, amount_sum)

        if options['file']:
            pairs = self._from_file(options['file'])
        elif options['from_db']:
            pairs = self._from_db()
        else:
            self.stderr.write('请提供 --file 或 --from-db')
            return

        if options['alias_type']:
            pairs = [(options['alias_type'], v, c, a) for _, v, c, a in pairs]

        created = updated = 0
        for alias_type, value, cnt, amt in pairs:
            norm = normalize_account_name(value)
            if not norm:
                continue
            note = f'笔数={cnt} 金额={amt:.2f}'
            if options['dry_run']:
                self.stdout.write(f'  [预览] {alias_type}:{value} -> {norm} ({note})')
                continue
            alias, was = CustomerAlias.objects.get_or_create(
                alias_type=alias_type, normalized_value=norm,
                defaults={'alias_value': value, 'is_active': True, 'note': note})
            if was:
                created += 1
            else:
                alias.note = note
                alias.save(update_fields=['note'])
                updated += 1

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(f'预览完成：{len(pairs)} 个锚点（未写库）'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'建档完成：新增 {created} 条，更新 {updated} 条（entity 留空待指派）'))

    def _from_file(self, path):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        tname, sn, idx, hdr_row = _detect_sheet(wb)
        if not tname:
            self.stderr.write('未识别表头（需招行交易查询或富掌柜交易明细格式）')
            return []
        alias_type = '付款人名称' if tname == '招行交易查询' else '付款人账号'
        ws = wb[sn]
        stat = defaultdict(lambda: [0, 0.0])
        rows = list(ws.iter_rows(values_only=True))
        hdr = [str(c).strip().strip('\\t') if c is not None else '' for c in rows[hdr_row]]
        aidx = hdr.index('交易金额') if '交易金额' in hdr else None
        for r in rows[hdr_row + 1:]:
            if idx >= len(r) or not r[idx]:
                continue
            v = str(r[idx]).strip().strip('\\t')
            if not v:
                continue
            amt = 0.0
            if aidx is not None and aidx < len(r):
                try:
                    amt = float(r[aidx])
                except (ValueError, TypeError):
                    amt = 0.0
            stat[v][0] += 1
            stat[v][1] += amt
        wb.close()
        self.stdout.write(f'识别文件类型：{tname}，锚点类型：{alias_type}，唯一付款人 {len(stat)}')
        return [(alias_type, v, c, a) for v, (c, a) in stat.items()]

    def _from_db(self):
        stat = defaultdict(lambda: [0, 0.0])
        for r in PaymentReceipt.objects.all().only('payer_name', 'payer_account', 'amount'):
            if r.payer_name:
                key = ('付款人名称', r.payer_name)
                stat[key][0] += 1
                stat[key][1] += float(r.amount or 0)
            if r.payer_account:
                key = ('付款人账号', r.payer_account)
                stat[key][0] += 1
                stat[key][1] += float(r.amount or 0)
        self.stdout.write(f'从库中提取：{len(stat)} 个锚点')
        return [(t, v, c, a) for (t, v), (c, a) in stat.items()]
