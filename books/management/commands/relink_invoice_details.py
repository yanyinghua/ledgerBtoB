"""把「已解析但未挂到主表」的货物明细行按发票号重新挂接。

背景：早期版本的明细挂接只在同一次导入(upload)内按发票号找主表，遇到增量导入
（主表 0 新增、明细落在新 upload）就会整批挂不上。修复后的导入已能正确挂接，
但历史遗留的这批明细仍在库里空转 —— 用本命令按发票号补挂即可，无需原始 xlsx。

判定：明细行 invoice_number 能在同来源类型的主表中找到同号记录 -> 挂到该主表
（同一发票号有多条主表时取 id 最小的那条）。找不到就保持原样并计数提示。

用法：
    python3 manage.py relink_invoice_details            # 预览（默认不写库）
    python3 manage.py relink_invoice_details --apply    # 真正挂接
    python3 manage.py relink_invoice_details --source-type sales_invoice
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from books.models import MasterRecord, InvoiceLineItem

TYPES = ('sales_invoice', 'purchase_invoice')
LABEL = {'sales_invoice': '销项', 'purchase_invoice': '进项'}


class Command(BaseCommand):
    help = '按发票号把未挂主表的货物明细行补挂到对应发票主表（默认预览）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正写库；缺省仅预览匹配情况')
        parser.add_argument('--source-type', default=None,
                            help='仅处理指定来源(sales_invoice/purchase_invoice)，默认两类')

    def handle(self, *args, **options):
        apply = options['apply']
        types = [options['source_type']] if options['source_type'] else list(TYPES)

        for st in types:
            orphan = InvoiceLineItem.objects.filter(
                source_upload__source_type=st, master_record__isnull=True)
            total = orphan.count()
            self.stdout.write(f"\n=== {LABEL.get(st, st)}（{st}）未挂主表明细 {total} 条 ===")
            if not total:
                self.stdout.write('  无待挂接明细，跳过。')
                continue

            idx = {}
            for mid, no in (MasterRecord.objects.filter(source_type=st)
                            .exclude(invoice_number='')
                            .order_by('id').values_list('id', 'invoice_number')):
                idx.setdefault(no, mid)

            plan, miss = {}, 0
            for no in (orphan.exclude(invoice_number='')
                       .values_list('invoice_number', flat=True).distinct()):
                mid = idx.get(no)
                if mid:
                    plan[no] = mid
                else:
                    miss += 1
            hit = InvoiceLineItem.objects.filter(
                source_upload__source_type=st, master_record__isnull=True,
                invoice_number__in=plan.keys()).count()

            self.stdout.write(
                f'  可按发票号挂接：{hit} 条（{len(plan)} 张发票）；'
                f'库中没有对应主表的发票号：{miss} 个')
            if miss:
                # 匹配不上时打印两边样本，便于判断是"值不同"还是"格式不同（科学计数法/小数尾）"
                orphan_sample = list(orphan.exclude(invoice_number='')
                                     .values_list('invoice_number', flat=True).distinct()[:5])
                keys = list(idx.keys())
                self.stdout.write('  [未挂明细] 发票号样本：' + ' | '.join(map(str, orphan_sample)))
                self.stdout.write('  [主表最新] 发票号样本：' + ' | '.join(map(str, keys[-5:])))
                self.stdout.write('  [主表最早] 发票号样本：' + ' | '.join(map(str, keys[:5])))
                # 未挂明细所属期间（按票号前 4 位推断年份 + 主表期间交叉核对）
                recent = list(MasterRecord.objects.filter(source_type=st)
                              .exclude(period='').order_by('-period')
                              .values_list('period', flat=True).distinct()[:3])
                for p in recent:
                    sample = list(MasterRecord.objects.filter(source_type=st, period=p)
                                  .exclude(invoice_number='')
                                  .values_list('invoice_number', flat=True)[:3])
                    self.stdout.write(f'  [主表 {p}] 发票号样本：' + ' | '.join(map(str, sample)))
            for no, mid in list(plan.items())[:10]:
                self.stdout.write(f'    发票号 {no} -> 主表 id={mid}')

            if not apply:
                self.stdout.write(self.style.WARNING('  （预览模式，未写库）加 --apply 执行。'))
                continue

            done = 0
            with transaction.atomic():
                for no, mid in plan.items():
                    done += InvoiceLineItem.objects.filter(
                        source_upload__source_type=st, master_record__isnull=True,
                        invoice_number=no).update(master_record_id=mid)
            self.stdout.write(self.style.SUCCESS(f'  已挂接 {done} 条明细行。'))
