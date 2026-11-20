"""导出主表（MasterRecord）为 Excel，便于核对。

用法：
    python3 manage.py export_master                 # 导出到 exports/master_YYYYMMDD_HHMMSS.xlsx
    python3 manage.py export_master --output a.xlsx # 指定输出文件
    python3 manage.py export_master --source sales_invoice   # 只导某个来源
    python3 manage.py export_master --year 2024             # 只导某年

导出内容含两张表：
    1) 明细：全部主表记录（按 发生时间, id 排序）。
    2) 汇总：按来源/年份的条数、借方合计、贷方合计，以及总合计。
"""
import os
from django.core.management.base import BaseCommand
from django.db.models import Sum, Count
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

from books.models import MasterRecord

COLUMNS = [
    ('occur_date', '发生时间'),
    ('source_type', '来源'),
    ('unit_name', '单位名称'),
    ('biz_type', '业务类型'),
    ('period', '期间'),
    ('year', '年份'),
    ('category', '类别'),
    ('debit', '借方'),
    ('credit', '贷方'),
    ('tag', '标记'),
    ('handler', '经手人/业务员'),
    ('tax_no', '税号'),
    ('invoice_number', '发票号码'),
    ('blue_invoice_no', '对应蓝字发票号'),
    ('remark', '备注'),
]

CHOICE_FIELDS = {'source_type', 'biz_type', 'category', 'tag'}


class Command(BaseCommand):
    help = '导出主表 MasterRecord 为 Excel（明细 + 汇总），用于核对'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='', help='输出 xlsx 路径，缺省为 exports/master_<时间戳>.xlsx')
        parser.add_argument('--source', default='', help='只导指定来源（如 sales_invoice/purchase_invoice/bank/other）')
        parser.add_argument('--year', default='', help='只导指定年份（如 2024）')

    def _display(self, obj, field):
        val = getattr(obj, field)
        if field in CHOICE_FIELDS:
            return getattr(obj, 'get_%s_display' % field)()
        return val

    def handle(self, *args, **options):
        qs = MasterRecord.objects.all().order_by('occur_date', 'id')
        if options['source']:
            qs = qs.filter(source_type=options['source'])
        if options['year']:
            qs = qs.filter(year=int(options['year']))

        total = qs.count()
        if total == 0:
            self.stderr.write('没有符合条件的主表记录')
            return

        out = options['output'] or os.path.join(
            'exports', 'master_%s.xlsx' % _ts())
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)

        wb = Workbook()

        # ---- 明细表 ----
        ws = wb.active
        ws.title = '主表明细'
        head_fill = PatternFill('solid', fgColor='4472C4')
        head_font = Font(bold=True, color='FFFFFF')
        for c, (_, label) in enumerate(COLUMNS, 1):
            cell = ws.cell(1, c, label)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal='center')
        ws.freeze_panes = 'A2'

        r = 2
        for obj in qs.iterator(chunk_size=2000):
            for c, (field, _) in enumerate(COLUMNS, 1):
                v = self._display(obj, field)
                if field == 'occur_date' and v is not None:
                    v = v.strftime('%Y-%m-%d')
                ws.cell(r, c, v)
            r += 1

        widths = [12, 16, 30, 10, 8, 7, 10, 14, 14, 10, 14, 20, 18, 18, 40]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # ---- 汇总表 ----
        s_ws = wb.create_sheet('汇总')
        agg = qs.values('source_type', 'year').annotate(
            cnt=Count('id'),
            debit=Sum('debit'),
            credit=Sum('credit')).order_by('source_type', 'year')

        headers = ['来源', '年份', '条数', '借方合计', '贷方合计']
        for c, h in enumerate(headers, 1):
            cell = s_ws.cell(1, c, h)
            cell.font = head_font
            cell.fill = head_fill
        rr = 2
        g_cnt = g_debit = g_credit = 0
        for row in agg:
            s_ws.cell(rr, 1, MasterRecord(source_type=row['source_type']).get_source_type_display())
            s_ws.cell(rr, 2, row['year'])
            s_ws.cell(rr, 3, row['cnt'])
            s_ws.cell(rr, 4, float(row['debit'] or 0))
            s_ws.cell(rr, 5, float(row['credit'] or 0))
            g_cnt += row['cnt']
            g_debit += float(row['debit'] or 0)
            g_credit += float(row['credit'] or 0)
            rr += 1
        s_ws.cell(rr, 1, '全部合计')
        s_ws.cell(rr, 3, g_cnt)
        s_ws.cell(rr, 4, g_debit)
        s_ws.cell(rr, 5, g_credit)
        for c in range(1, 6):
            s_ws.cell(rr, c).font = Font(bold=True)
        for i, w in enumerate([16, 8, 10, 16, 16], 1):
            s_ws.column_dimensions[get_column_letter(i)].width = w

        wb.save(out)
        self.stdout.write(self.style.SUCCESS(
            f'已导出 {total} 条主表记录到 {os.path.abspath(out)}'))


def _ts():
    import datetime
    return datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
