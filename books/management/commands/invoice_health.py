"""按期间体检发票主表与货物明细的挂接情况。

用于排查「某期发票看不到货物明细」：一眼看出是主表没有发票号、明细行根本没解析出来，
还是明细行没挂到主表上。

用法：
    python3 manage.py invoice_health                     # 最近 6 个期间
    python3 manage.py invoice_health --periods 12
    python3 manage.py invoice_health --source-type purchase_invoice
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from books.models import MasterRecord, InvoiceLineItem, SourceUpload

TYPES = ('sales_invoice', 'purchase_invoice')
LABEL = {'sales_invoice': '销项', 'purchase_invoice': '进项'}


class Command(BaseCommand):
    help = '按期间体检发票主表与货物明细的挂接情况（排查某期看不到货物明细）'

    def add_arguments(self, parser):
        parser.add_argument('--periods', type=int, default=6,
                            help='显示最近 N 个期间（默认 6，0=全部）')
        parser.add_argument('--source-type', default=None,
                            help='仅看指定来源(sales_invoice/purchase_invoice)，默认两类都看')
        parser.add_argument('--uploads', type=int, default=0,
                            help='追加显示最近 N 次导入的主表行/明细行（默认不显示）')
        parser.add_argument('--no-lines', type=int, default=0,
                            help='列出没有货物明细的主表：按客户汇总 Top N（0=不显示）')
        parser.add_argument('--unit', default='',
                            help='配合 --no-lines：只看指定客户（模糊匹配）')

    def handle(self, *args, **options):
        n = options['periods']
        types = [options['source_type']] if options['source_type'] else list(TYPES)

        for st in types:
            ids = MasterRecord.objects.filter(source_type=st)
            orphan = InvoiceLineItem.objects.filter(
                source_upload__source_type=st, master_record__isnull=True).count()
            self.stdout.write(
                f"\n=== {LABEL.get(st, st)}（{st}）主表 {ids.count()} 条，"
                f"明细行 {InvoiceLineItem.objects.filter(source_upload__source_type=st).count()} 条，"
                f"未挂主表的明细 {orphan} 条 ===")
            self.stdout.write('期间      主表   有票号  有明细主表  明细行')
            periods = (ids.exclude(period='').values_list('period', flat=True)
                       .distinct().order_by('-period'))
            if n:
                periods = list(periods[:n])
            for p in periods:
                ms = ids.filter(period=p)
                with_no = ms.exclude(invoice_number='').count()
                with_lines = ms.filter(line_items__isnull=False).distinct().count()
                lines = InvoiceLineItem.objects.filter(
                    master_record__source_type=st, master_record__period=p).count()
                flag = '' if (ms.count() and with_lines) else '   <-- 无明细'
                self.stdout.write(
                    f'{p}  {ms.count():>6}  {with_no:>6}  {with_lines:>9}  {lines:>6}{flag}')

            if options['no_lines']:
                empty = (ids.annotate(n=Count('line_items')).filter(n=0))
                if options['unit']:
                    empty = empty.filter(unit_name__icontains=options['unit'])
                self.stdout.write(
                    f'  -- 无货物明细的主表 {empty.count()} 条'
                    + (f'（客户含“{options["unit"]}”）' if options['unit'] else ''))
                self.stdout.write('     按客户 Top %d：' % options['no_lines'])
                for r in (empty.values('unit_name').annotate(c=Count('id'))
                          .order_by('-c')[:options['no_lines']]):
                    self.stdout.write(f'       {r["unit_name"]}  {r["c"]} 条')
                self.stdout.write('     按期间：')
                for r in (empty.exclude(period='').values('period').annotate(c=Count('id'))
                          .order_by('-period')[:12]):
                    self.stdout.write(f'       {r["period"]}  {r["c"]} 条')
                self.stdout.write('     样本：')
                for r in empty.order_by('-occur_date')[:20]:
                    self.stdout.write(
                        f'       {r.period} {r.occur_date} {r.unit_name} '
                        f'借{r.debit} 贷{r.credit} 票号={r.invoice_number or "-"}')

        if options['uploads']:
            self.stdout.write('\n=== 最近导入（主表行 / 明细行 / 未挂明细）===')
            ups = SourceUpload.objects.filter(source_type__in=types).order_by('-id')
            for up in ups[:options['uploads']]:
                lines = InvoiceLineItem.objects.filter(source_upload=up)
                self.stdout.write(
                    f'  #{up.id} [{LABEL.get(up.source_type, up.source_type)}] '
                    f'{up.mode or "full"} rows={up.row_count} 主表='
                    f'{MasterRecord.objects.filter(source_upload=up).count()} '
                    f'明细={lines.count()} 未挂={lines.filter(master_record__isnull=True).count()}'
                    f' | {up.uploaded_at:%Y-%m-%d %H:%M} | {up.file_name[-38:]}')
