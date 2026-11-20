"""按『备注里的复核人』逻辑回填销项发票经手人。

背景：2024-09 起规定销项发票「复核人」即业务员(经手人)；2024-10 起导出里没有
独立『复核人』列，复核人姓名写在『备注』列（如『复核人:高维』『对账单号…复核人:魏家荣』）。
经手人只从『复核人』派生（备注里的人名或复核人列），开票人列从不作为来源。
2024-09 之前无此规定，经手人按管理要求忽略（不派生）。

本命令：对 sales_invoice 且 handler 为空、且发生日期 >= --from-date（默认 2024-09-01）
的主表记录，从对应原始导入行重新派生经手人：
  1) 取『备注』中『复核人/审核人/经办/…』后的人名（extract_reviewer_name）；
  2) 取不到时回退到『复核人』列（extract_salesperson）。
注意：经手人恒为“复核人”，开票人（如严应华）是开票员、不是经手人，绝不能作为经手人来源。
只填空、不覆盖已有经手人。同时把原始『备注』补写回 remark（若当前为空）。

关联方式：数电发票的 invoice_number 为空，无法用发票号关联，故用
(发生日期, 归一化单位名, 金额) 三元组关联（与银行匹配同思路），单位名用与导入
一致的归一逻辑，确保与 MasterRecord.unit_name 对齐。
同时把『发票号码 / 数电发票号码』补写回 invoice_number（数电发票原本此列为空）。
支持 --dry-run 预览。
"""
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Q
from django.core.management.base import BaseCommand

from books.imports.utils import (
    extract_reviewer_name, extract_salesperson,
    to_date, to_decimal, normalize_unit_name,
)
from books.models import MasterRecord, RawImportRow

CENT = Decimal('0.01')


def gv(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ''):
            return str(v).strip()
    return ''


class Command(BaseCommand):
    help = '按备注复核人逻辑回填销项发票经手人（只填空、不覆盖）'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')
        parser.add_argument('--from-date', default='2024-09-01',
                            help='仅处理发生日期 >= 该日期的销项发票（默认 2024-09-01，即规定复核人=业务员之月起）')

    def handle(self, *args, **options):
        dry = options['dry_run']
        from_date = options['from_date']

        # 1) 由各来源的原始行构建 (日期, 单位, 金额) -> 候选集合(经手人/备注/发票号)
        #    用集合聚合：仅当某三元组只对应唯一值时才回填，避免碰撞误填。
        cand_handler = {}
        cand_remark = {}
        cand_inv = {}
        raw_total = 0
        for r in RawImportRow.objects.filter(
                source_upload__source_type='sales_invoice').iterator():
            raw_total += 1
            d = r.data
            unit_raw = gv(d, '购买方名称', '购方名称', '客户名称', '购方单位名称', '购方单位')
            tax_raw = gv(d, '购方纳税人识别号', '购买方纳税人识别号', '购方识别号', '购方税号')
            norm_unit = normalize_unit_name(unit_raw, tax_raw)
            amt = abs(to_decimal(gv(d, '价税合计') or gv(d, '金额'))).quantize(CENT, rounding=ROUND_HALF_UP)
            occ = to_date(gv(d, '开票日期', '填开日期', '日期'))
            if not norm_unit or amt == 0 or occ is None:
                continue
            key = (occ, norm_unit, amt)
            h = extract_reviewer_name(gv(d, '备注')) or extract_salesperson(gv(d, '复核人'))
            if h:
                cand_handler.setdefault(key, set()).add(h)
            bz = gv(d, '备注')
            if bz:
                cand_remark.setdefault(key, set()).add(bz)
            inv_val = gv(d, '发票号码') or gv(d, '数电发票号码')
            if inv_val:
                cand_inv.setdefault(key, set()).add(inv_val)

        # 仅保留「唯一值」的候选（碰撞的一律不填，避免错填标识符）
        def uniq(s):
            return next(iter(s)) if s and len(s) == 1 else None
        cand_handler = {k: uniq(v) for k, v in cand_handler.items()}
        cand_remark = {k: uniq(v) for k, v in cand_remark.items()}
        cand_inv = {k: uniq(v) for k, v in cand_inv.items()}

        self.stdout.write(f'已扫描原始行 {raw_total} 条；可唯一派生经手人的(日期,单位,金额)组合 {len(cand_handler)} 个。')

        # 2) 回填主表（只填空、不覆盖）
        qs = MasterRecord.objects.filter(
            source_type='sales_invoice', occur_date__gte=from_date
        ).filter(Q(handler='') | Q(invoice_number='') | Q(remark=''))
        total = qs.count()
        upd = []
        samples = []
        for rec in qs.iterator():
            amt = abs(rec.debit or Decimal('0')) or abs(rec.credit or Decimal('0'))
            key = (rec.occur_date, rec.unit_name, amt.quantize(CENT, rounding=ROUND_HALF_UP))
            new_h = cand_handler.get(key)
            new_remark = cand_remark.get(key) if not rec.remark else None
            new_inv = cand_inv.get(key) if not rec.invoice_number else None
            if not (new_h or new_remark or new_inv):
                continue
            changed = False
            if new_h and rec.handler != new_h:
                rec.handler = new_h
                changed = True
            if new_remark:
                rec.remark = new_remark
                changed = True
            if new_inv:
                rec.invoice_number = new_inv
                changed = True
            if changed:
                upd.append(rec)
                if len(samples) < 10:
                    samples.append((rec.unit_name, str(rec.occur_date), new_inv, new_h))

        if dry:
            self.stdout.write(self.style.WARNING(
                f'[预览] 待回填 {len(upd)} 条（需补全的主表共 {total} 条）。'))
        else:
            if upd:
                MasterRecord.objects.bulk_update(upd, ['handler', 'remark', 'invoice_number'])
            self.stdout.write(self.style.SUCCESS(
                f'已回填 {len(upd)} 条（需补全的主表共 {total} 条）。'))

        if samples:
            self.stdout.write('样本（单位 / 日期 / 发票号 / 经手人）:')
            for u, dt, inv, h in samples:
                self.stdout.write(f'  {u} | {dt} | {inv} | {h}')
