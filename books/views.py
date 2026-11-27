"""往来账管理系统 —— 视图。"""
import secrets as _secrets
from datetime import date, timedelta
from django.conf import settings
from django.contrib.auth import login as auth_login, get_user_model
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView, redirect_to_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy, reverse
from django.core.paginator import Paginator
import pandas as pd
from django.db.models import Q, Sum, Count, F, Window, Min, Max
from django.db import transaction, IntegrityError
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.http import urlencode
from urllib.parse import quote as urlquote

from books.models import (
    SourceUpload, MasterRecord, OpeningBalance, AcceptanceOpeningBalance, SOURCE_TYPE_CHOICES,
    BIZ_TYPE_CHOICES, TAG_CHOICES, CustomerSalesperson, NameMapping, EXCLUDED_TAGS,
    SalespersonChangeReminder,
    InvoiceLineItem, log_operation,
    Department, SalespersonProfile, Merchant, ReceivingAccount, Counterparty,
    Order, OrderPayment, OrderDetail, AccountNameMapping, AccountNameAlias,
    InvitedPhone, SmsCode, MATCH_STATUS_CHOICES,
    CustomerEntity, CustomerAlias, Owner, OwnerAccountMap, PaymentReceipt,
    ShopBusinessRegistry, CollectionReminder, ShopStatement,
    PAY_METHOD_CHOICES, CHANNEL_TYPE_CHOICES, CUSTOMER_ALIAS_TYPE_CHOICES,
)
from books.sms import send_sms
from books.imports.utils import normalize_account_name
from books.decorators import (
    require_admin, get_visible_queryset, get_user_role, visible_unit_names,
    AdminRequiredMixin, ROLE_ADMIN, ROLE_SALESPERSON,
    require_roles, ROLE_EXECUTIVE, ROLE_MERCHANT, ROLE_DEPARTMENT, ROLE_QUERY,
    visible_order_queryset,
)
from books.forms import NameMappingForm, CustomerSalespersonForm, CustomerSalespersonCreditTermForm
from books.imports.engine import (
    run_import, rerun_import, to_date, _norm_invoice_no, INVOICE_NO_CANDIDATES,
)
from books.imports.fund_handler import import_fund_handler
from books.imports.utils import apply_name_mapping_to_history
from books.templatetags.money import cny
from books import sorting


# ---------------------------------------------------------------------------
# 表头排序字段白名单（key -> (ORM 字段或字段元组, 首次点击方向)）
# ---------------------------------------------------------------------------
INVOICE_SEARCH_SORT = {
    'date': ('occur_date', sorting.DESC), 'unit': ('unit_name', sorting.ASC),
    'biz_type': ('biz_type', sorting.ASC), 'status': ('category', sorting.ASC),
    'debit': ('debit', sorting.DESC), 'credit': ('credit', sorting.DESC),
    'tag': ('tag', sorting.ASC), 'invoice_no': ('invoice_number', sorting.ASC),
    'handler': ('handler', sorting.ASC), 'source': ('source_type', sorting.ASC),
}
GLOBAL_SEARCH_SORT = {
    'date': ('occur_date', sorting.DESC), 'unit': ('unit_name', sorting.ASC),
    'biz_type': ('biz_type', sorting.ASC), 'category': ('category', sorting.ASC),
    'debit': ('debit', sorting.DESC), 'credit': ('credit', sorting.DESC),
    'tag': ('tag', sorting.ASC), 'handler': ('handler', sorting.ASC),
    'invoice_no': ('invoice_number', sorting.ASC), 'remark': ('remark', sorting.ASC),
    'source': ('source_type', sorting.ASC),
}
STATEMENT_SORT = {
    'date': ('occur_date', sorting.DESC), 'biz_type': ('biz_type', sorting.ASC),
    'category': ('category', sorting.ASC), 'debit': ('debit', sorting.DESC),
    'credit': ('credit', sorting.DESC), 'handler': ('handler', sorting.ASC),
    'tag': ('tag', sorting.ASC), 'invoice_no': ('invoice_number', sorting.ASC),
    'source': ('source_type', sorting.ASC), 'remark': ('remark', sorting.ASC),
}
IMPORT_LIST_SORT = {
    'id': ('id', sorting.DESC), 'source_type': ('source_type', sorting.ASC),
    'file_name': ('file_name', sorting.ASC),
    'uploaded_by': ('uploaded_by__username', sorting.ASC),
    'uploaded_at': ('uploaded_at', sorting.DESC), 'status': ('status', sorting.ASC),
    'row_count': ('row_count', sorting.DESC),
}
INVOICE_DETAIL_SORT = {
    'goods_name': ('goods_name', sorting.ASC), 'spec': ('spec', sorting.ASC),
    'unit': ('unit', sorting.ASC), 'quantity': ('quantity', sorting.DESC),
    'price': ('price', sorting.DESC), 'amount': ('amount', sorting.DESC),
    'tax_rate': ('tax_rate', sorting.ASC), 'tax': ('tax', sorting.DESC),
}
# 账龄/催款共用的金额列（列表排序，值是 dict 的键）
_BUCKET_SORT = {
    'balance': ('balance', sorting.DESC), 'overdue': ('overdue', sorting.DESC),
    'b1': ('b1', sorting.DESC), 'b2': ('b2', sorting.DESC),
    'b3': ('b3', sorting.DESC), 'b4': ('b4', sorting.DESC),
}
AGING_SORT = {'unit': ('unit', sorting.ASC), 'term': ('credit_term', sorting.DESC), **_BUCKET_SORT}
COLLECTION_SORT = {
    'salesperson': ('salesperson', sorting.ASC), 'unit': ('unit', sorting.ASC), **_BUCKET_SORT}
SALES_PERF_SORT = {
    'salesperson': ('salesperson', sorting.ASC), 'unit': ('unit', sorting.ASC),
    'invoice': ('invoice', sorting.DESC), 'collect': ('collect', sorting.DESC),
    'unpaid': ('unpaid', sorting.DESC),
}
DEPOSIT_SUM_SORT = {
    'unit_name': ('unit_name', sorting.ASC), 'recv': ('recv', sorting.DESC),
    'ret': ('ret', sorting.DESC), 'balance': ('balance', sorting.DESC),
    'cnt': ('cnt', sorting.DESC),
}
DEPOSIT_DET_SORT = {
    'date': ('date', sorting.DESC), 'unit': ('unit', sorting.ASC),
    'biz_type': ('biz_type', sorting.ASC), 'debit': ('debit', sorting.DESC),
    'credit': ('credit', sorting.DESC), 'handler': ('handler', sorting.ASC),
    'invoice_no': ('invoice_no', sorting.ASC), 'source_type': ('source_type', sorting.ASC),
    'remark': ('remark', sorting.ASC),
}
# 票据检索页三张表各自独立排序（参数前缀不同，互不干扰）
_AMOUNT_SUM_SORT = {
    'recv': ('recv', sorting.DESC), 'pay': ('pay', sorting.DESC),
    'net': ('net', sorting.DESC), 'cnt': ('cnt', sorting.DESC),
}
ACCEPTANCE_PKG_SORT = {
    'package_no': ('package_no', sorting.ASC), 'unit': ('unit', sorting.ASC),
    **_AMOUNT_SUM_SORT}
ACCEPTANCE_SUM_SORT = {'unit_name': ('unit_name', sorting.ASC), **_AMOUNT_SUM_SORT}
ACCEPTANCE_DET_SORT = {
    'date': ('occur_date', sorting.DESC), 'package_no': ('package_no', sorting.ASC),
    'unit_name': ('unit_name', sorting.ASC), 'kind': ('tag', sorting.ASC),
    'direction': ('category', sorting.ASC), 'debit': ('debit', sorting.DESC),
    'credit': ('credit', sorting.DESC), 'handler': ('handler', sorting.ASC),
    'remark': ('remark', sorting.ASC),
}
# 穿透明细：原始列里只有这几列在主表有对应字段，排序走 ORM（原始列按首条记录铺开，
# 页内重排会与分页冲突）；其余原始列保持不可排序。
ACCEPTANCE_RAW_SORT = {
    '交易日期': ('occur_date', sorting.ASC), '票据包号码': ('package_no', sorting.ASC),
    '交易对手名称': ('unit_name', sorting.ASC), '操作类型': ('category', sorting.ASC),
    '业务类型': ('biz_type', sorting.ASC), '备注': ('remark', sorting.ASC),
}
ACCEPTANCE_REPORT_SORT = {
    'period': ('period', sorting.ASC), 'recv': ('recv', sorting.DESC),
    'pay': ('pay', sorting.DESC), 'net': ('net', sorting.DESC),
}
# 对账函：余额按日期滚动累计，不参与排序
RECONCILE_SORT = {
    'date': ('date', sorting.DESC), 'biz_type': ('biz_type', sorting.ASC),
    'debit': ('debit', sorting.DESC), 'credit': ('credit', sorting.DESC),
    'invoice_no': ('invoice_no', sorting.ASC),
}

# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
class LoginView(LoginView):
    template_name = 'books/login.html'
    redirect_authenticated_user = True


class PasswordChangeView(PasswordChangeView):
    template_name = 'books/password_change.html'
    success_url = reverse_lazy('home')

    def form_valid(self, form):
        # 改密成功后清除「强制改密」标记，避免被中间件反复跳转
        profile = getattr(self.request.user, 'profile', None)
        if profile is not None and profile.must_change_password:
            profile.must_change_password = False
            profile.save(update_fields=['must_change_password'])
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# 首页
# ---------------------------------------------------------------------------
@login_required
def home(request):
    role = get_user_role(request.user)
    total = get_visible_queryset(request.user, MasterRecord.objects.all()).count()
    units = visible_unit_names(request.user)
    unit_count = len(units) if units is not None else MasterRecord.objects.values('unit_name').distinct().count()
    ctx = {
        'role': role,
        'record_count': total,
        'unit_count': unit_count,
    }
    return render(request, 'books/home.html', ctx)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
@require_admin
def import_upload(request):
    if request.method == 'POST':
        st = request.POST.get('source_type')
        f = request.FILES.get('file')
        account_type = request.POST.get('account_type', '').strip()
        # 个人银行流水整批打 tag='个人'，便于与对公流水在报表/往来账中区分
        tag = '个人' if account_type == 'personal' else ''
        if not st or not f:
            messages.error(request, '请选择来源类型并上传 Excel 文件。')
        else:
            if st == 'fund_handler':
                # 容错：若上传的其实是「收款明细」（富掌柜/招行表头），自动走收款明细导入器，
                # 避免用户选错来源导致「尚未配置 other 列映射」的困惑。
                from books.imports.payment_receipt import detect_template
                detected = None
                try:
                    xls_tmp = pd.ExcelFile(f, engine='openpyxl')
                    for sn in xls_tmp.sheet_names:
                        head = xls_tmp.parse(sn, header=None, nrows=6)
                        cols = set()
                        for _, r in head.iterrows():
                            cols |= {str(c).strip() for c in r.tolist() if c is not None}
                        detected, _ = detect_template(cols)
                        if detected:
                            break
                    f.seek(0)
                except Exception:
                    detected = None
                    try: f.seek(0)
                    except Exception: pass
                if detected:
                    from books.imports.payment_receipt import run_import_payment_receipt
                    upload = run_import_payment_receipt(f, request.user)
                    if upload.status == 'success':
                        messages.info(request, f'检测到该文件为「{detected}」收款明细，已按收款明细导入（而非资金明细业务员表）。')
                else:
                    # 资金明细业务员表走专用导入：复用「other」列映射，写入独立表
                    # FundReceiptHandler（避免成为主表重复交易），随后按表回填银行经手人。
                    upload = import_fund_handler(f, request.user)
            elif st in ('lakala_order', 'lakala_order_api'):
                # 拉卡拉订单走 B2B 订单导入器（Order + 归一），而不是旧 MasterRecord。
                # 用户在「导入数据」页误选该来源时也能正确落库。
                from books.imports.lakala import run_import_lakala
                upload = run_import_lakala(st, f, request.user, tag=tag)
            else:
                upload = run_import(st, f, request.user, tag=tag)
            if upload.status == 'failed':
                messages.error(request, f'导入失败：{upload.note}')
                log_operation(request.user, '导入文件失败', target_type='SourceUpload',
                              target_id=str(upload.id), detail=f'来源={st}；{upload.note}', request=request)
            else:
                if st == 'fund_handler':
                    msg = f'资金明细业务员表导入完成，共 {upload.row_count} 行。'
                    try:
                        from io import StringIO
                        from django.core.management import call_command
                        buf = StringIO()
                        call_command('match_bank_handlers', stdout=buf, stderr=buf)
                        tail = [l for l in buf.getvalue().splitlines() if l.strip()]
                        if tail:
                            msg += ' ' + tail[-1]
                    except Exception as exc:  # 匹配失败不阻断导入结果
                        msg += f'（经手人匹配未执行：{exc}）'
                elif st in ('lakala_order', 'lakala_order_api'):
                    msg = f'拉卡拉订单导入完成，新增 {upload.row_count} 单。' + \
                          (f' {upload.note}' if upload.note else '')
                else:
                    msg = f'导入完成，新增 {upload.row_count} 条主表记录。'
                if upload.note:
                    msg += ' ' + upload.note
                messages.success(request, msg)
                log_operation(request.user, '导入文件', target_type='SourceUpload',
                              target_id=str(upload.id), detail=f'来源={st}；{msg}', request=request)
        return redirect('import_list')
    return render(request, 'books/import_upload.html', {'source_types': SOURCE_TYPE_CHOICES})


@login_required
def import_list(request):
    uploads, sort = sorting.sort_queryset(
        SourceUpload.objects.all(), request.GET, fields=IMPORT_LIST_SORT,
        default=('-uploaded_at',), default_key='uploaded_at', default_dir=sorting.DESC)
    role = get_user_role(request.user)
    return render(request, 'books/import_list.html',
                  {'uploads': uploads, 'role': role, 'sort': sort,
                   'source_type_choices': [c for c in SOURCE_TYPE_CHOICES if c[0] != 'fund_handler']})


@require_admin
def import_delete(request, pk):
    if request.method == 'POST':
        u = get_object_or_404(SourceUpload, pk=pk)
        MasterRecord.objects.filter(source_upload=u).delete()
        u.delete()
        messages.success(request, '已删除该导入及其主表记录。')
        log_operation(request.user, '删除导入', target_type='SourceUpload',
                      target_id=str(pk), detail='删除导入及其主表记录', request=request)
    return redirect('import_list')


@require_admin
def import_rerun(request, pk):
    if request.method == 'POST':
        u = get_object_or_404(SourceUpload, pk=pk)
        rerun_import(u.id, request.user)
        messages.success(request, f'已按当前映射重跑，主表记录 {u.row_count} 条。')
        log_operation(request.user, '重跑导入', target_type='SourceUpload',
                      target_id=str(pk), detail=f'重跑后主表记录 {u.row_count} 条', request=request)
    return redirect('import_list')


@require_POST
@require_admin
def namemap_apply_all(request):
    """将当前名称映射库应用到历史单位名称（可按来源类型筛选，原地归一）。"""
    valid = {c[0] for c in SOURCE_TYPE_CHOICES}
    source_types = [s for s in request.POST.getlist('source_types') if s in valid]
    summary = apply_name_mapping_to_history(source_types=source_types or None)
    if source_types:
        labels = dict(SOURCE_TYPE_CHOICES)
        scope = '、'.join(labels.get(s, s) for s in source_types)
    else:
        scope = '全部来源'
    parts = [f'{k} {v} 条' for k, v in summary.items() if not k.endswith('_dedup_removed')]
    msg = f'已按名称映射库归一 [{scope}] 历史单位名称：' + '；'.join(parts)
    if summary.get('CustomerSalesperson_dedup_removed'):
        msg += f"；客户业务员去重 {summary['CustomerSalesperson_dedup_removed']} 条"
    messages.success(request, msg)
    log_operation(request.user, '应用名称映射', target_type='NameMapping',
                  detail=f'[{scope}] ' + '；'.join(parts), request=request)
    return redirect('namemap_list')


# ---------------------------------------------------------------------------
# 客户往来明细（MVP 完整功能）
# ---------------------------------------------------------------------------
def _window_start_date(date_from, period='', year=''):
    """明细窗口的起始日：优先显式 date_from，其次期间/年份的 1 号。

    返回 None 表示不限窗口（查全部历史），此时期初就是最初余额。
    """
    if date_from:
        return date_from
    if period and len(period) == 6 and period.isdigit():
        try:
            return date(int(period[:4]), int(period[4:]), 1)
        except ValueError:
            return None
    if year and str(year).isdigit():
        try:
            return date(int(year), 1, 1)
        except ValueError:
            return None
    return None


def _statement_opening(base_qs, unit, start_date, biz='', tag=''):
    """期初余额 = 期初表(窗口前) + 主表(窗口前) 的借贷差额。

    必须与对账单 `_build_reconcile` 口径一致：只取 OpeningBalance 会把窗口之前
    已入账的发生额漏掉，导致选了起始日期后期初仍是「最初金额」、余额对不上。
    start_date 为 None 时保持旧行为（期初表全部，且明细本来就是全量）。
    """
    ob = OpeningBalance.objects.filter(unit_name=unit)
    if start_date:
        ob = ob.filter(occur_date__lt=start_date)
    opening = (float(ob.aggregate(s=Sum('debit'))['s'] or 0)
               - float(ob.aggregate(s=Sum('credit'))['s'] or 0))
    if not start_date:
        return opening
    before = base_qs.filter(unit_name=unit, occur_date__lt=start_date)
    if biz:
        before = before.filter(biz_type=biz)
    if tag:
        before = before.filter(tag=tag)
    if tag not in EXCLUDED_TAGS:
        before = before.exclude(tag__in=EXCLUDED_TAGS)
    agg = before.aggregate(d=Sum('debit'), c=Sum('credit'))
    return opening + float(agg['d'] or 0) - float(agg['c'] or 0)


@login_required
def statement(request):
    role = get_user_role(request.user)
    qs = get_visible_queryset(request.user, MasterRecord.objects.all())

    # 可选项：可见单位名称
    unit_choices = sorted(set(qs.values_list('unit_name', flat=True)))

    unit = request.GET.get('unit_name', '').strip()
    year = request.GET.get('year', '').strip()
    period = request.GET.get('period', '').strip()
    biz = request.GET.get('biz_type', '').strip()
    tag = request.GET.get('tag', '').strip()
    edit = request.GET.get('edit') == '1' and role == '管理员'
    # 不设置默认时间范围：date_from 缺省时查全部，避免“默认近一年”误导用户
    date_from_raw = request.GET.get('date_from', '').strip()
    date_to_raw = request.GET.get('date_to', '').strip()
    date_from = to_date(date_from_raw)
    date_to = to_date(date_to_raw)

    # 每页行数：默认 50，用户可调整（20/50/100/200）
    per_page_choices = [20, 50, 100, 200]
    try:
        per_page = int(request.GET.get('per_page', 50))
    except (TypeError, ValueError):
        per_page = 50
    if per_page not in per_page_choices:
        per_page = 50

    rows = []
    opening = 0
    totals = {'debit': 0, 'credit': 0}
    page_obj = None
    sort = None
    if unit:
        q = qs.filter(unit_name=unit)
        if year:
            q = q.filter(year=int(year))
        if period:
            q = q.filter(period=period)
        if biz:
            q = q.filter(biz_type=biz)
        if tag:
            q = q.filter(tag=tag)
        # date_from/date_to 为空时不加时间范围（查全部），不再默认近一年
        if date_from:
            q = q.filter(occur_date__gte=date_from)
        if date_to:
            q = q.filter(occur_date__lte=date_to)
        # 作废发票不参与往来与统计（历史遗留的作废记录仍在此过滤；新导入已不再入库）。
        # 红冲（红字/负数）必须纳入，以保证借贷余额等统计完整。
        if tag not in EXCLUDED_TAGS:
            q = q.exclude(tag__in=EXCLUDED_TAGS)
        q = q.order_by('occur_date', 'id')

        # 期初余额：起始日之前的期初表 + 主表发生额（与对账单口径一致）
        opening = _statement_opening(qs, unit, _window_start_date(date_from, period, year),
                                     biz=biz, tag=tag)

        # 全量合计（跨页统计，合计行反映整个筛选结果而非当前页）
        agg = q.aggregate(d=Sum('debit'), c=Sum('credit'))
        totals['debit'] = float(agg['d'] or 0)
        totals['credit'] = float(agg['c'] or 0)

        # 运行余额：用数据库窗口函数按发生序累计，分页后每行的余额仍正确
        q = q.annotate(running_balance=Window(
            expression=Sum(F('debit') - F('credit')),
            order_by=[F('occur_date'), F('id')],
        ))
        # 表头排序只改显示顺序；窗口函数仍按发生日累计，每行余额口径不变
        q, sort = sorting.sort_queryset(q, request.GET, fields=STATEMENT_SORT,
                                        default=('occur_date', 'id'),
                                        default_key='date', default_dir=sorting.ASC)
        paginator = Paginator(q, per_page)
        page_obj = paginator.get_page(request.GET.get('page'))
        for r in page_obj:
            balance = opening + float(r.running_balance)
            rows.append({
                'id': r.id,
                'date': r.occur_date,
                'biz_type': r.biz_type,
                'category': r.category,
                'debit': r.debit,
                'credit': r.credit,
                'balance': balance,
                'handler': r.handler,
                'tag': r.tag,
                'remark': r.remark,
                'invoice_no': r.invoice_number,
                'source_type': r.get_source_type_display(),
            })

    # 过滤条件拼成 query string（不含 page/per_page），供导出、编辑、翻页链接复用
    filter_params = []
    if unit:
        filter_params.append('unit_name=%s' % urlquote(unit))
    if year:
        filter_params.append('year=%s' % year)
    if period:
        filter_params.append('period=%s' % period)
    if biz:
        filter_params.append('biz_type=%s' % urlquote(biz))
    if tag:
        filter_params.append('tag=%s' % urlquote(tag))
    # 默认近 1 年也会写入查询串，使翻页/导出与当前明细窗口一致
    if date_from:
        filter_params.append('date_from=%s' % date_from.strftime('%Y-%m-%d'))
    if date_to:
        filter_params.append('date_to=%s' % date_to.strftime('%Y-%m-%d'))
    filter_qs = '&'.join(filter_params)
    # 编辑模式下翻页/改每页时保持编辑态（切换编辑态的链接单独拼 edit）
    # 排序只进 filter_qs_edit：导出链接（filter_qs）保持原有日期口径
    filter_qs_edit = '&'.join(filter_params
                              + (sort.params() if sort else [])
                              + (['edit=1'] if edit else []))

    ctx = {
        'role': role,
        'unit_choices': unit_choices,
        'years': sorted({y for y in qs.values_list('year', flat=True) if y}),
        'biz_types': BIZ_TYPE_CHOICES,
        'tags': TAG_CHOICES,
        'filters': {
            'unit_name': unit, 'year': year, 'period': period, 'biz_type': biz, 'tag': tag,
            'date_from': date_from.strftime('%Y-%m-%d') if date_from else '',
            'date_to': date_to.strftime('%Y-%m-%d') if date_to else '',
        },
        'edit': edit,
        'next': request.get_full_path(),
        'back_query': request.GET.get('back', ''),
        'rows': rows,
        'opening': opening,
        'totals': totals,
        'ending': opening + totals['debit'] - totals['credit'],
        'page_obj': page_obj,
        'per_page': per_page,
        'per_page_choices': per_page_choices,
        'filter_qs': filter_qs,
        'filter_qs_edit': filter_qs_edit,
        'sort': sort,
    }
    return render(request, 'books/statement.html', ctx)


@login_required
def statement_export(request):
    unit = request.GET.get('unit_name', '').strip()
    if not unit:
        return HttpResponse('请先选择单位名称', status=400)
    role = get_user_role(request.user)
    if role == '业务员' and unit not in visible_unit_names(request.user):
        return HttpResponse('无权限导出该客户', status=403)

    # base_qs 保留未加筛选的可见集，用于计算期初（窗口之前的记录不在此处的 qs 里）
    base_qs = get_visible_queryset(request.user, MasterRecord.objects.all())
    qs = base_qs.filter(unit_name=unit)
    year = request.GET.get('year', '').strip()
    period = request.GET.get('period', '').strip()
    biz = request.GET.get('biz_type', '').strip()
    tag = request.GET.get('tag', '').strip()
    # 日期范围与页面保持一致（页面把 date_from/date_to 拼进了导出链接）
    date_from = to_date(request.GET.get('date_from', '').strip())
    date_to = to_date(request.GET.get('date_to', '').strip())
    if year:
        qs = qs.filter(year=int(year))
    if period:
        qs = qs.filter(period=period)
    if biz:
        qs = qs.filter(biz_type=biz)
    if tag:
        qs = qs.filter(tag=tag)
    if date_from:
        qs = qs.filter(occur_date__gte=date_from)
    if date_to:
        qs = qs.filter(occur_date__lte=date_to)
    # 作废发票不导出；红冲纳入以保证统计完整
    if tag not in EXCLUDED_TAGS:
        qs = qs.exclude(tag__in=EXCLUDED_TAGS)
    qs = qs.order_by('occur_date', 'id')

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from django.utils import timezone

    wb = Workbook()
    ws = wb.active
    ws.title = '客户往来明细'
    title_font = Font(bold=True, size=14)
    hdr_font = Font(bold=True)
    hdr_fill = PatternFill('solid', fgColor='DDEBF7')

    ws.cell(1, 1, f'客户往来明细 - {unit}').font = title_font
    ws.cell(2, 1, f'导出时间：{timezone.now():%Y-%m-%d %H:%M}')

    # 期初：与页面同一口径（起始日之前的期初表 + 主表发生额）
    opening = _statement_opening(base_qs, unit, _window_start_date(date_from, period, year),
                                 biz=biz, tag=tag)

    headers = ['日期', '业务类型', '类别', '借方金额', '贷方金额', '余额', '经手人/业务员', '标记', '发票号', '备注', '来源']
    hrow = 4
    for c, h in enumerate(headers, 1):
        cell = ws.cell(hrow, c, h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal='center')

    def write_row(r, date, biz_t, cat, debit, credit, bal, handler, tag_v, inv, remark, src):
        ws.cell(r, 1, date)
        ws.cell(r, 2, biz_t)
        ws.cell(r, 3, cat)
        ws.cell(r, 4, debit)
        ws.cell(r, 5, credit)
        ws.cell(r, 6, bal)
        ws.cell(r, 7, handler)
        ws.cell(r, 8, tag_v)
        ws.cell(r, 9, inv)
        ws.cell(r, 10, remark)
        ws.cell(r, 11, src)

    r = hrow + 1
    write_row(r, '期初余额', '', '', '', '', round(opening, 2), '', '', '', '', '')
    r += 1
    balance = opening
    for m in qs:
        balance += float(m.debit) - float(m.credit)
        write_row(r, m.occur_date, m.biz_type, m.category, float(m.debit), float(m.credit),
                  round(balance, 2), m.handler, m.tag, m.invoice_number, m.remark,
                  m.get_source_type_display())
        r += 1
    # 合计
    agg = qs.aggregate(d=Sum('debit'), c=Sum('credit'))
    td = float(agg['d'] or 0)
    tc = float(agg['c'] or 0)
    write_row(r, '合计', '', '', round(td, 2), round(tc, 2), round(opening + td - tc, 2), '', '', '', '', '')

    # 列宽
    for c in range(1, 12):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 16
    ws.column_dimensions['J'].width = 30

    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{unit}_往来明细.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 占位页（其余 6 项功能，Phase 2 实现）
# ---------------------------------------------------------------------------
PLACEHOLDERS = [
    ('sales_repay', '业务员开票回款明细'),
    ('period', '账期管理'),
    ('reconcile', '对账单管理'),
    ('collect', '分业务员客户催款管理'),
    ('deposit', '保证金管理'),
    ('other', '其他功能'),
]


def _placeholder(request, title):
    return render(request, 'books/placeholder.html', {'title': title})


def placeholder_view(request, key):
    title = dict(PLACEHOLDERS).get(key, '功能')
    return _placeholder(request, title)


# ---------------------------------------------------------------------------
# 分业务员客户催款管理（账龄/账期分析）
#   按客户 FIFO 匹配发票(借)与回款(贷)，统计应收余额与账龄分桶。
# ---------------------------------------------------------------------------
def _parse_date(s):
    d = to_date(s)
    return d if d else timezone.localdate()


def _aging_buckets(open_items, as_of, credit_term):
    b1 = b2 = b3 = b4 = overdue = 0.0
    for it in open_items:
        rem = it['remaining']
        if rem <= 1e-9 or not it['date']:
            continue
        age = (as_of - it['date']).days
        if age <= 30:
            b1 += rem
        elif age <= 60:
            b2 += rem
        elif age <= 90:
            b3 += rem
        else:
            b4 += rem
        if age > credit_term:
            overdue += rem
    return b1, b2, b3, b4, overdue


def _consume(items, amount, positive=True):
    """按发生序用 amount 冲销队列里的未清项，返回未用完的金额。

    positive=True   冲减正项（应收）：回款、红冲发票
    positive=False  冲减负项（预收/多收）：后续开票抵扣、红冲回款
    """
    rem = amount
    for it in items:
        if rem <= 1e-9:
            break
        avail = it['remaining'] if positive else -it['remaining']
        if avail <= 1e-9:
            continue
        take = min(avail, rem)
        it['remaining'] += -take if positive else take
        rem -= take
    items[:] = [x for x in items if abs(x['remaining']) > 1e-9]
    return rem


def _settle_credits(items):
    """把队列里的负项（预收/多收）按发生序抵减最早的正项（应收）。

    抵消后 sum(remaining) 仍等于借贷净额，且正项合计恰为应收余额
    （余额为负时正项为空，账面是预收/多收，账龄各桶为 0）。
    """
    credit = sum(-x['remaining'] for x in items if x['remaining'] < -1e-9)
    if credit <= 1e-9:
        return
    for x in items:
        if credit <= 1e-9:
            break
        if x['remaining'] <= 1e-9:
            continue
        take = min(x['remaining'], credit)
        x['remaining'] -= take
        credit -= take
    items[:] = [x for x in items if abs(x['remaining']) > 1e-9]


def _customer_aging(unit_name, base_qs, as_of, credit_term):
    """按客户统计应收余额与账龄。

    余额口径：借贷净额 Σ(借方−贷方)，与往来明细账/对账函一致。红冲（负数金额）
    必须参与核销，否则应收虚高；预收/多收必须保留为负项，不能丢弃，否则后续
    开票无法被冲销。
    账龄口径：FIFO —— 先发生的应收先被回款与预收冲销，剩余未清项按发生日分桶。
    """
    recs = base_qs.filter(unit_name=unit_name, occur_date__isnull=False,
                          occur_date__lte=as_of).order_by('occur_date', 'id')
    items = []   # 未清项队列，remaining>0 为应收，<0 为预收/多收
    for r in recs:
        debit = float(r.debit or 0)
        credit = float(r.credit or 0)
        if debit > 0:
            # 先抵减已有预收，仍未抵完的部分才是新增应收
            rest = _consume(items, debit, positive=False)
            if rest > 1e-9:
                items.append({'date': r.occur_date, 'remaining': rest})
        elif debit < 0:
            # 红冲发票：冲减最早未清应收，超出部分转为预收
            rest = _consume(items, -debit, positive=True)
            if rest > 1e-9:
                items.append({'date': r.occur_date, 'remaining': -rest})
        if credit > 0:
            # 回款：冲减最早未清应收，多出部分记为预收（不再丢弃）
            rest = _consume(items, credit, positive=True)
            if rest > 1e-9:
                items.append({'date': r.occur_date, 'remaining': -rest})
        elif credit < 0:
            # 红冲回款：先冲抵预收，超出部分恢复为应收
            rest = _consume(items, -credit, positive=False)
            if rest > 1e-9:
                items.append({'date': r.occur_date, 'remaining': rest})
    _settle_credits(items)
    balance = sum(it['remaining'] for it in items)
    b1, b2, b3, b4, overdue = _aging_buckets(items, as_of, credit_term)
    return {'balance': balance, 'b1': b1, 'b2': b2, 'b3': b3, 'b4': b4, 'overdue': overdue}


def _build_collection(user, as_of, credit_term):
    """返回 (rows, totals)。"""
    from books.models import CustomerSalesperson
    role = get_user_role(user)
    base = get_visible_queryset(user, MasterRecord.objects.all()).exclude(tag__in=EXCLUDED_TAGS)
    visible = visible_unit_names(user)

    if role == '业务员':
        sps = [getattr(user.profile, 'salesperson_name', '')] if getattr(user.profile, 'salesperson_name', '') else []
    else:
        sps = sorted(set(CustomerSalesperson.objects.filter(is_current=True)
                         .values_list('salesperson', flat=True)))

    rows = []
    totals = {'balance': 0, 'b1': 0, 'b2': 0, 'b3': 0, 'b4': 0, 'overdue': 0}
    for sp in sps:
        if not sp:
            continue
        customers = CustomerSalesperson.objects.filter(salesperson=sp, is_current=True
                                                       ).values_list('unit_name', flat=True)
        for cu in customers:
            if visible is not None and cu not in visible:
                continue
            a = _customer_aging(cu, base, as_of, credit_term)
            rows.append({'salesperson': sp, 'unit': cu, **a})
            for k in totals:
                totals[k] += a[k]
    # 排序：业务员、应收余额降序
    rows.sort(key=lambda x: (x['salesperson'], -x['balance']))
    return rows, totals


@login_required
def collection(request):
    as_of = _parse_date(request.GET.get('as_of', ''))
    try:
        credit_term = int(request.GET.get('credit_term', 30))
    except (TypeError, ValueError):
        credit_term = 30

    rows, totals = _build_collection(request.user, as_of, credit_term)
    rows, sort = sorting.sort_rows(rows, request.GET, fields=COLLECTION_SORT,
                                   default_key='salesperson', default_dir=sorting.ASC)
    ctx = {
        'rows': rows,
        'totals': totals,
        'as_of': as_of,
        'credit_term': credit_term,
        'role': get_user_role(request.user),
        'sort': sort,
    }
    return render(request, 'books/collection.html', ctx)


@login_required
def collection_export(request):
    as_of = _parse_date(request.GET.get('as_of', ''))
    try:
        credit_term = int(request.GET.get('credit_term', 30))
    except (TypeError, ValueError):
        credit_term = 30
    rows, totals = _build_collection(request.user, as_of, credit_term)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '分业务员催款'
    ws.cell(1, 1, f'分业务员客户催款管理（截至 {as_of}，账期 {credit_term} 天）').font = Font(bold=True, size=14)
    headers = ['业务员', '客户', '应收余额', '逾期金额', '30天内', '31-60天', '61-90天', '90天以上']
    hrow = 3
    for c, h in enumerate(headers, 1):
        cell = ws.cell(hrow, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = hrow + 1
    for row in rows:
        ws.cell(r, 1, row['salesperson'])
        ws.cell(r, 2, row['unit'])
        ws.cell(r, 3, round(row['balance'], 2))
        ws.cell(r, 4, round(row['overdue'], 2))
        ws.cell(r, 5, round(row['b1'], 2))
        ws.cell(r, 6, round(row['b2'], 2))
        ws.cell(r, 7, round(row['b3'], 2))
        ws.cell(r, 8, round(row['b4'], 2))
        r += 1
    ws.cell(r, 1, '合计')
    ws.cell(r, 3, round(totals['balance'], 2))
    ws.cell(r, 4, round(totals['overdue'], 2))
    ws.cell(r, 5, round(totals['b1'], 2))
    ws.cell(r, 6, round(totals['b2'], 2))
    ws.cell(r, 7, round(totals['b3'], 2))
    ws.cell(r, 8, round(totals['b4'], 2))
    for c in range(1, 9):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="分业务员催款.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 账期管理 / 账龄分析（按客户，逾期按各客户自身账期判断）
# ---------------------------------------------------------------------------
@login_required
def aging(request):
    as_of = _parse_date(request.GET.get('as_of', ''))
    try:
        default_term = int(request.GET.get('default_term', 30))
    except (TypeError, ValueError):
        default_term = 30
    unit_filter = request.GET.get('unit_name', '').strip()

    base = get_visible_queryset(request.user, MasterRecord.objects.all()).exclude(tag__in=EXCLUDED_TAGS)
    q = base.filter(unit_name=unit_filter) if unit_filter else base
    units = sorted(set(q.values_list('unit_name', flat=True)))

    ct_map = dict(CustomerSalesperson.objects.filter(is_current=True)
                  .values_list('unit_name', 'credit_term'))
    rows = []
    totals = {'balance': 0, 'b1': 0, 'b2': 0, 'b3': 0, 'b4': 0, 'overdue': 0}
    for cu in units:
        ct = ct_map.get(cu, default_term)
        a = _customer_aging(cu, base, as_of, ct)
        rows.append({'unit': cu, 'credit_term': ct, **a})
        for k in totals:
            totals[k] += a[k]
    rows.sort(key=lambda x: -x['balance'])
    rows, sort = sorting.sort_rows(rows, request.GET, fields=AGING_SORT,
                                   default_key='balance', default_dir=sorting.DESC)
    ctx = {
        'rows': rows,
        'totals': totals,
        'as_of': as_of,
        'default_term': default_term,
        'unit_choices': sorted(set(base.values_list('unit_name', flat=True))),
        'filters': {'unit_name': unit_filter},
        'role': get_user_role(request.user),
        'sort': sort,
    }
    return render(request, 'books/aging.html', ctx)


@login_required
def aging_export(request):
    as_of = _parse_date(request.GET.get('as_of', ''))
    try:
        default_term = int(request.GET.get('default_term', 30))
    except (TypeError, ValueError):
        default_term = 30
    unit_filter = request.GET.get('unit_name', '').strip()

    base = get_visible_queryset(request.user, MasterRecord.objects.all()).exclude(tag__in=EXCLUDED_TAGS)
    q = base.filter(unit_name=unit_filter) if unit_filter else base
    units = sorted(set(q.values_list('unit_name', flat=True)))
    ct_map = dict(CustomerSalesperson.objects.filter(is_current=True)
                  .values_list('unit_name', 'credit_term'))

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '账龄分析'
    ws.cell(1, 1, f'账龄分析（截至 {as_of}）').font = Font(bold=True, size=14)
    headers = ['客户', '账期(天)', '应收余额', '逾期金额', '30天内', '31-60天', '61-90天', '90天以上']
    hrow = 3
    for c, h in enumerate(headers, 1):
        cell = ws.cell(hrow, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = hrow + 1
    tb = {'balance': 0, 'overdue': 0, 'b1': 0, 'b2': 0, 'b3': 0, 'b4': 0}
    for cu in units:
        ct = ct_map.get(cu, default_term)
        a = _customer_aging(cu, base, as_of, ct)
        ws.cell(r, 1, cu)
        ws.cell(r, 2, ct)
        ws.cell(r, 3, round(a['balance'], 2))
        ws.cell(r, 4, round(a['overdue'], 2))
        ws.cell(r, 5, round(a['b1'], 2))
        ws.cell(r, 6, round(a['b2'], 2))
        ws.cell(r, 7, round(a['b3'], 2))
        ws.cell(r, 8, round(a['b4'], 2))
        for k in tb:
            tb[k] += a[k]
        r += 1
    ws.cell(r, 1, '合计')
    ws.cell(r, 2, '')
    ws.cell(r, 3, round(tb['balance'], 2))
    ws.cell(r, 4, round(tb['overdue'], 2))
    ws.cell(r, 5, round(tb['b1'], 2))
    ws.cell(r, 6, round(tb['b2'], 2))
    ws.cell(r, 7, round(tb['b3'], 2))
    ws.cell(r, 8, round(tb['b4'], 2))
    for c in range(1, 9):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="账龄分析.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 对账单管理（按客户+期间出具对账单，含客户签章确认区）
# ---------------------------------------------------------------------------
def _get_letter_no(unit, year, ms, me):
    """只读取已出具的编号；未出具则返回 None（预览不占用序号）。"""
    from books.models import ReconcileLetter
    letter = ReconcileLetter.objects.filter(
        unit_name=unit, year=year,
        month_start=ms, month_end=me,
    ).first()
    return letter.letter_no if letter else None


def _allocate_letter_no(unit, year, ms, me, username=''):
    """取得或分配对账函编号（公文式，按年份各自连续）。

    同一 (单位, 年份, 起止月) 复用同一编号；否则按该年份的最大 seq +1 分配。
    seq 在「年份」内唯一（Meta.unique_together），并发时以重试循环避免重复/报错。
    """
    from books.models import ReconcileLetter
    existing = ReconcileLetter.objects.filter(
        unit_name=unit, year=year,
        month_start=ms, month_end=me,
    ).first()
    if existing:
        return existing.letter_no
    for _ in range(20):
        with transaction.atomic():
            last = ReconcileLetter.objects.filter(year=year
                                                 ).aggregate(m=Max('seq'))['m']
            seq = (last or 0) + 1
            try:
                letter = ReconcileLetter.objects.create(
                    unit_name=unit, year=year,
                    month_start=ms, month_end=me, seq=seq, issued_by=username,
                )
                return letter.letter_no
            except IntegrityError:
                # 并发冲突（同函或同年同 seq）：重试取最新最大值
                continue
    raise RuntimeError('对账函编号分配失败：并发重试次数过多')


def _build_reconcile(user, unit, year, ms, me):
    from datetime import date
    base = get_visible_queryset(user, MasterRecord.objects.all()).exclude(tag__in=EXCLUDED_TAGS)
    start_period = f"{year}{ms:02d}"
    end_period = f"{year}{me:02d}"
    period_start_date = date(year, ms, 1)

    ob = OpeningBalance.objects.filter(unit_name=unit)
    ob_before = ob.filter(occur_date__lt=period_start_date)
    opening = (float(ob_before.aggregate(s=Sum('debit'))['s'] or 0)
               - float(ob_before.aggregate(s=Sum('credit'))['s'] or 0))
    mr_before = base.filter(unit_name=unit, period__lt=start_period)
    opening += (float(mr_before.aggregate(s=Sum('debit'))['s'] or 0)
                - float(mr_before.aggregate(s=Sum('credit'))['s'] or 0))

    q = base.filter(unit_name=unit, period__gte=start_period, period__lte=end_period
                    ).order_by('occur_date', 'id')
    rows = []
    balance = opening
    tot_d = tot_c = 0.0
    for r in q:
        balance += float(r.debit) - float(r.credit)
        tot_d += float(r.debit)
        tot_c += float(r.credit)
        rows.append({
            'date': r.occur_date,
            'biz_type': r.biz_type,
            'category': r.category,
            'debit': r.debit,
            'credit': r.credit,
            'balance': round(balance, 2),
            'handler': r.handler,
            'remark': r.remark,
            'invoice_no': r.invoice_number,
            'source_type': r.get_source_type_display(),
            'id': r.id,
        })
    ending = opening + tot_d - tot_c
    return {
        'unit': unit, 'year': year, 'ms': ms, 'me': me,
        'start_period': start_period, 'end_period': end_period,
        'opening': round(opening, 2), 'rows': rows,
        'tot_d': round(tot_d, 2), 'tot_c': round(tot_c, 2),
        'ending': round(ending, 2),
    }


@login_required
def reconcile(request):
    role = get_user_role(request.user)
    unit_choices = sorted(set(get_visible_queryset(request.user, MasterRecord.objects.all())
                              .values_list('unit_name', flat=True)))
    unit = request.GET.get('unit_name', '').strip()
    year = request.GET.get('year', '').strip()
    ms = request.GET.get('month_start', '1').strip()
    me = request.GET.get('month_end', '12').strip()

    data = None
    if unit:
        try:
            y = int(year)
            m1 = int(ms) if ms else 1
            m2 = int(me) if me else 12
        except (TypeError, ValueError):
            y, m1, m2 = timezone.localdate().year, 1, 12
        # 业务员只能对自己客户出具
        if role == '业务员' and unit not in visible_unit_names(request.user):
            return HttpResponse('无权限', status=403)
        data = _build_reconcile(request.user, unit, y, m1, m2)
        # 预览只读已出具的编号，不在此处占用序号（由「出具编号」按钮分配）
        data['letter_no'] = _get_letter_no(unit, y, m1, m2)

    sort = None
    if data:
        # 只改显示顺序：每行余额仍是按发生日累计到该行的值
        data['rows'], sort = sorting.sort_rows(data['rows'], request.GET, fields=RECONCILE_SORT,
                                               default_key='date', default_dir=sorting.ASC)
    ctx = {
        'role': role, 'unit_choices': unit_choices,
        'filters': {'unit_name': unit, 'year': year, 'month_start': ms, 'month_end': me},
        'data': data,
        'sort': sort,
    }
    return render(request, 'books/reconcile.html', ctx)


@login_required
def reconcile_issue(request):
    """出具对账函：分配并持久化编号（仅在用户主动点击「出具编号」时占用序号）。"""
    if request.method != 'POST':
        return redirect('reconcile')
    unit = request.POST.get('unit_name', '').strip()
    if not unit:
        return redirect('reconcile')
    role = get_user_role(request.user)
    if role == '业务员' and unit not in visible_unit_names(request.user):
        return HttpResponse('无权限', status=403)
    year = request.POST.get('year', '').strip()
    ms = request.POST.get('month_start', '1').strip()
    me = request.POST.get('month_end', '12').strip()
    try:
        y = int(year); m1 = int(ms) if ms else 1; m2 = int(me) if me else 12
    except (TypeError, ValueError):
        y, m1, m2 = timezone.localdate().year, 1, 12
    _allocate_letter_no(unit, y, m1, m2, request.user.username)
    # 分配后重定向回对账函视图，此时已能读到已出具的编号
    q = urlencode({'unit_name': unit, 'year': y, 'month_start': m1, 'month_end': m2})
    return redirect(f"{reverse('reconcile')}?{q}")


@login_required
def reconcile_export(request):
    unit = request.GET.get('unit_name', '').strip()
    if not unit:
        return HttpResponse('请先选择单位', status=400)
    role = get_user_role(request.user)
    if role == '业务员' and unit not in visible_unit_names(request.user):
        return HttpResponse('无权限', status=403)
    year = request.GET.get('year', '').strip()
    ms = request.GET.get('month_start', '1').strip()
    me = request.GET.get('month_end', '12').strip()
    try:
        y = int(year); m1 = int(ms) if ms else 1; m2 = int(me) if me else 12
    except (TypeError, ValueError):
        y, m1, m2 = timezone.localdate().year, 1, 12
    data = _build_reconcile(request.user, unit, y, m1, m2)
    # 导出同样分配编号，使 Excel 与纸质函编号一致（同一函复用，不重复占号）
    letter_no = _allocate_letter_no(unit, y, m1, m2, request.user.username)

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '对账单'
    thin = Side(style='thin', color='BBBBBB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.cell(1, 1, '往来对账函').font = Font(bold=True, size=16)
    ws.cell(2, 1, f'编号：{letter_no}').font = Font(bold=True, size=12)
    ws.cell(3, 1, f'客户名称：{data["unit"]}').font = Font(bold=True, size=12)
    period_label = f'{data["year"]}年{data["ms"]:02d}月 - {data["year"]}年{data["me"]:02d}月'
    ws.cell(4, 1, f'对账期间：{period_label}    出具日期：{timezone.localdate():%Y-%m-%d}')

    headers = ['日期', '业务类型', '类别', '借方金额', '贷方金额', '余额', '经手人', '发票号', '备注', '来源']
    hrow = 6
    for c, h in enumerate(headers, 1):
        cell = ws.cell(hrow, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
        cell.border = border

    r = hrow + 1
    # 期初
    ws.cell(r, 1, '期初余额')
    ws.cell(r, 6, data['opening'])
    for c in range(1, 11):
        ws.cell(r, c).border = border
    r += 1
    for row in data['rows']:
        ws.cell(r, 1, row['date'].strftime('%Y-%m-%d') if row['date'] else '')
        ws.cell(r, 2, row['biz_type'])
        ws.cell(r, 3, row['category'])
        ws.cell(r, 4, float(row['debit']))
        ws.cell(r, 5, float(row['credit']))
        ws.cell(r, 6, row['balance'])
        ws.cell(r, 7, row['handler'])
        ws.cell(r, 8, row['invoice_no'])
        ws.cell(r, 9, row['remark'])
        ws.cell(r, 10, row['source_type'])
        for c in range(1, 11):
            ws.cell(r, c).border = border
        r += 1
    # 合计
    ws.cell(r, 1, '合计')
    ws.cell(r, 4, data['tot_d'])
    ws.cell(r, 5, data['tot_c'])
    ws.cell(r, 6, data['ending'])
    for c in range(1, 11):
        ws.cell(r, c).border = border
    r += 2
    # 确认区
    ws.cell(r, 1, '请贵公司核对无误后加盖公章回传，谢谢合作！').font = Font(bold=True)
    r += 2
    ws.cell(r, 1, '客户名称（盖章）：______________________')
    r += 2
    ws.cell(r, 1, '核对人：________________    日期：________________')

    widths = [14, 10, 10, 14, 14, 14, 12, 16, 24, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{unit}_对账单.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 业务员开票回款明细（开票额 / 回款额 / 未回款额）
# ---------------------------------------------------------------------------
COLLECT_CATEGORIES = ['贷方', '收入', '签收']
# 开票回款统计不含保证金：保证金不是货款收付，已有独立的「保证金管理」页。
# 开票额按 source_type=sales_invoice 取数，保证金本就不会计入；但回款额按
# category 取数会把保证金收款算进来，两侧不对称会让「未回款额」虚减，
# 故开票与回款同时排除 tag=保证金。
PERF_EXCLUDED_TAGS = EXCLUDED_TAGS + ['保证金']


def _perf_for_customer(base, cu):
    recs = base.filter(unit_name=cu)
    # 开票额 = 销项发票（借）净额：仅排除「已作废」；红冲（红字负数）按负数扣减，
    # 与「客户往来明细」借方的净额口径保持一致（不再按票面正数加计）。
    invoice = float(recs.filter(source_type='sales_invoice').exclude(tag__in=PERF_EXCLUDED_TAGS)
                    .aggregate(s=Sum('debit'))['s'] or 0)
    # 回款 = 贷方/收入/签收类（仍由非发票来源产生），排除红冲/作废/保证金
    collect = float(recs.filter(category__in=COLLECT_CATEGORIES).exclude(tag__in=PERF_EXCLUDED_TAGS)
                    .aggregate(s=Sum('credit'))['s'] or 0)
    unpaid = invoice - collect
    return {'invoice': invoice, 'collect': collect, 'unpaid': unpaid}


def _build_sales_perf(user, year, sp_filter, expand, cu_filter=''):
    base = get_visible_queryset(user, MasterRecord.objects.all())
    if year:
        base = base.filter(year=year)
    if cu_filter:
        base = base.filter(unit_name=cu_filter)
    role = get_user_role(user)

    if role == '业务员':
        sps = [getattr(user.profile, 'salesperson_name', '')]
    else:
        sps = sorted(set(CustomerSalesperson.objects.filter(is_current=True)
                         .values_list('salesperson', flat=True)))
        if sp_filter:
            sps = [sp_filter] if sp_filter in sps else []

    keys = ['invoice', 'collect', 'unpaid']
    rows = []
    totals = {k: 0 for k in keys}
    for sp in sps:
        if not sp:
            continue
        customers = CustomerSalesperson.objects.filter(salesperson=sp, is_current=True
                                                        ).values_list('unit_name', flat=True)
        if cu_filter:
            customers = customers.filter(unit_name=cu_filter)
            if not customers:
                continue   # 该业务员名下无匹配客户时跳过空行
        if expand:
            for cu in customers:
                a = _perf_for_customer(base, cu)
                rows.append({'salesperson': sp, 'unit': cu, **a})
                for k in keys:
                    totals[k] += a[k]
        else:
            agg = {k: 0 for k in keys}
            for cu in customers:
                a = _perf_for_customer(base, cu)
                for k in keys:
                    agg[k] += a[k]
            agg['salesperson'] = sp
            rows.append(agg)
            for k in keys:
                totals[k] += agg[k]
    rows.sort(key=lambda x: (-x['invoice'], x['salesperson']))
    return rows, totals


@login_required
def sales_perf(request):
    year = request.GET.get('year', '').strip()
    sp_filter = request.GET.get('salesperson', '').strip()
    cu_filter = request.GET.get('unit_name', '').strip()
    expand = request.GET.get('expand', '') in ('1', 'on', 'true')
    y = int(year) if year.isdigit() else None

    rows, totals = _build_sales_perf(request.user, y, sp_filter, expand, cu_filter)
    rows, sort = sorting.sort_rows(rows, request.GET, fields=SALES_PERF_SORT,
                                   default_key='invoice', default_dir=sorting.DESC)
    sp_choices = sorted(set(CustomerSalesperson.objects.filter(is_current=True)
                            .values_list('salesperson', flat=True)))
    unit_choices = sorted(set(get_visible_queryset(request.user, MasterRecord.objects.all())
                              .values_list('unit_name', flat=True)))
    ctx = {
        'rows': rows, 'totals': totals,
        'year': year, 'sp_filter': sp_filter, 'cu_filter': cu_filter, 'expand': expand,
        'sp_choices': sp_choices, 'unit_choices': unit_choices, 'role': get_user_role(request.user),
        'years': sorted({yy for yy in MasterRecord.objects.values_list('year', flat=True) if yy}),
        'sort': sort,
    }
    return render(request, 'books/sales_perf.html', ctx)


@login_required
def sales_perf_export(request):
    year = request.GET.get('year', '').strip()
    sp_filter = request.GET.get('salesperson', '').strip()
    cu_filter = request.GET.get('unit_name', '').strip()
    expand = request.GET.get('expand', '') in ('1', 'on', 'true')
    y = int(year) if year.isdigit() else None
    rows, totals = _build_sales_perf(request.user, y, sp_filter, expand, cu_filter)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    CURRENCY_FMT = '¥#,##0.00'   # 货币格式：千分位 + 2 位小数

    wb = Workbook()
    ws = wb.active
    ws.title = '业务员开票回款'
    title = f'业务员开票回款明细' + (f'（{year}年）' if year else '') + (f'（{cu_filter}）' if cu_filter else '')
    ws.cell(1, 1, title).font = Font(bold=True, size=14)
    headers = ['业务员'] + (['客户'] if expand else []) + \
        ['开票额', '回款额', '未回款额']
    hrow = 3
    for c, h in enumerate(headers, 1):
        cell = ws.cell(hrow, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = hrow + 1
    for row in rows:
        col = 1
        ws.cell(r, col, row['salesperson']); col += 1
        if expand:
            ws.cell(r, col, row['unit']); col += 1
        c = ws.cell(r, col, round(row['invoice'], 2)); c.number_format = CURRENCY_FMT; col += 1
        c = ws.cell(r, col, round(row['collect'], 2)); c.number_format = CURRENCY_FMT; col += 1
        c = ws.cell(r, col, round(row['unpaid'], 2)); c.number_format = CURRENCY_FMT
        r += 1
    # 合计
    col = 1
    ws.cell(r, col, '合计')
    if expand:
        col += 1
    c = ws.cell(r, col + 1, round(totals['invoice'], 2)); c.number_format = CURRENCY_FMT
    c = ws.cell(r, col + 2, round(totals['collect'], 2)); c.number_format = CURRENCY_FMT
    c = ws.cell(r, col + 3, round(totals['unpaid'], 2)); c.number_format = CURRENCY_FMT
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="业务员开票回款明细.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 保证金管理（主表中 tag=保证金 的记录）
# ---------------------------------------------------------------------------
@login_required
def deposit(request):
    unit = request.GET.get('unit_name', '').strip()
    year = request.GET.get('year', '').strip()
    biz = request.GET.get('biz_type', '').strip()

    all_base = get_visible_queryset(request.user, MasterRecord.objects.all()).filter(tag='保证金')
    unit_choices = sorted(set(all_base.values_list('unit_name', flat=True)))
    base = all_base
    if unit:
        base = base.filter(unit_name=unit)
    if year:
        base = base.filter(year=int(year))
    if biz:
        base = base.filter(biz_type=biz)
    # 不设置默认时间范围：date_from 缺省时查全部
    date_from_raw = request.GET.get('date_from', '').strip()
    date_to_raw = request.GET.get('date_to', '').strip()
    df = to_date(date_from_raw)
    dt = to_date(date_to_raw)
    if df:
        base = base.filter(occur_date__gte=df)
    if dt:
        base = base.filter(occur_date__lte=dt)

    summary = base.values('unit_name').annotate(
        recv=Sum('debit'), ret=Sum('credit'), cnt=Count('id')
    ).order_by('unit_name')
    for s in summary:
        s['recv'] = float(s['recv'] or 0)
        s['ret'] = float(s['ret'] or 0)
        s['balance'] = round(s['recv'] - s['ret'], 2)
        s['recv'] = round(s['recv'], 2)
        s['ret'] = round(s['ret'], 2)

    summary, sum_sort = sorting.sort_rows(
        list(summary), request.GET, fields=DEPOSIT_SUM_SORT, param='sum_sort',
        default_key='unit_name', default_dir=sorting.ASC)

    detail = base.order_by('unit_name', 'occur_date', 'id')
    details = []
    t_recv = t_ret = 0.0
    for r in detail:
        details.append({
            'date': r.occur_date, 'unit': r.unit_name, 'biz_type': r.biz_type,
            'debit': r.debit, 'credit': r.credit, 'handler': r.handler,
            'remark': r.remark, 'invoice_no': r.invoice_number,
            'source_type': r.get_source_type_display(),
            'id': r.id,
        })
        t_recv += float(r.debit)
        t_ret += float(r.credit)
    details, det_sort = sorting.sort_rows(details, request.GET, fields=DEPOSIT_DET_SORT,
                                          param='det_sort')

    ctx = {
        'unit_choices': unit_choices,
        'years': sorted({y for y in all_base.values_list('year', flat=True) if y}),
        'biz_types': BIZ_TYPE_CHOICES,
        'filters': {
            'unit_name': unit, 'year': year, 'biz_type': biz,
            'date_from': df.strftime('%Y-%m-%d') if df else '',
            'date_to': dt.strftime('%Y-%m-%d') if dt else '',
        },
        'summary': summary, 'details': details,
        'totals': {'recv': round(t_recv, 2), 'ret': round(t_ret, 2),
                   'balance': round(t_recv - t_ret, 2)},
        'role': get_user_role(request.user),
        'sum_sort': sum_sort, 'det_sort': det_sort,
    }
    return render(request, 'books/deposit.html', ctx)


@login_required
def deposit_export(request):
    unit = request.GET.get('unit_name', '').strip()
    year = request.GET.get('year', '').strip()
    biz = request.GET.get('biz_type', '').strip()
    role = get_user_role(request.user)
    if role == '业务员' and unit and unit not in visible_unit_names(request.user):
        return HttpResponse('无权限', status=403)

    base = get_visible_queryset(request.user, MasterRecord.objects.all()).filter(tag='保证金')
    if unit:
        base = base.filter(unit_name=unit)
    if year:
        base = base.filter(year=int(year))
    if biz:
        base = base.filter(biz_type=biz)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '保证金汇总'
    ws.cell(1, 1, '保证金汇总').font = Font(bold=True, size=14)
    headers = ['单位名称', '收保证金', '退保证金', '结余', '笔数']
    for c, h in enumerate(headers, 1):
        cell = ws.cell(3, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = 4
    tr = {'recv': 0, 'ret': 0, 'cnt': 0}
    for s in base.values('unit_name').annotate(recv=Sum('debit'), ret=Sum('credit'), cnt=Count('id')).order_by('unit_name'):
        recv = float(s['recv'] or 0)
        ret = float(s['ret'] or 0)
        ws.cell(r, 1, s['unit_name'])
        ws.cell(r, 2, round(recv, 2))
        ws.cell(r, 3, round(ret, 2))
        ws.cell(r, 4, round(recv - ret, 2))
        ws.cell(r, 5, s['cnt'])
        tr['recv'] += recv; tr['ret'] += ret; tr['cnt'] += s['cnt']
        r += 1
    ws.cell(r, 1, '合计')
    ws.cell(r, 2, round(tr['recv'], 2))
    ws.cell(r, 3, round(tr['ret'], 2))
    ws.cell(r, 4, round(tr['recv'] - tr['ret'], 2))
    ws.cell(r, 5, tr['cnt'])
    for c in range(1, 6):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 18

    # 明细 sheet
    wd = wb.create_sheet('保证金明细')
    dheaders = ['日期', '单位名称', '业务类型', '借方', '贷方', '经手人', '发票号', '备注', '来源']
    for c, h in enumerate(dheaders, 1):
        cell = wd.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
    rr = 2
    for m in base.order_by('unit_name', 'occur_date', 'id'):
        wd.cell(rr, 1, m.occur_date.strftime('%Y-%m-%d') if m.occur_date else '')
        wd.cell(rr, 2, m.unit_name)
        wd.cell(rr, 3, m.biz_type)
        wd.cell(rr, 4, float(m.debit))
        wd.cell(rr, 5, float(m.credit))
        wd.cell(rr, 6, m.handler)
        wd.cell(rr, 7, m.invoice_number)
        wd.cell(rr, 8, m.remark)
        wd.cell(rr, 9, m.get_source_type_display())
        rr += 1
    for c in range(1, 10):
        wd.column_dimensions[wd.cell(1, c).column_letter].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="保证金管理.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 票据检索（承兑汇票 + 迪链，同属 acceptance 来源，用 tag=迪链 区分）
# ---------------------------------------------------------------------------
# 方向口径：签收（收款）记贷方，申请/转单（付款）记借方。
ACCEPTANCE_KINDS = [('承兑', '承兑汇票'), ('迪链', '迪链')]
ACCEPTANCE_DIRECTIONS = [('签收', '签收（收款）'), ('申请', '申请（付款）'), ('转单', '转单（付款）')]
# 承兑导入原始列（穿透明细时按此顺序展示「导入的23列信息」）
ACCEPTANCE_RAW_FIELDS = [
    '票据包号码', '子票区间', '机构名称', '账号', '票据种类', '交易金额（元）', '票据到期日',
    '票据出票日期', '出票人账号', '出票人全称', '出票人开户行行号', '承兑行/人全称',
    '承兑行/人账号', '承兑行/人开户行号', '收款人账号', '收款人全称', '收款人开户行行号',
    '交易对手名称', '交易日期', '操作类型', '业务类型', '处理结果', '备注',
    '操作', '是否自动签收', '票据历史状态',
]


def _acceptance_queryset(user, GET):
    """按筛选条件返回票据记录集，附带已归一的筛选值。"""
    base = get_visible_queryset(user, MasterRecord.objects.all()).filter(source_type='acceptance')

    unit = GET.get('unit_name', '').strip()
    package_no = GET.get('package_no', '').strip()
    year = GET.get('year', '').strip()
    kind = GET.get('kind', '').strip()
    direction = GET.get('direction', '').strip()
    df = to_date(GET.get('date_from', '').strip())
    dt = to_date(GET.get('date_to', '').strip())

    qs = base
    if unit:
        qs = qs.filter(unit_name__icontains=unit)
    if package_no:
        qs = qs.filter(package_no__icontains=package_no)
    if year:
        try:
            qs = qs.filter(year=int(year))
        except ValueError:
            pass
    if kind == '迪链':
        qs = qs.filter(tag='迪链')
    elif kind == '承兑':
        qs = qs.exclude(tag='迪链')
    if direction:
        qs = qs.filter(category=direction)
    if df:
        qs = qs.filter(occur_date__gte=df)
    if dt:
        qs = qs.filter(occur_date__lte=dt)

    filters = {
        'unit_name': unit, 'package_no': package_no, 'year': year, 'kind': kind,
        'direction': direction,
        'date_from': df.strftime('%Y-%m-%d') if df else '',
        'date_to': dt.strftime('%Y-%m-%d') if dt else '',
    }
    return base, qs, filters


@login_required
def acceptance(request):
    base, qs, filters = _acceptance_queryset(request.user, request.GET)

    # 按单位汇总
    summary = list(qs.values('unit_name').annotate(
        recv=Sum('credit'), pay=Sum('debit'), cnt=Count('id')).order_by('unit_name'))
    t_recv = t_pay = 0.0
    for s in summary:
        s['recv'] = round(float(s['recv'] or 0), 2)
        s['pay'] = round(float(s['pay'] or 0), 2)
        s['net'] = round(s['recv'] - s['pay'], 2)
        t_recv += s['recv']
        t_pay += s['pay']

    # 按票据包号码汇总（承兑汇票有包号；迪链无包号，不在此汇总）
    pkg_summary = list(qs.exclude(package_no='').values('package_no').annotate(
        recv=Sum('credit'), pay=Sum('debit'), cnt=Count('id'),
        unit=Min('unit_name')).order_by('package_no'))
    p_recv = p_pay = 0.0
    for s in pkg_summary:
        s['recv'] = round(float(s['recv'] or 0), 2)
        s['pay'] = round(float(s['pay'] or 0), 2)
        s['net'] = round(s['recv'] - s['pay'], 2)
        p_recv += s['recv']
        p_pay += s['pay']

    summary, sum_sort = sorting.sort_rows(
        summary, request.GET, fields=ACCEPTANCE_SUM_SORT, param='sum_sort',
        default_key='unit_name', default_dir=sorting.ASC)
    pkg_summary, pkg_sort = sorting.sort_rows(
        pkg_summary, request.GET, fields=ACCEPTANCE_PKG_SORT, param='pkg_sort',
        default_key='package_no', default_dir=sorting.ASC)

    detail_qs, det_sort = sorting.sort_queryset(
        qs, request.GET, fields=ACCEPTANCE_DET_SORT, param='det_sort',
        default=('-occur_date', 'unit_name', 'id'),
        default_key='date', default_dir=sorting.DESC)
    paginator = Paginator(detail_qs, 50)
    try:
        page_obj = paginator.get_page(int(request.GET.get('page', 1)))
    except ValueError:
        page_obj = paginator.get_page(1)

    ctx = {
        'page_obj': page_obj, 'count': qs.count(),
        'summary': summary,
        'totals': {'recv': round(t_recv, 2), 'pay': round(t_pay, 2),
                   'net': round(t_recv - t_pay, 2)},
        'pkg_summary': pkg_summary,
        'pkg_totals': {'recv': round(p_recv, 2), 'pay': round(p_pay, 2),
                        'net': round(p_recv - p_pay, 2)},
        'filters': filters,
        'unit_choices': sorted(set(base.values_list('unit_name', flat=True))),
        'package_choices': sorted(set(
            base.exclude(package_no='').values_list('package_no', flat=True))),
        'years': sorted({y for y in base.values_list('year', flat=True) if y}, reverse=True),
        'kinds': ACCEPTANCE_KINDS,
        'directions': ACCEPTANCE_DIRECTIONS,
        'role': get_user_role(request.user),
        'querystring': urlencode({k: v for k, v in request.GET.items() if k != 'page'}),
        'querystring_no_unit': urlencode(
            {k: v for k, v in request.GET.items() if k not in ('page', 'unit_name')}),
        'querystring_no_pkg': urlencode(
            {k: v for k, v in request.GET.items() if k not in ('page', 'package_no')}),
        'sum_sort': sum_sort, 'pkg_sort': pkg_sort, 'det_sort': det_sort,
    }
    return render(request, 'books/acceptance.html', ctx)


@login_required
def acceptance_detail(request):
    """按单位或票据包号码穿透查看票据明细（点击单位名称/票据包号码进入）。

    明细展示导入时的原始列（约23列），即「穿透票据包号码查询票据明细」。
    """
    _, qs, filters = _acceptance_queryset(request.user, request.GET)
    unit = filters['unit_name']
    package_no = filters['package_no']

    if package_no:
        mode = 'pkg'
        title = package_no
        detail_qs = qs.filter(package_no=package_no)
    elif unit:
        mode = 'unit'
        title = unit
        detail_qs = qs.filter(unit_name=unit)
    else:
        return redirect('{}?{}'.format(reverse('acceptance'), request.GET.urlencode()))

    agg = detail_qs.aggregate(recv=Sum('credit'), pay=Sum('debit'), cnt=Count('id'))
    recv = round(float(agg['recv'] or 0), 2)
    pay = round(float(agg['pay'] or 0), 2)
    unit_name = detail_qs.values_list('unit_name', flat=True).first() or ''
    unit_summary = {
        'title': title, 'unit_name': unit_name, 'recv': recv, 'pay': pay,
        'net': round(recv - pay, 2), 'cnt': agg['cnt'],
    }

    detail_qs, sort = sorting.sort_queryset(
        detail_qs, request.GET, fields=ACCEPTANCE_RAW_SORT,
        default=('-occur_date', 'id'), default_key='交易日期', default_dir=sorting.DESC)
    paginator = Paginator(detail_qs, 50)
    try:
        page_obj = paginator.get_page(int(request.GET.get('page', 1)))
    except ValueError:
        page_obj = paginator.get_page(1)

    # 原始导入列（穿透查看导入的23列信息）。列集合取本页记录里出现的全部字段，
    # 避免换排序后首条记录变化导致列数变化。
    raw_columns, raw_rows, raw_data_list = [], [], []
    for rec in page_obj:
        data = (rec.raw_row.data if rec.raw_row else None) or {}
        raw_data_list.append(data)
        for c in ACCEPTANCE_RAW_FIELDS:
            if c in data and c not in raw_columns:
                raw_columns.append(c)
    for data in raw_data_list:
        raw_rows.append([data.get(c, '') for c in raw_columns])

    ctx = {
        'mode': mode,
        'title': title,
        'unit_name': unit_name,
        'unit_summary': unit_summary,
        'page_obj': page_obj,
        'count': detail_qs.count(),
        'filters': filters,
        'raw_columns': raw_columns,
        'raw_rows': raw_rows,
        'raw_sortable': list(ACCEPTANCE_RAW_SORT),
        'sort': sort,
        'querystring': urlencode({k: v for k, v in request.GET.items() if k != 'page'}),
        'back_query': request.GET.urlencode(),
        'role': get_user_role(request.user),
    }
    return render(request, 'books/acceptance_detail.html', ctx)


@login_required
def acceptance_export(request):
    _, qs, filters = _acceptance_queryset(request.user, request.GET)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    def _head(ws, row, headers):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row, c, h)
            cell.font = Font(bold=True)
            cell.fill = PatternFill('solid', fgColor='DDEBF7')
            cell.alignment = Alignment(horizontal='center')

    wb = Workbook()
    ws = wb.active
    ws.title = '票据汇总'
    ws.cell(1, 1, '承兑/迪链票据汇总').font = Font(bold=True, size=14)
    _head(ws, 3, ['单位名称', '签收(收款)', '申请/转单(付款)', '净额', '笔数'])
    r = 4
    tr = {'recv': 0.0, 'pay': 0.0, 'cnt': 0}
    for s in qs.values('unit_name').annotate(
            recv=Sum('credit'), pay=Sum('debit'), cnt=Count('id')).order_by('unit_name'):
        recv = float(s['recv'] or 0)
        pay = float(s['pay'] or 0)
        ws.cell(r, 1, s['unit_name'])
        ws.cell(r, 2, round(recv, 2))
        ws.cell(r, 3, round(pay, 2))
        ws.cell(r, 4, round(recv - pay, 2))
        ws.cell(r, 5, s['cnt'])
        tr['recv'] += recv; tr['pay'] += pay; tr['cnt'] += s['cnt']
        r += 1
    ws.cell(r, 1, '合计')
    ws.cell(r, 2, round(tr['recv'], 2))
    ws.cell(r, 3, round(tr['pay'], 2))
    ws.cell(r, 4, round(tr['recv'] - tr['pay'], 2))
    ws.cell(r, 5, tr['cnt'])
    for c in range(1, 6):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 20

    wd = wb.create_sheet('票据明细')
    _head(wd, 1, ['日期', '单位名称', '类型', '方向', '借方(付款)', '贷方(收款)', '经手人', '备注'])
    rr = 2
    for m in qs.order_by('-occur_date', 'unit_name', 'id'):
        wd.cell(rr, 1, m.occur_date.strftime('%Y-%m-%d') if m.occur_date else '')
        wd.cell(rr, 2, m.unit_name)
        wd.cell(rr, 3, '迪链' if m.tag == '迪链' else '承兑')
        wd.cell(rr, 4, m.category)
        wd.cell(rr, 5, float(m.debit))
        wd.cell(rr, 6, float(m.credit))
        wd.cell(rr, 7, m.handler)
        wd.cell(rr, 8, m.remark)
        rr += 1
    for c in range(1, 9):
        wd.column_dimensions[wd.cell(1, c).column_letter].width = 18

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="承兑迪链票据.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 承兑收付款统计报表（按期间趋势 + 期末库存余额）
# ---------------------------------------------------------------------------
def _acceptance_report_rows(user, GET):
    """按期间(月/年)聚合承兑收付款，并计算期末滚动库存余额。

    期末余额 = 期初库存余额 + 各期净额(收款−付款)的累计；无期初时从首期起算。
    若存在期初余额，则仅统计期初日期之后的收付款（期初已含此前活动）。
    """
    base, qs, filters = _acceptance_queryset(user, GET)
    opening = AcceptanceOpeningBalance.objects.first()
    start_balance = 0.0
    if opening:
        start_balance = round(float(opening.amount or 0), 2)
        if opening.as_of_date:
            qs = qs.filter(occur_date__gte=opening.as_of_date)
    dim = GET.get('dim', 'month').strip()
    agg = (qs.values('year').annotate(recv=Sum('credit'), pay=Sum('debit')).order_by('year')
           if dim == 'year'
           else qs.values('period').annotate(recv=Sum('credit'), pay=Sum('debit')).order_by('period'))
    rows, balance = [], start_balance
    for a in agg:
        key = str(a['year'] if dim == 'year' else a['period'] or '')
        recv = round(float(a['recv'] or 0), 2)
        pay = round(float(a['pay'] or 0), 2)
        net = round(recv - pay, 2)
        balance = round(balance + net, 2)
        if dim == 'year':
            label = key
        else:
            label = '{}-{}'.format(key[:4], key[4:6]) if len(key) == 6 else key
        rows.append({'period': key, 'label': label, 'recv': recv,
                     'pay': pay, 'net': net, 'balance': balance})
    t_recv = round(sum(r['recv'] for r in rows), 2)
    t_pay = round(sum(r['pay'] for r in rows), 2)
    totals = {'recv': t_recv, 'pay': t_pay, 'net': round(t_recv - t_pay, 2),
              'balance': balance}
    return rows, totals, filters, dim, base, opening


@login_required
def acceptance_report(request):
    """承兑收付款统计报表：按期间展示收款/付款/期末库存余额趋势。"""
    rows, totals, filters, dim, base, opening = _acceptance_report_rows(request.user, request.GET)
    # 期末库存余额是按期间滚动累计的，不参与排序
    rows, sort = sorting.sort_rows(rows, request.GET, fields=ACCEPTANCE_REPORT_SORT,
                                   default_key='period', default_dir=sorting.ASC)
    ctx = {
        'rows': rows, 'totals': totals, 'filters': filters, 'dim': dim,
        'opening': opening,
        'sort': sort,
        'years': sorted({y for y in base.values_list('year', flat=True) if y}, reverse=True),
        'kinds': ACCEPTANCE_KINDS,
        'role': get_user_role(request.user),
        'querystring': urlencode({k: v for k, v in request.GET.items() if k != 'page'}),
    }
    return render(request, 'books/acceptance_report.html', ctx)


@login_required
def acceptance_report_set_opening(request):
    """录入/更新承兑汇票期初库存余额（单一总额）。"""
    from decimal import Decimal
    if request.method != 'POST':
        return redirect('acceptance_report')
    as_of = to_date(request.POST.get('as_of_date', '').strip())
    amt = request.POST.get('amount', '').strip()
    remark = request.POST.get('remark', '').strip()
    if as_of is None:
        return redirect('{}?{}'.format(reverse('acceptance_report'), request.GET.urlencode()))
    try:
        amount = Decimal(amt) if amt not in ('', None) else Decimal('0')
    except Exception:
        amount = Decimal('0')
    ob = AcceptanceOpeningBalance.objects.first()
    if ob:
        ob.as_of_date = as_of
        ob.amount = amount
        ob.remark = remark
        ob.save()
        log_operation(request.user, '更新承兑期初', target_type='AcceptanceOpeningBalance',
                      target_id=str(ob.pk), detail=f'as_of={as_of}, amount={amount}', request=request)
    else:
        ob = AcceptanceOpeningBalance.objects.create(as_of_date=as_of, amount=amount, remark=remark)
        log_operation(request.user, '设置承兑期初', target_type='AcceptanceOpeningBalance',
                      target_id=str(ob.pk), detail=f'as_of={as_of}, amount={amount}', request=request)
    return redirect('{}?{}'.format(reverse('acceptance_report'), request.GET.urlencode()))


@login_required
def acceptance_report_export(request):
    """导出承兑收付款统计报表为 Excel。"""
    rows, totals, filters, dim, _, _ = _acceptance_report_rows(request.user, request.GET)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '收付款统计报表'
    ws.cell(1, 1, '承兑收付款统计报表（按{}）'.format('年' if dim == 'year' else '月')).font = Font(bold=True, size=14)
    headers = ['期间', '收款', '付款', '当期净额', '期末库存余额']
    for c, h in enumerate(headers, 1):
        cell = ws.cell(3, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = 4
    for row in rows:
        ws.cell(r, 1, row['label'])
        ws.cell(r, 2, row['recv'])
        ws.cell(r, 3, row['pay'])
        ws.cell(r, 4, row['net'])
        ws.cell(r, 5, row['balance'])
        r += 1
    ws.cell(r, 1, '合计')
    ws.cell(r, 2, totals['recv'])
    ws.cell(r, 3, totals['pay'])
    ws.cell(r, 4, totals['net'])
    ws.cell(r, 5, totals['balance'])
    for c in range(1, 6):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 18

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="承兑收付款统计报表.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 综合查询 / 全局检索（跨所有来源检索主表）
# ---------------------------------------------------------------------------
def _search_queryset(user, GET):
    from decimal import Decimal as _D
    base = get_visible_queryset(user, MasterRecord.objects.all())
    keyword = GET.get('q', '').strip()
    unit = GET.get('unit_name', '').strip()
    biz = GET.get('biz_type', '').strip()
    src = GET.get('source_type', '').strip()
    tag = GET.get('tag', '').strip()
    df = GET.get('date_from', '').strip()
    dt = GET.get('date_to', '').strip()
    amin = GET.get('amount_min', '').strip()
    amax = GET.get('amount_max', '').strip()

    q = base
    if keyword:
        q = q.filter(Q(unit_name__icontains=keyword) | Q(remark__icontains=keyword)
                     | Q(handler__icontains=keyword) | Q(invoice_number__icontains=keyword))
    if unit:
        q = q.filter(unit_name=unit)
    if biz:
        q = q.filter(biz_type=biz)
    if src:
        q = q.filter(source_type=src)
    if tag:
        q = q.filter(tag=tag)
    # 不设置默认时间范围：date_from 缺省时查全部
    d1 = to_date(df)
    if d1:
        q = q.filter(occur_date__gte=d1)
    d2 = to_date(dt)
    if d2:
        q = q.filter(occur_date__lte=d2)
    if amin:
        try:
            v = _D(amin)
            q = q.filter(Q(debit__gte=v) | Q(credit__gte=v))
        except Exception:
            pass
    if amax:
        try:
            v = _D(amax)
            q = q.filter(Q(debit__lte=v) | Q(credit__lte=v))
        except Exception:
            pass
    return q.order_by('-occur_date', '-id')


# ---------------------------------------------------------------------------
# 发票检索 / 穿透明细查询
# ---------------------------------------------------------------------------
INVOICE_SOURCES = ['sales_invoice', 'purchase_invoice']


def _invoice_search_qs(user, q='', unit='', invoice_no='', status='', year='',
                        goods_name='', spec='', all_sources=False):
    """发票检索查询集构造（列表与导出共用，保证一致）。"""
    base = get_visible_queryset(user, MasterRecord.objects.all())
    qs = base.filter(source_type__in=INVOICE_SOURCES) if not all_sources else base
    if q:
        qs = qs.filter(Q(unit_name__icontains=q) | Q(remark__icontains=q)
                       | Q(handler__icontains=q) | Q(invoice_number__icontains=q)
                       | Q(category__icontains=q))
    if unit:
        qs = qs.filter(unit_name=unit)
    if invoice_no:
        qs = qs.filter(invoice_number__icontains=invoice_no)
    if status:
        qs = qs.filter(category=status)
    if year:
        qs = qs.filter(year=int(year))
    if goods_name:
        qs = qs.filter(line_items__goods_name__icontains=goods_name)
    if spec:
        qs = qs.filter(line_items__spec__icontains=spec)
    if goods_name or spec:
        qs = qs.distinct()
    return qs


def _matched_invoice_lines(pks, goods_name, spec, tax_included):
    """返回 {master_record_id: [InvoiceLineItem,...]}，按货物名/规格匹配，按需算含税单价。"""
    if not (goods_name or spec) or not pks:
        return {}
    cond = Q()
    if goods_name:
        cond |= Q(goods_name__icontains=goods_name)
    if spec:
        cond |= Q(spec__icontains=spec)
    matched = {}
    for li in InvoiceLineItem.objects.filter(cond, master_record_id__in=pks):
        if tax_included:
            qty = li.quantity or 0
            li.tax_included_price = ((li.amount or 0) + (li.tax or 0)) / qty if qty else None
        matched.setdefault(li.master_record_id, []).append(li)
    return matched


@login_required
def invoice_search(request):
    role = get_user_role(request.user)
    all_sources = request.GET.get('all_sources') == '1'
    q = request.GET.get('q', '').strip()
    unit = request.GET.get('unit_name', '').strip()
    invoice_no = request.GET.get('invoice_no', '').strip()
    status = request.GET.get('status', '').strip()
    year = request.GET.get('year', '').strip()
    goods_name = request.GET.get('goods_name', '').strip()
    spec = request.GET.get('spec', '').strip()
    tax_included = request.GET.get('tax_included') == '1'

    qs = _invoice_search_qs(request.user, q, unit, invoice_no, status, year,
                            goods_name, spec, all_sources)
    qs, sort = sorting.sort_queryset(qs, request.GET, fields=INVOICE_SEARCH_SORT,
                                     default=('-occur_date', '-id'),
                                     default_key='date', default_dir=sorting.DESC)
    paginator = Paginator(qs, 50)
    try:
        page_obj = paginator.get_page(int(request.GET.get('page', 1)))
    except Exception:
        page_obj = paginator.get_page(1)

    show_matched = bool(goods_name or spec)
    matched = _matched_invoice_lines([r.pk for r in page_obj], goods_name, spec, tax_included)
    for r in page_obj:
        r.matched_lines = matched.get(r.pk, [])

    unit_choices = sorted(set(get_visible_queryset(request.user, MasterRecord.objects.all())
                              .filter(source_type__in=INVOICE_SOURCES)
                              .values_list('unit_name', flat=True)))
    ctx = {
        'page_obj': page_obj, 'count': qs.count(),
        'filters': {k: request.GET.get(k, '') for k in
                    ('q', 'unit_name', 'invoice_no', 'status', 'year',
                     'goods_name', 'spec')},
        'all_sources': all_sources,
        'unit_choices': unit_choices,
        'tags': TAG_CHOICES,
        'role': role,
        'querystring': sorting.page_querystring(request.GET),
        'show_matched': show_matched,
        'sort': sort,
        'show_tax_included': tax_included,
    }
    return render(request, 'books/invoice_search.html', ctx)


@login_required
def invoice_search_export(request):
    """导出发票检索结果（Excel），命中物料行含数量/单价(不含税)，勾选含税时含单价(含税)。"""
    all_sources = request.GET.get('all_sources') == '1'
    q = request.GET.get('q', '').strip()
    unit = request.GET.get('unit_name', '').strip()
    invoice_no = request.GET.get('invoice_no', '').strip()
    status = request.GET.get('status', '').strip()
    year = request.GET.get('year', '').strip()
    goods_name = request.GET.get('goods_name', '').strip()
    spec = request.GET.get('spec', '').strip()
    tax_included = request.GET.get('tax_included') == '1'

    qs = _invoice_search_qs(request.user, q, unit, invoice_no, status, year,
                            goods_name, spec, all_sources).order_by('-occur_date', '-id')
    records = list(qs)
    matched = _matched_invoice_lines([r.pk for r in records], goods_name, spec, tax_included)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    def fmt_line(li):
        parts = [p for p in (li.goods_name, ('/ ' + li.spec) if li.spec else '') if p]
        s = ' '.join(parts).strip()
        if li.quantity is not None:
            s += ' ×%s%s' % ('%g' % li.quantity, li.unit or '')
        if li.price is not None:
            s += ' 单价(不含税)%s' % cny(li.price)
        if tax_included and getattr(li, 'tax_included_price', None) is not None:
            s += ' 单价(含税)%s' % cny(li.tax_included_price)
        return s

    wb = Workbook()
    ws = wb.active
    ws.title = '发票检索结果'
    headers = ['日期', '单位名称', '业务类型', '发票状态/类别', '借方', '贷方',
               '标记', '发票号', '经手人', '来源', '命中物料(数量·单价不含税·单价含税)']
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    r = 2
    for m in records:
        ws.cell(r, 1, m.occur_date.strftime('%Y-%m-%d') if m.occur_date else '')
        ws.cell(r, 2, m.unit_name)
        ws.cell(r, 3, m.biz_type)
        ws.cell(r, 4, m.category)
        ws.cell(r, 5, float(m.debit))
        ws.cell(r, 6, float(m.credit))
        ws.cell(r, 7, m.tag)
        ws.cell(r, 8, m.invoice_number)
        ws.cell(r, 9, m.handler)
        ws.cell(r, 10, m.get_source_type_display())
        lines = matched.get(m.pk, [])
        cell = ws.cell(r, 11, '\n'.join(fmt_line(li) for li in lines) if lines else '')
        cell.alignment = Alignment(wrap_text=True, vertical='top')
        r += 1

    for i, w in enumerate([12, 22, 10, 14, 14, 14, 8, 16, 10, 10, 50], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(),
                        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="发票检索结果.xlsx"'
    return resp


@login_required
def invoice_detail(request, pk):
    """发票穿透明细：展示某张发票的主表归一字段、全部原始表头字段、货物明细行。"""
    rec = get_object_or_404(
        get_visible_queryset(
            request.user,
            MasterRecord.objects.select_related('raw_row', 'source_upload')),
        pk=pk)

    raw_data = {}
    if rec.raw_row:
        rd = rec.raw_row.data
        if isinstance(rd, dict):
            raw_data = rd

    # 用主表发票号 + 原始表头候选列共同构造查询键，确保跨列名差异都能命中明细
    candidates = set()
    if rec.invoice_number:
        candidates.add(rec.invoice_number)
    for c in INVOICE_NO_CANDIDATES:
        v = _norm_invoice_no(raw_data.get(c))
        if v:
            candidates.add(v)

    # 明细行与本次导入同源时直接命中；增量导入常出现「主表在旧 upload、明细在新 upload」，
    # 此时退一步按发票号在同来源类型内查找，避免穿透页看不到货物明细。
    line_items = InvoiceLineItem.objects.filter(
        source_upload=rec.source_upload, invoice_number__in=candidates
    ).order_by('row_index')
    if not line_items.exists() and candidates:
        line_items = InvoiceLineItem.objects.filter(
            source_upload__source_type=rec.source_type, invoice_number__in=candidates
        ).order_by('row_index')
    line_items, sort = sorting.sort_queryset(
        line_items, request.GET, fields=INVOICE_DETAIL_SORT, default=('row_index',))

    # 原始表头字段（跳过空值），保留表内原有顺序，供展示全部发票原始字段
    raw_fields = [(k, _fmt_raw(v)) for k, v in raw_data.items() if not _is_empty(v)]

    line_items_total = sum((li.amount or 0) for li in line_items)
    line_items_tax = sum((li.tax or 0) for li in line_items)

    # 每条明细补充：含税单价 = (金额+税额)/数量；价税合计 = 金额+税额
    for li in line_items:
        amt = li.amount or 0
        tax = li.tax or 0
        li.tax_included_total = amt + tax
        qty = li.quantity
        li.tax_included_price = (li.tax_included_total / qty) if qty else None
    line_items_total_incl = line_items_total + line_items_tax

    ctx = {
        'rec': rec,
        'raw_fields': raw_fields,
        'line_items': line_items,
        'line_count': line_items.count(),
        'line_items_total': line_items_total,
        'line_items_tax': line_items_tax,
        'line_items_total_incl': line_items_total_incl,
        'role': get_user_role(request.user),
        'back_query': request.GET.get('back', ''),
        'sort': sort,
        'title': f'发票明细 - {rec.invoice_number or rec.unit_name}',
    }
    return render(request, 'books/invoice_detail.html', ctx)


def _is_empty(v):
    return v is None or v == '' or v == {}


def _fmt_raw(v):
    """原始字段值格式化：None/空串 -> ''，其余原样（已为 JSON 原生类型）。"""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


@login_required
def global_search(request):
    from django.core.paginator import Paginator
    q = _search_queryset(request.user, request.GET)
    q, sort = sorting.sort_queryset(q, request.GET, fields=GLOBAL_SEARCH_SORT,
                                    default=('-occur_date', '-id'),
                                    default_key='date', default_dir=sorting.DESC)
    paginator = Paginator(q, 50)
    try:
        page_obj = paginator.get_page(int(request.GET.get('page', 1)))
    except Exception:
        page_obj = paginator.get_page(1)

    unit_choices = sorted(set(get_visible_queryset(request.user, MasterRecord.objects.all())
                              .values_list('unit_name', flat=True)))
    ctx = {
        'page_obj': page_obj, 'count': q.count(),
        'filters': {k: request.GET.get(k, '') for k in
                    ('q', 'unit_name', 'biz_type', 'source_type', 'tag', 'date_from', 'date_to', 'amount_min', 'amount_max')},
        'unit_choices': unit_choices,
        'biz_types': BIZ_TYPE_CHOICES, 'source_types': SOURCE_TYPE_CHOICES, 'tags': TAG_CHOICES,
        'role': get_user_role(request.user),
        'edit': request.GET.get('edit') == '1' and get_user_role(request.user) == '管理员',
        'next': request.get_full_path(),
        'querystring': sorting.page_querystring(request.GET),
        'sort': sort,
    }
    return render(request, 'books/global_search.html', ctx)


@login_required
def global_search_export(request):
    q = _search_queryset(request.user, request.GET)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = '综合查询结果'
    headers = ['日期', '单位名称', '业务类型', '类别', '借方', '贷方', '标记', '经手人', '发票号', '备注', '来源']
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
        cell.alignment = Alignment(horizontal='center')
    r = 2
    for m in q.order_by('occur_date', 'id'):
        ws.cell(r, 1, m.occur_date.strftime('%Y-%m-%d') if m.occur_date else '')
        ws.cell(r, 2, m.unit_name)
        ws.cell(r, 3, m.biz_type)
        ws.cell(r, 4, m.category)
        ws.cell(r, 5, float(m.debit))
        ws.cell(r, 6, float(m.credit))
        ws.cell(r, 7, m.tag)
        ws.cell(r, 8, m.handler)
        ws.cell(r, 9, m.invoice_number)
        ws.cell(r, 10, m.remark)
        ws.cell(r, 11, m.get_source_type_display())
        r += 1
    for c in range(1, 12):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="综合查询结果.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 基础数据可视化编辑（名称映射库 / 客户-业务员映射 / 主表标记）
#   管理员可增删改；查询员/业务员只读（业务员仅见自己客户）。
# ---------------------------------------------------------------------------
class NameMappingListView(sorting.SortableListMixin, LoginRequiredMixin, ListView):
    model = NameMapping
    template_name = 'books/data_namemap.html'
    context_object_name = 'items'
    ordering = ['canonical_name', 'raw_name']
    sort_fields = {
        'raw_name': ('raw_name', sorting.ASC),
        'canonical_name': ('canonical_name', sorting.ASC),
        'tax_no': ('tax_no', sorting.ASC),
    }
    sort_default_key = 'canonical_name'
    sort_default_dir = sorting.ASC
    sort_tiebreak = 'id'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['source_type_choices'] = [c for c in SOURCE_TYPE_CHOICES if c[0] != 'fund_handler']
        return ctx


class NameMappingCreateView(AdminRequiredMixin, CreateView):
    model = NameMapping
    form_class = NameMappingForm
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('namemap_list')
    extra_context = {'title': '新增名称映射', 'back': 'namemap_list'}

    def form_valid(self, form):
        resp = super().form_valid(form)
        obj = self.object
        log_operation(self.request.user, '新增名称映射',
                      target_type='NameMapping', target_id=str(obj.pk),
                      detail=f'{obj.raw_name} → {obj.canonical_name}', request=self.request)
        return resp


class NameMappingUpdateView(AdminRequiredMixin, UpdateView):
    model = NameMapping
    form_class = NameMappingForm
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('namemap_list')
    extra_context = {'title': '编辑名称映射', 'back': 'namemap_list'}

    def form_valid(self, form):
        resp = super().form_valid(form)
        obj = self.object
        log_operation(self.request.user, '编辑名称映射',
                      target_type='NameMapping', target_id=str(obj.pk),
                      detail=f'{obj.raw_name} → {obj.canonical_name}', request=self.request)
        return resp


class NameMappingDeleteView(AdminRequiredMixin, DeleteView):
    model = NameMapping
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('namemap_list')
    extra_context = {'back': 'namemap_list'}

    def form_valid(self, form):
        obj = self.object
        log_operation(self.request.user, '删除名称映射',
                      target_type='NameMapping', target_id=str(obj.pk),
                      detail=f'{obj.raw_name} → {obj.canonical_name}', request=self.request)
        return super().form_valid(form)


class CustomerSalespersonListView(sorting.SortableListMixin, LoginRequiredMixin, ListView):
    model = CustomerSalesperson
    template_name = 'books/data_customer.html'
    context_object_name = 'items'
    ordering = ['unit_name', '-is_current']
    sort_fields = {
        'unit_name': ('unit_name', sorting.ASC),
        'salesperson': ('salesperson', sorting.ASC),
        'is_current': ('is_current', sorting.DESC),
        'source': ('source', sorting.ASC),
        'effective_date': ('effective_date', sorting.DESC),
    }
    sort_default_key = 'unit_name'
    sort_default_dir = sorting.ASC
    sort_tiebreak = 'id'

    def get_queryset(self):
        qs = CustomerSalesperson.objects.all()
        units = visible_unit_names(self.request.user)
        if units is not None:
            qs = qs.filter(unit_name__in=units)
        return self.apply_sort(qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        profile = getattr(self.request.user, 'profile', None)
        ctx['my_salesperson'] = getattr(profile, 'salesperson_name', '') if profile else ''
        # 有待处理变更提醒的单位，列表加 ⚠ 角标（仅统计当前可见范围内）
        units = visible_unit_names(self.request.user)
        rem_qs = SalespersonChangeReminder.objects.filter(status='待处理')
        if units is not None:
            rem_qs = rem_qs.filter(unit_name__in=units)
        ctx['reminder_units'] = set(rem_qs.values_list('unit_name', flat=True))
        return ctx


class CustomerSalespersonCreateView(AdminRequiredMixin, CreateView):
    model = CustomerSalesperson
    form_class = CustomerSalespersonForm
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('customer_list')
    extra_context = {'title': '新增客户-业务员映射', 'back': 'customer_list'}

    def form_valid(self, form):
        from books.models import log_operation
        resp = super().form_valid(form)
        obj = self.object
        detail = f'{obj.unit_name} | 业务员 {obj.salesperson} | 账期 {obj.credit_term} | 来源 {obj.source}'
        if obj.manual_override:
            detail += ' | 手动锁定'
        log_operation(self.request.user, '新增客户映射',
                      target_type='客户-业务员映射', target_id=obj.pk,
                      detail=detail, request=self.request)
        return resp


class CustomerSalespersonUpdateView(UpdateView):
    model = CustomerSalesperson
    form_class = CustomerSalespersonForm
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('customer_list')
    extra_context = {'title': '编辑客户-业务员映射', 'back': 'customer_list'}

    def _owns_record(self, obj):
        """业务员仅能编辑自己客户（salesperson 与其绑定姓名一致）的记录。"""
        profile = getattr(self.request.user, 'profile', None)
        name = getattr(profile, 'salesperson_name', '') if profile else ''
        return bool(name) and obj.salesperson == name

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        obj = self.get_object()
        role = get_user_role(request.user)
        if role == ROLE_ADMIN:
            return super().dispatch(request, *args, **kwargs)
        if role == ROLE_SALESPERSON and self._owns_record(obj):
            return super().dispatch(request, *args, **kwargs)
        messages.error(request, '无权限编辑该客户的账期。')
        return redirect('customer_list')

    def get_form_class(self):
        if get_user_role(self.request.user) == ROLE_ADMIN:
            return CustomerSalespersonForm
        return CustomerSalespersonCreditTermForm

    def form_valid(self, form):
        from books.models import log_operation, CustomerSalesperson
        old = CustomerSalesperson.objects.get(pk=self.object.pk)
        resp = super().form_valid(form)
        new = self.object
        changes = []
        if old.credit_term != new.credit_term:
            changes.append(f'账期 {old.credit_term}→{new.credit_term}')
        if old.salesperson != new.salesperson:
            changes.append(f'业务员 {old.salesperson}→{new.salesperson}')
        if old.manual_override != new.manual_override:
            changes.append(f'手动锁定 {old.manual_override}→{new.manual_override}')
        if old.source != new.source:
            changes.append(f'来源 {old.source}→{new.source}')
        if changes:
            log_operation(self.request.user, '编辑客户映射',
                          target_type='客户-业务员映射', target_id=new.pk,
                          detail='；'.join(changes), request=self.request)
        return resp


class CustomerSalespersonDeleteView(AdminRequiredMixin, DeleteView):
    model = CustomerSalesperson
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('customer_list')
    extra_context = {'back': 'customer_list'}

    def form_valid(self, form):
        from books.models import log_operation
        obj = self.object
        detail = f'{obj.unit_name} | 业务员 {obj.salesperson}'
        log_operation(self.request.user, '删除客户映射',
                      target_type='客户-业务员映射', target_id=obj.pk,
                      detail=detail, request=self.request)
        return super().form_valid(form)


@login_required
def record_tag_update(request):
    """查询页「编辑模式」就地更新标注/备注：仅管理员可 POST。
    支持批量（表单提交按 next 重定向）与单条（AJAX，返回 JSON 不刷新页面）。"""
    if request.method != 'POST':
        return redirect(request.META.get('HTTP_REFERER', 'books:search'))
    if get_user_role(request.user) != '管理员':
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'ok': False, 'msg': '仅管理员可编辑'}, status=403)
        return HttpResponse('仅管理员可编辑标注', status=403)
    allowed = {v for v, _ in TAG_CHOICES}
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('global_search')
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    tag_updates, remark_updates = {}, {}
    for key, val in request.POST.items():
        if key.startswith('tag_') and key[4:].isdigit():
            if val in allowed:
                tag_updates[int(key[4:])] = val
        elif key.startswith('remark_') and key[7:].isdigit():
            remark_updates[int(key[7:])] = val
    pks = set(tag_updates) | set(remark_updates)
    changed = 0
    changed_pks = []
    if pks:
        for r in MasterRecord.objects.filter(pk__in=pks):
            fields = []
            if r.pk in tag_updates and r.tag != tag_updates[r.pk]:
                r.tag = tag_updates[r.pk]
                fields.append('tag')
            if r.pk in remark_updates and r.remark != remark_updates[r.pk]:
                r.remark = remark_updates[r.pk]
                fields.append('remark')
            if fields:
                r.save(update_fields=fields)
                changed += 1
                changed_pks.append(str(r.pk))
    if changed:
        # 只记录真正变更的 id：编辑模式会为每一行提交 remark_<pk>，
        # 把全部 pk 拼进 target_id 会超出 varchar(100)（PostgreSQL 会报错 500）。
        log_operation(request.user, '更新标注/备注', target_type='MasterRecord',
                      target_id='、'.join(changed_pks),
                      detail=f'更新 {changed} 条记录的标注或备注', request=request)
    if is_ajax:
        return JsonResponse({'ok': True, 'updated': changed})
    if changed:
        messages.success(request, f'已更新 {changed} 条记录。')
    else:
        messages.info(request, '没有需要更新的内容。')
    return redirect(next_url)



# ===========================================================================
# B2B 认证与授权：手机验证码注册/登录、管理员授权、邀请白名单
# ===========================================================================

def _sms_rate_ok(phone, purpose, seconds=60):
    """同一手机号+用途 60 秒内限发一次。"""
    last = SmsCode.objects.filter(phone=phone, purpose=purpose).order_by('-sent_at').first()
    if last and (timezone.now() - last.sent_at).total_seconds() < seconds:
        return False
    return True


def _new_sms_code(phone, purpose):
    """生成 6 位验证码并落库（5 分钟有效）。"""
    code = f'{_secrets.randbelow(1000000):06d}'
    SmsCode.objects.create(phone=phone, code=code, purpose=purpose,
                           expires_at=timezone.now() + timedelta(minutes=5))
    return code


def _verify_sms_code(phone, code, purpose):
    """校验验证码：未使用、未过期、用途匹配。成功返回 True。"""
    if not code or not phone:
        return False
    row = SmsCode.objects.filter(phone=phone, code=code, purpose=purpose,
                                 is_used=False).order_by('-sent_at').first()
    if not row:
        return False
    if row.expires_at < timezone.now():
        return False
    row.is_used = True
    row.save(update_fields=['is_used'])
    return True


def register(request):
    """邀请制注册：白名单手机号 → 发码 → 校验 → 建账号（默认待授权）。"""
    if request.method != 'POST':
        return render(request, 'books/register.html')
    phone = request.POST.get('phone', '').strip()
    action = request.POST.get('action', 'send')

    if not phone:
        messages.error(request, '请输入手机号。')
        return render(request, 'books/register.html', {'phone': phone})

    if action == 'send':
        if not InvitedPhone.objects.filter(phone=phone).exists():
            messages.error(request, '该手机号未在邀请名单中，请联系管理员。')
            return render(request, 'books/register.html', {'phone': phone})
        User = get_user_model()
        if User.objects.filter(username=phone).exists():
            messages.error(request, '该手机号已注册，请直接登录。')
            return redirect('login')
        if not _sms_rate_ok(phone, '注册'):
            messages.warning(request, '发送过于频繁，请 60 秒后再试。')
            return render(request, 'books/register.html', {'phone': phone})
        code = _new_sms_code(phone, '注册')
        send_sms(phone, code, '注册')
        messages.success(request, f'验证码已发送{"" if getattr(settings, "DJANGO_SMS_BACKEND", "console") != "console" else "（开发模式验证码见控制台）"}。')
        return render(request, 'books/register.html', {'phone': phone, 'step': 'confirm'})

    if action == 'confirm':
        code = request.POST.get('code', '').strip()
        if not InvitedPhone.objects.filter(phone=phone).exists():
            messages.error(request, '该手机号未在邀请名单中。')
            return render(request, 'books/register.html', {'phone': phone, 'step': 'confirm'})
        if not _verify_sms_code(phone, code, '注册'):
            messages.error(request, '验证码错误或已过期，请重新获取。')
            return render(request, 'books/register.html', {'phone': phone, 'step': 'confirm'})
        User = get_user_model()
        if User.objects.filter(username=phone).exists():
            messages.error(request, '该手机号已注册，请直接登录。')
            return redirect('login')
        user = User(username=phone)
        user.set_unusable_password()
        user.save()
        profile = user.profile
        profile.phone = phone
        profile.account_status = '待授权'
        profile.save(update_fields=['phone', 'account_status'])
        log_operation(user, '注册', target_type='User', target_id=username_of(user),
                      detail=f'手机号 {phone} 注册成功，等待授权', request=request)
        auth_login(request, user)
        return redirect('auth_pending')

    return redirect('register')


def username_of(user):
    return getattr(user, 'username', '') or ''


def phone_login(request):
    """验证码登录入口（与密码登录并存）。"""
    if request.method != 'POST':
        return render(request, 'books/phone_login.html')
    phone = request.POST.get('phone', '').strip()
    action = request.POST.get('action', 'send')
    User = get_user_model()
    user = User.objects.filter(username=phone).first()

    if action == 'send':
        if not user:
            messages.error(request, '该手机号尚未注册。')
            return render(request, 'books/phone_login.html', {'phone': phone})
        if not _sms_rate_ok(phone, '登录'):
            messages.warning(request, '发送过于频繁，请 60 秒后再试。')
            return render(request, 'books/phone_login.html', {'phone': phone})
        code = _new_sms_code(phone, '登录')
        send_sms(phone, code, '登录')
        messages.success(request, '验证码已发送（开发模式验证码见控制台）。')
        return render(request, 'books/phone_login.html', {'phone': phone, 'step': 'confirm'})

    if action == 'confirm':
        code = request.POST.get('code', '').strip()
        if not user:
            messages.error(request, '该手机号尚未注册。')
            return render(request, 'books/phone_login.html', {'phone': phone})
        if not _verify_sms_code(phone, code, '登录'):
            messages.error(request, '验证码错误或已过期，请重新获取。')
            return render(request, 'books/phone_login.html', {'phone': phone, 'step': 'confirm'})
        if user.profile.account_status == '已禁用':
            messages.error(request, '账号已被禁用，请联系管理员。')
            return render(request, 'books/phone_login.html', {'phone': phone})
        auth_login(request, user)
        return redirect('home')

    return render(request, 'books/phone_login.html')


@login_required
def auth_pending(request):
    """等待授权页：已登录但未获授权的用户被中间件引导至此。"""
    if request.user.profile.account_status == '已授权':
        return redirect('home')
    return render(request, 'books/auth_pending.html')


@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def admin_authorize(request):
    """管理员/高层：审核待授权用户，分配角色与归属后激活。"""
    User = get_user_model()
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        action = request.POST.get('action', 'approve')
        try:
            target = User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError):
            messages.error(request, '用户不存在。')
            return redirect('admin_authorize')
        profile = target.profile
        if action == 'disable':
            profile.account_status = '已禁用'
            profile.save(update_fields=['account_status'])
            log_operation(request.user, '禁用账号', target_type='User', target_id=target.username,
                          detail=f'禁用 {target.username}', request=request)
            messages.success(request, f'已禁用 {target.username}。')
        elif action == 'approve':
            role = request.POST.get('role', '')
            if role:
                from django.contrib.auth.models import Group
                target.groups.clear()
                target.groups.add(Group.objects.get(name=role))
            profile.merchant_id = request.POST.get('merchant') or None
            profile.salesperson_id = request.POST.get('salesperson') or None
            profile.department_id = request.POST.get('department') or None
            profile.account_status = '已授权'
            profile.authorized_by = request.user
            profile.authorized_at = timezone.now()
            profile.save(update_fields=['merchant', 'salesperson', 'department',
                                        'account_status', 'authorized_by', 'authorized_at'])
            log_operation(request.user, '授权账号', target_type='User', target_id=target.username,
                          detail=f'授权 {target.username} 角色={role}', request=request)
            messages.success(request, f'已授权 {target.username}（{role}）。')
        return redirect('admin_authorize')

    pending = User.objects.filter(profile__account_status='待授权').select_related('profile')
    authorized = User.objects.filter(profile__account_status='已授权').select_related('profile')[:100]
    ctx = {
        'pending_users': pending,
        'authorized_users': authorized,
        'roles': [ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_DEPARTMENT, ROLE_SALESPERSON, ROLE_MERCHANT, ROLE_QUERY],
        'merchants': Merchant.objects.filter(is_active=True),
        'salespeople': SalespersonProfile.objects.filter(is_active=True),
        'departments': Department.objects.filter(is_active=True),
    }
    return render(request, 'books/auth_authorize.html', ctx)


@require_admin
def invited_list(request):
    """管理员：维护邀请白名单。"""
    if request.method == 'POST':
        phone = request.POST.get('phone', '').strip()
        if phone:
            _, created = InvitedPhone.objects.get_or_create(
                phone=phone, defaults={'role_hint': request.POST.get('role_hint', ''),
                                       'created_by': request.user})
            if created:
                log_operation(request.user, '添加邀请白名单', target_type='InvitedPhone',
                              target_id=phone, request=request)
                messages.success(request, f'已添加 {phone}。')
            else:
                messages.info(request, f'{phone} 已在名单中。')
        return redirect('invited_list')
    rows = InvitedPhone.objects.all()
    return render(request, 'books/invited_list.html', {'rows': rows})


@require_admin
@require_POST
def invited_delete(request, pk):
    row = get_object_or_404(InvitedPhone, pk=pk)
    phone = row.phone
    row.delete()
    log_operation(request.user, '删除邀请白名单', target_type='InvitedPhone',
                  target_id=phone, request=request)
    messages.success(request, f'已移除 {phone}。')
    return redirect('invited_list')


# ===========================================================================
# B2B 订单视图：分层可见的订单列表/详情/看板/导出，户名匹配管理与复核
# ===========================================================================

@login_required
def order_list(request):
    qs = visible_order_queryset(request.user,
        Order.objects.select_related('counterparty', 'merchant', 'salesperson',
                                     'department', 'receiving_account'))
    ctx = {'orders': None, 'filters': {}}
    # 筛选
    direction = request.GET.get('direction', '').strip()
    merchant = request.GET.get('merchant', '').strip()
    salesperson = request.GET.get('salesperson', '').strip()
    department = request.GET.get('department', '').strip()
    match_status = request.GET.get('match_status', '').strip()
    keyword = request.GET.get('q', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    if direction:
        qs = qs.filter(direction=direction)
    if merchant:
        qs = qs.filter(merchant_id=merchant)
    if salesperson:
        qs = qs.filter(salesperson_id=salesperson)
    if department:
        qs = qs.filter(department_id=department)
    if match_status:
        qs = qs.filter(match_status=match_status)
    if date_from:
        qs = qs.filter(order_date__gte=date_from)
    if date_to:
        qs = qs.filter(order_date__lte=date_to)
    if keyword:
        qs = qs.filter(Q(counterparty_raw_name__icontains=keyword) |
                       Q(external_order_no__icontains=keyword) |
                       Q(remark__icontains=keyword))

    # 汇总（跨页全量）
    summary = qs.aggregate(total=Sum('total_amount'), paid=Sum('paid_amount'), cnt=Count('id'))
    summary['unpaid'] = (summary['total'] or 0) - (summary['paid'] or 0)

    sort_key = request.GET.get('sort', '')
    if sort_key in ('order_date', 'external_order_no', 'total_amount', 'paid_amount', 'counterparty_raw_name'):
        qs = qs.order_by(sort_key)
    else:
        qs = qs.order_by('-order_date', '-id')

    per_page = int(request.GET.get('per_page', 50) or 50)
    paginator = Paginator(qs, per_page)
    page = paginator.get_page(request.GET.get('page'))
    # 分页查询串（保留筛选条件）
    qd = request.GET.copy()
    qd.pop('page', None)
    ctx = {
        'orders': page, 'page_obj': page, 'querystring': qd.urlencode(),
        'per_page': per_page, 'summary': summary,
        'filters': {'direction': direction, 'merchant': merchant, 'salesperson': salesperson,
                    'department': department, 'match_status': match_status, 'keyword': keyword,
                    'date_from': date_from, 'date_to': date_to},
        'merchants': Merchant.objects.filter(is_active=True),
        'salespeople': SalespersonProfile.objects.filter(is_active=True),
        'departments': Department.objects.filter(is_active=True),
        'match_statuses': [c[0] for c in MATCH_STATUS_CHOICES],
    }
    return render(request, 'books/order_list.html', ctx)


@login_required
def order_detail(request, pk):
    rec = get_object_or_404(
        visible_order_queryset(request.user, Order.objects.select_related(
            'counterparty', 'merchant', 'salesperson', 'department', 'receiving_account')),
        pk=pk)
    raw = rec.raw_row.data if rec.raw_row else {}
    return render(request, 'books/order_detail.html', {'order': rec, 'raw': raw,
                  'payments': rec.payments.all()})


@login_required
def order_dashboard(request):
    base = visible_order_queryset(request.user, Order.objects.all())
    role = get_user_role(request.user)
    groups = {'by': 'merchant', 'rows': []}
    if role in (ROLE_ADMIN, ROLE_EXECUTIVE):
        groups['by'] = 'department'
        rows = base.values('department__name').annotate(
            total=Sum('total_amount'), paid=Sum('paid_amount'), cnt=Count('id'))
        groups['rows'] = [{'name': r['department__name'] or '（未分配）',
                           'total': r['total'], 'paid': r['paid'], 'unpaid': (r['total'] or 0)-(r['paid'] or 0),
                           'cnt': r['cnt']} for r in rows]
    else:
        rows = base.values('merchant__name').annotate(
            total=Sum('total_amount'), paid=Sum('paid_amount'), cnt=Count('id'))
        groups['rows'] = [{'name': r['merchant__name'] or '（未分配）',
                           'total': r['total'], 'paid': r['paid'], 'unpaid': (r['total'] or 0)-(r['paid'] or 0),
                           'cnt': r['cnt']} for r in rows]
    return render(request, 'books/order_dashboard.html', {'groups': groups, 'role': role})


@login_required
def order_export(request):
    qs = visible_order_queryset(request.user, Order.objects.select_related(
        'counterparty', 'merchant', 'salesperson', 'department'))
    # 复用列表筛选（简化：仅关键字与状态）
    if request.GET.get('match_status'):
        qs = qs.filter(match_status=request.GET['match_status'])
    if request.GET.get('q'):
        qs = qs.filter(Q(counterparty_raw_name__icontains=request.GET['q']) |
                       Q(external_order_no__icontains=request.GET['q']))
    import openpyxl
    from io import BytesIO
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '订单'
    ws.append(['订单号', '订单户名', '方向', '订单日期', '订单金额', '已收付', '未收付',
               '往来主体', '商户', '业务员', '部门', '匹配状态'])
    for o in qs.order_by('-order_date', '-id'):
        ws.append([o.external_order_no, o.counterparty_raw_name, o.direction,
                   str(o.order_date or ''), float(o.total_amount), float(o.paid_amount),
                   float(o.unpaid_amount),
                   o.counterparty.name if o.counterparty else '',
                   o.merchant.name if o.merchant else '',
                   o.salesperson.name if o.salesperson else o.salesperson_raw,
                   o.department.name if o.department else '', o.match_status])
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    resp = HttpResponse(out.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="orders.xlsx"'
    return resp


class AccountNameMappingListView(sorting.SortableListMixin, LoginRequiredMixin, ListView):
    model = AccountNameMapping
    template_name = 'books/accountname_mapping.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = AccountNameMapping.objects.select_related(
            'counterparty', 'merchant', 'salesperson', 'department', 'receiving_account')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(raw_name__icontains=q) | Q(normalized_name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from books.sorting import page_querystring
        ctx['querystring'] = page_querystring(self.request.GET)
        return ctx


class AccountNameMappingCreateView(AdminRequiredMixin, CreateView):
    model = AccountNameMapping
    fields = ['raw_name', 'counterparty', 'salesperson', 'department', 'merchant',
              'receiving_account', 'match_source', 'is_manual', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('accountname_list')

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.normalized_name = normalize_account_name(obj.raw_name)
        obj.confidence = 1.0
        obj.created_by = self.request.user
        resp = super().form_valid(form)
        log_operation(self.request.user, '新增户名映射', target_type='AccountNameMapping',
                      target_id=obj.raw_name, request=self.request)
        return resp


class AccountNameMappingUpdateView(AdminRequiredMixin, UpdateView):
    model = AccountNameMapping
    fields = ['raw_name', 'counterparty', 'salesperson', 'department', 'merchant',
              'receiving_account', 'match_source', 'is_manual', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('accountname_list')

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.normalized_name = normalize_account_name(obj.raw_name)
        resp = super().form_valid(form)
        log_operation(self.request.user, '编辑户名映射', target_type='AccountNameMapping',
                      target_id=obj.raw_name, request=self.request)
        return resp


class AccountNameMappingDeleteView(AdminRequiredMixin, DeleteView):
    model = AccountNameMapping
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('accountname_list')


@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def accountname_review(request):
    """待复核/未匹配户名：给出模糊建议，管理员可一键建立映射并回填订单。"""
    from books.matching.engine import resolve_account_name
    unmatched = Order.objects.filter(match_status__in=['待复核', '未匹配']).exclude(
        counterparty_raw_name='').values('counterparty_raw_name').distinct()[:100]
    rows = []
    for item in unmatched:
        name = item['counterparty_raw_name']
        result = resolve_account_name(name, bump_usage=False)
        rows.append({'name': name, 'result': result})
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        cp_id = request.POST.get('counterparty') or None
        if name and cp_id:
            cp = Counterparty.objects.filter(pk=cp_id).first()
            if cp:
                mapping, _ = AccountNameMapping.objects.update_or_create(
                    raw_name=name, merchant=None,
                    defaults={'normalized_name': normalize_account_name(name),
                              'counterparty': cp, 'is_manual': True,
                              'match_source': '人工', 'confidence': 1.0, 'is_active': True,
                              'created_by': request.user})
                updated = Order.objects.filter(counterparty_raw_name=name).update(
                    counterparty=cp, match_status='已人工确认', match_confidence=1.0)
                log_operation(request.user, '复核户名映射', target_type='AccountNameMapping',
                              target_id=name, detail=f'指派主体 {cp.name}，回填 {updated} 单',
                              request=request)
                messages.success(request, f'{name} → {cp.name}，已回填 {updated} 单。')
        return redirect('accountname_review')
    return render(request, 'books/accountname_review.html', {
        'rows': rows,
        'counterparties': Counterparty.objects.filter(is_active=True),
    })


@require_admin
def import_lakala(request):
    """管理员：上传拉卡拉订单 Excel，走 B2B Order 导入。"""
    from books.imports.lakala import run_import_lakala
    if request.method == 'POST':
        f = request.FILES.get('file')
        if not f:
            messages.error(request, '请选择 Excel 文件。')
        else:
            upload = run_import_lakala('lakala_order', f, request.user)
            if upload.status == 'failed':
                messages.error(request, f'导入失败：{upload.note}')
            else:
                messages.success(request, f'导入完成：{upload.note}')
            log_operation(request.user, '导入拉卡拉订单', target_type='SourceUpload',
                          target_id=str(upload.id), detail=upload.note, request=request)
    return render(request, 'books/import_lakala.html')


# ===========================================================================
# 往来单位归一字典 Web 管理（0028）
# ===========================================================================
class CustomerEntityListView(LoginRequiredMixin, ListView):
    model = CustomerEntity
    template_name = 'books/norm_customerentity_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = CustomerEntity.objects.all()
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(tax_no__icontains=q) |
                           Q(platform_customer_id__icontains=q))
        return qs


class CustomerEntityCreateView(AdminRequiredMixin, CreateView):
    model = CustomerEntity
    fields = ['name', 'tax_no', 'platform_customer_id', 'party_type', 'is_active', 'remark']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_entity_list')
    extra_context = {'title': '客户主体', 'back': 'norm_entity_list'}

    def form_valid(self, form):
        resp = super().form_valid(form)
        log_operation(self.request.user, '新增客户主体', target_type='CustomerEntity',
                      target_id=str(form.instance.pk), request=self.request)
        return resp


class CustomerEntityUpdateView(AdminRequiredMixin, UpdateView):
    model = CustomerEntity
    fields = ['name', 'tax_no', 'platform_customer_id', 'party_type', 'is_active', 'remark']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_entity_list')
    extra_context = {'title': '客户主体', 'back': 'norm_entity_list'}

    def form_valid(self, form):
        resp = super().form_valid(form)
        log_operation(self.request.user, '编辑客户主体', target_type='CustomerEntity',
                      target_id=str(form.instance.pk), request=self.request)
        return resp


class CustomerEntityDeleteView(AdminRequiredMixin, DeleteView):
    model = CustomerEntity
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('norm_entity_list')
    extra_context = {'back': 'norm_entity_list'}


class CustomerAliasListView(LoginRequiredMixin, ListView):
    model = CustomerAlias
    template_name = 'books/norm_customeralias_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = CustomerAlias.objects.select_related('entity')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(alias_value__icontains=q) | Q(normalized_value__icontains=q) |
                           Q(entity__name__icontains=q))
        t = self.request.GET.get('alias_type', '').strip()
        if t:
            qs = qs.filter(alias_type=t)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['alias_types'] = CUSTOMER_ALIAS_TYPE_CHOICES
        ctx['filter_type'] = self.request.GET.get('alias_type', '')
        return ctx


class CustomerAliasCreateView(AdminRequiredMixin, CreateView):
    model = CustomerAlias
    fields = ['alias_type', 'alias_value', 'entity', 'platform_shop_id',
              'is_manual', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_alias_list')
    extra_context = {'title': '客户归一别名', 'back': 'norm_alias_list'}

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.normalized_value = normalize_account_name(obj.alias_value)
        obj.created_by = self.request.user
        resp = super().form_valid(form)
        log_operation(self.request.user, '新增客户归一别名', target_type='CustomerAlias',
                      target_id=obj.alias_value, request=self.request)
        return resp


class CustomerAliasUpdateView(AdminRequiredMixin, UpdateView):
    model = CustomerAlias
    fields = ['alias_type', 'alias_value', 'entity', 'platform_shop_id',
              'is_manual', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_alias_list')
    extra_context = {'title': '客户归一别名', 'back': 'norm_alias_list'}

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.normalized_value = normalize_account_name(obj.alias_value)
        resp = super().form_valid(form)
        log_operation(self.request.user, '编辑客户归一别名', target_type='CustomerAlias',
                      target_id=obj.alias_value, request=self.request)
        return resp


class CustomerAliasDeleteView(AdminRequiredMixin, DeleteView):
    model = CustomerAlias
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('norm_alias_list')
    extra_context = {'back': 'norm_alias_list'}


class OwnerListView(LoginRequiredMixin, ListView):
    model = Owner
    template_name = 'books/norm_owner_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = Owner.objects.all()
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(platform_owner_id__icontains=q))
        return qs


class OwnerCreateView(AdminRequiredMixin, CreateView):
    model = Owner
    fields = ['platform_owner_id', 'name', 'tax_no', 'is_active', 'remark']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_owner_list')
    extra_context = {'title': '货主', 'back': 'norm_owner_list'}


class OwnerUpdateView(AdminRequiredMixin, UpdateView):
    model = Owner
    fields = ['platform_owner_id', 'name', 'tax_no', 'is_active', 'remark']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_owner_list')
    extra_context = {'title': '货主', 'back': 'norm_owner_list'}


class OwnerDeleteView(AdminRequiredMixin, DeleteView):
    model = Owner
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('norm_owner_list')
    extra_context = {'back': 'norm_owner_list'}


class OwnerAccountMapListView(LoginRequiredMixin, ListView):
    model = OwnerAccountMap
    template_name = 'books/norm_owneraccount_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = OwnerAccountMap.objects.select_related('owner', 'account')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(owner__name__icontains=q) | Q(account__account_name__icontains=q))
        return qs


class OwnerAccountMapCreateView(AdminRequiredMixin, CreateView):
    model = OwnerAccountMap
    fields = ['owner', 'account', 'channel_type', 'pay_method',
              'priority', 'is_default', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_owneraccount_list')
    extra_context = {'title': '货主收款账户字典', 'back': 'norm_owneraccount_list'}


class OwnerAccountMapUpdateView(AdminRequiredMixin, UpdateView):
    model = OwnerAccountMap
    fields = ['owner', 'account', 'channel_type', 'pay_method',
              'priority', 'is_default', 'is_active', 'note']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('norm_owneraccount_list')
    extra_context = {'title': '货主收款账户字典', 'back': 'norm_owneraccount_list'}


class OwnerAccountMapDeleteView(AdminRequiredMixin, DeleteView):
    model = OwnerAccountMap
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('norm_owneraccount_list')
    extra_context = {'back': 'norm_owneraccount_list'}


class PaymentReceiptListView(LoginRequiredMixin, ListView):
    model = PaymentReceipt
    template_name = 'books/norm_paymentreceipt_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        from django.db.models import Sum as _Sum
        qs = PaymentReceipt.objects.select_related('owner', 'entity', 'receiving_account', 'order')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(payer_name__icontains=q) | Q(external_ref__icontains=q) |
                           Q(receiving_account_name__icontains=q))
        st = self.request.GET.get('match_status', '').strip()
        if st:
            qs = qs.filter(match_status=st)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['match_statuses'] = [c[0] for c in MATCH_STATUS_CHOICES]
        ctx['filter_status'] = self.request.GET.get('match_status', '')
        return ctx


@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def normalization_review(request):
    """待复核归一：为未匹配的收款明细/订单指派主体/货主并回填。"""
    if request.method == 'POST':
        kind = request.POST.get('kind', '')
        pk = request.POST.get('pk', '')
        entity_id = request.POST.get('entity') or None
        owner_id = request.POST.get('owner') or None
        if kind == 'receipt' and pk:
            r = PaymentReceipt.objects.filter(pk=pk).first()
            if r:
                changed = []
                if entity_id:
                    r.entity_id = entity_id; changed.append('entity')
                if owner_id:
                    r.owner_id = owner_id; changed.append('owner')
                if changed:
                    r.match_status = '已人工确认'
                    changed.append('match_status')
                    r.save(update_fields=changed)
                    log_operation(request.user, '复核收款明细归一',
                                  target_type='PaymentReceipt', target_id=str(pk),
                                  detail=f'归一 {changed}', request=request)
                    messages.success(request, f'收款明细 #{pk} 已人工归一。')
        elif kind == 'order' and pk:
            o = Order.objects.filter(pk=pk).first()
            if o and owner_id:
                o.owner_id = owner_id
                o.match_status = '已人工确认'
                o.save(update_fields=['owner', 'match_status'])
                log_operation(request.user, '复核订单货主归一',
                              target_type='Order', target_id=str(pk),
                              detail=f'货主 {owner_id}', request=request)
                messages.success(request, f'订单 #{pk} 已指派货主。')
        elif kind == 'alias' and pk:
            # 给付款人锚点指派客户主体，并把同一归一值的收款明细批量回填
            a = CustomerAlias.objects.filter(pk=pk).first()
            if a and entity_id:
                ent = CustomerEntity.objects.filter(pk=entity_id).first()
                if ent:
                    a.entity = ent
                    a.is_manual = True
                    a.save(update_fields=['entity', 'is_manual'])
                    # 回填所有同归一值的收款明细
                    from books.imports.utils import normalize_account_name
                    norm = a.normalized_value
                    matched = []
                    if a.alias_type == '付款人名称':
                        for r in PaymentReceipt.objects.filter(payer_name__isnull=False):
                            if normalize_account_name(r.payer_name) == norm:
                                matched.append(r)
                    elif a.alias_type == '付款人账号':
                        for r in PaymentReceipt.objects.filter(payer_account__isnull=False):
                            if normalize_account_name(r.payer_account) == norm:
                                matched.append(r)
                    n = 0
                    for r in matched:
                        r.entity = ent
                        r.match_status = '已人工确认'
                        r.save(update_fields=['entity', 'match_status'])
                        n += 1
                    log_operation(request.user, '付款人别名指派', target_type='CustomerAlias',
                                  target_id=str(pk), detail=f'指派 {ent.name}，回填 {n} 笔',
                                  request=request)
                    messages.success(request, f'{a.alias_value} → {ent.name}，已回填 {n} 笔收款明细。')
        return redirect('norm_review')

    receipts = PaymentReceipt.objects.filter(
        match_status__in=['未匹配', '待复核']).select_related(
        'owner', 'entity', 'receiving_account')[:100]
    orders = Order.objects.filter(match_status__in=['未匹配', '待复核']).exclude(
        platform_owner_id='').select_related('owner')[:100]
    # 未指派的付款人锚点（批量建档生成），按 note 里的金额/笔数排序展示前 100
    unassigned = list(CustomerAlias.objects.filter(
        entity__isnull=True, is_active=True).order_by('alias_type', 'id')[:200])
    return render(request, 'books/norm_review.html', {
        'receipts': receipts,
        'orders': orders,
        'unassigned_aliases': unassigned,
        'entities': CustomerEntity.objects.filter(is_active=True),
        'owners': Owner.objects.filter(is_active=True),
    })


@require_admin
def import_payment_page(request):
    """管理员：上传收款明细 Excel。"""
    from books.imports.payment_receipt import run_import_payment_receipt
    if request.method == 'POST':
        f = request.FILES.get('file')
        if not f:
            messages.error(request, '请选择 Excel 文件。')
        else:
            incremental = request.POST.get('incremental') == '1'
            owner_id = request.POST.get('owner') or None
            upload = run_import_payment_receipt(f, request.user, incremental=incremental,
                                                default_owner=owner_id)
            if upload.status == 'failed':
                messages.error(request, f'导入失败：{upload.note}')
            else:
                messages.success(request, f'导入完成：{upload.note}')
            log_operation(request.user, '导入收款明细', target_type='SourceUpload',
                          target_id=str(upload.id), detail=upload.note, request=request)
    from books.models import Owner as _Owner
    return render(request, 'books/import_payment.html',
                  {'owners': _Owner.objects.filter(is_active=True)})


# ===========================================================================
# 店铺往来账：往来明细 / 订单检索 / 穿透 / 抹零
# ===========================================================================
SHOP_LEDGER_SORT = {
    'date': ('order_date', sorting.DESC),
    'shop': ('shop_name', sorting.ASC),
    'order_no': ('external_order_no', sorting.ASC),
    'amount': ('total_amount', sorting.DESC),
    'refund': ('refund_amount', sorting.DESC),
    'cost': ('cost_amount', sorting.DESC),
    'region': ('region', sorting.ASC),
    'supervisor': ('supervisor', sorting.ASC),
}


def _shop_ledger_queryset(request):
    """往来明细/检索的公共过滤：先过行级权限，再套筛选条件。"""
    qs = visible_order_queryset(request.user, Order.objects.select_related(
        'salesperson', 'department', 'merchant', 'counterparty', 'customer_entity'))
    filters = {
        'order_no': request.GET.get('order_no', '').strip(),
        'shop_name': request.GET.get('shop_name', '').strip(),
        'salesperson': request.GET.get('salesperson', '').strip(),
        'region': request.GET.get('region', '').strip(),
        'status': request.GET.get('status', '').strip(),
        'date_from': request.GET.get('date_from', '').strip(),
        'date_to': request.GET.get('date_to', '').strip(),
        'q': request.GET.get('q', '').strip(),
    }
    if filters['order_no']:
        qs = qs.filter(external_order_no__icontains=filters['order_no'])
    if filters['shop_name']:
        qs = qs.filter(shop_name__icontains=filters['shop_name'])
    if filters['region']:
        qs = qs.filter(region__icontains=filters['region'])
    if filters['status']:
        qs = qs.filter(order_status=filters['status'])
    if filters['salesperson']:
        sp = filters['salesperson']
        if sp.isdigit():
            qs = qs.filter(Q(salesperson_id=int(sp)) | Q(salesperson_raw__icontains=sp))
        else:
            qs = qs.filter(Q(salesperson__name__icontains=sp) | Q(salesperson_raw__icontains=sp))
    if filters['date_from']:
        qs = qs.filter(order_date__gte=filters['date_from'])
    if filters['date_to']:
        qs = qs.filter(order_date__lte=filters['date_to'])
    if filters['q']:
        qs = qs.filter(Q(external_order_no__icontains=filters['q']) |
                       Q(shop_name__icontains=filters['q']) |
                       Q(counterparty_raw_name__icontains=filters['q']) |
                       Q(goods_summary__icontains=filters['q']) |
                       Q(remark__icontains=filters['q']))
    return qs, filters


def _shop_ledger_summary(qs):
    a = qs.aggregate(cnt=Count('id'), total=Sum('total_amount'), goods=Sum('goods_amount'),
                     discount=Sum('discount_amount'), refund=Sum('refund_amount'),
                     cost=Sum('cost_amount'), round_off=Sum('round_off'))
    net = (a['total'] or 0) - (a['refund'] or 0) - (a['round_off'] or 0)
    profit = net - (a['cost'] or 0)
    a.update(net=net, profit=profit,
             margin=float(profit / net) if net > 0 else 0)
    return a


@login_required
def shop_ledger_list(request):
    """往来明细 + 订单检索：15 列表头，按订单id可穿透。"""
    qs, filters = _shop_ledger_queryset(request)
    summary = _shop_ledger_summary(qs)
    qs, sort = sorting.sort_queryset(qs, request.GET, fields=SHOP_LEDGER_SORT,
                                     default=('order_date', 'id'),
                                     default_key='date', default_dir=sorting.DESC)
    per_page_choices = [20, 50, 100, 200]
    try:
        per_page = int(request.GET.get('per_page', 50))
    except (TypeError, ValueError):
        per_page = 50
    if per_page not in per_page_choices:
        per_page = 50
    page_obj = Paginator(qs, per_page).get_page(request.GET.get('page'))
    qd = request.GET.copy()
    qd.pop('page', None)
    base = visible_order_queryset(request.user, Order.objects.all())
    ctx = {
        'page_obj': page_obj, 'orders': page_obj, 'summary': summary, 'filters': filters,
        'querystring': qd.urlencode(), 'per_page': per_page, 'per_page_choices': per_page_choices,
        'sort': sort, 'role': get_user_role(request.user),
        'regions': sorted({r for r in base.values_list('region', flat=True) if r}),
        'statuses': sorted({s for s in base.values_list('order_status', flat=True) if s}),
        'salespeople': SalespersonProfile.objects.filter(is_active=True),
    }
    return render(request, 'books/shop_ledger_list.html', ctx)


@login_required
def shop_ledger_export(request):
    """与页面同口径的 Excel 导出（15 列）。"""
    qs, _ = _shop_ledger_queryset(request)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '店铺往来明细'
    headers = ['单据类型', '店铺名称', '订单id', '日期', '金额', '商品金额', '折扣', '退款',
               '成本金额', '毛利', '毛利率', '状态', '业务员', '业务主管', '区域']
    ws.append(headers)
    for o in qs.order_by('-order_date', '-id'):
        ws.append([
            '订单', o.shop_name, o.external_order_no, str(o.order_date or ''),
            float(o.net_amount), float(o.goods_amount), float(o.discount_amount),
            float(o.refund_amount), float(o.cost_amount), float(o.gross_profit),
            o.gross_margin_pct, o.order_status,
            o.salesperson.name if o.salesperson_id else o.salesperson_raw,
            o.supervisor, o.region,
        ])
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    resp = HttpResponse(out.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="shop_ledger.xlsx"'
    return resp


@login_required
def shop_order_detail(request, pk):
    """按订单id穿透：订单头 + 明细行（老数据回退原始行 JSON）+ 收付款。"""
    o = get_object_or_404(visible_order_queryset(request.user, Order.objects.select_related(
        'salesperson', 'department', 'merchant', 'counterparty', 'customer_entity')), pk=pk)
    details = list(o.details.all())
    raw_rows = []
    if not details and o.raw_row and isinstance(o.raw_row.data, dict):
        raw = o.raw_row.data
        raw_rows = [{
            'goods_name': raw.get('商品名') or raw.get('销售标题') or '',
            'spec': raw.get('销售标题') or '', 'qty': raw.get('数量') or '',
            'unit_price': raw.get('单价') or '', 'subtotal': raw.get('小计') or '',
            'discount': raw.get('折扣') or '', 'paid_amount': raw.get('实付额') or '',
            'refund_amount': raw.get('退款额') or '',
        }]
    role = get_user_role(request.user)
    can_edit = role in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_DEPARTMENT)
    return render(request, 'books/shop_order_detail.html', {
        'order': o, 'details': details, 'raw_rows': raw_rows,
        'payments': o.payments.all(), 'raw': o.raw_row.data if o.raw_row else {},
        'can_edit': can_edit, 'role': role,
        'next': request.get_full_path(),
    })


@require_POST
@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_DEPARTMENT)
def shop_roundoff(request, pk):
    """订单抹零：只允许 0 ≤ round_off ≤ 当前净额+原抹零。"""
    from decimal import Decimal, InvalidOperation
    o = get_object_or_404(visible_order_queryset(request.user, Order.objects.all()), pk=pk)
    try:
        value = Decimal(request.POST.get('round_off', '0') or '0')
    except (InvalidOperation, ValueError):
        messages.error(request, '抹零金额格式不正确。')
        return redirect('shop_order_detail', pk=pk)
    if value < 0 or value > o.net_amount + o.round_off:
        messages.error(request, '抹零金额需在 0 与可抹金额之间。')
        return redirect('shop_order_detail', pk=pk)
    old = o.round_off
    o.round_off = value
    o.save(update_fields=['round_off', 'updated_at'])
    log_operation(request.user, '订单抹零', target_type='Order', target_id=str(pk),
                  detail=f'{old} → {value}', request=request)
    messages.success(request, '抹零已保存。')
    return redirect(request.POST.get('next') or 'shop_order_detail', pk=pk)


# ===========================================================================
# 店铺往来账：回款管理 / 店铺工商字典 / 店铺开票记录
# ===========================================================================
@login_required
def shop_receipt_list(request):
    """回款表管理：显示账户类型/店铺名/编辑人，可编辑店铺名称。"""
    qs = PaymentReceipt.objects.select_related('receiving_account', 'entity', 'edited_by', 'owner')
    filters = {
        'shop_name': request.GET.get('shop_name', '').strip(),
        'payer': request.GET.get('payer', '').strip(),
        'date_from': request.GET.get('date_from', '').strip(),
        'date_to': request.GET.get('date_to', '').strip(),
    }
    if filters['shop_name']:
        qs = qs.filter(shop_name__icontains=filters['shop_name'])
    if filters['payer']:
        qs = qs.filter(payer_name__icontains=filters['payer'])
    if filters['date_from']:
        qs = qs.filter(pay_date__gte=filters['date_from'])
    if filters['date_to']:
        qs = qs.filter(pay_date__lte=filters['date_to'])
    summary = qs.aggregate(cnt=Count('id'), amount=Sum('amount'))
    page_obj = Paginator(qs.order_by('-pay_date', '-id'), 50).get_page(request.GET.get('page'))
    qd = request.GET.copy(); qd.pop('page', None)
    role = get_user_role(request.user)
    return render(request, 'books/shop_receipt_list.html', {
        'page_obj': page_obj, 'rows': page_obj, 'filters': filters, 'summary': summary,
        'querystring': qd.urlencode(), 'can_edit': role in (ROLE_ADMIN, ROLE_EXECUTIVE),
        'shop_choices': ShopBusinessRegistry.objects.values_list('shop_name', flat=True),
    })


@require_POST
@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def shop_receipt_edit(request, pk):
    """编辑回款店铺名称，保留编辑人与编辑时间。"""
    r = get_object_or_404(PaymentReceipt, pk=pk)
    old = r.shop_name
    r.shop_name = request.POST.get('shop_name', '').strip()
    r.edited_by = request.user
    r.edited_at = timezone.now()
    r.save(update_fields=['shop_name', 'edited_by', 'edited_at', 'updated_at'])
    log_operation(request.user, '编辑回款店铺名称', target_type='PaymentReceipt',
                  target_id=str(pk), detail=f'{old} → {r.shop_name}', request=request)
    messages.success(request, '店铺名称已保存。')
    return redirect(request.POST.get('next') or 'shop_receipt_list')


class ShopRegistryListView(LoginRequiredMixin, ListView):
    model = ShopBusinessRegistry
    template_name = 'books/shop_registry_list.html'
    context_object_name = 'rows'
    paginate_by = 50

    def get_queryset(self):
        qs = ShopBusinessRegistry.objects.select_related('entity')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(shop_name__icontains=q) | Q(registered_name__icontains=q) |
                           Q(credit_code__icontains=q))
        return qs


class ShopRegistryCreateView(AdminRequiredMixin, CreateView):
    model = ShopBusinessRegistry
    fields = ['shop_name', 'registered_name', 'credit_code', 'entity', 'source']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('shop_registry_list')
    extra_context = {'title': '店铺工商字典', 'back': 'shop_registry_list'}

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        resp = super().form_valid(form)
        log_operation(self.request.user, '新增店铺字典', target_type='ShopBusinessRegistry',
                      target_id=str(form.instance.pk), request=self.request)
        return resp


class ShopRegistryUpdateView(AdminRequiredMixin, UpdateView):
    model = ShopBusinessRegistry
    fields = ['shop_name', 'registered_name', 'credit_code', 'entity', 'source']
    template_name = 'books/data_form.html'
    success_url = reverse_lazy('shop_registry_list')
    extra_context = {'title': '店铺工商字典', 'back': 'shop_registry_list'}

    def form_valid(self, form):
        form.instance.updated_by = self.request.user
        resp = super().form_valid(form)
        log_operation(self.request.user, '编辑店铺字典', target_type='ShopBusinessRegistry',
                      target_id=str(form.instance.pk), request=self.request)
        return resp


class ShopRegistryDeleteView(AdminRequiredMixin, DeleteView):
    model = ShopBusinessRegistry
    template_name = 'books/data_confirm_delete.html'
    success_url = reverse_lazy('shop_registry_list')
    extra_context = {'back': 'shop_registry_list'}


@login_required
def shop_invoice_list(request):
    """店铺开票记录：按店铺筛选发票，发票号穿透到发票货物明细。"""
    invoice_sources = ['sales_invoice', 'purchase_invoice']
    qs = MasterRecord.objects.filter(source_type__in=invoice_sources).exclude(
        tag__in=EXCLUDED_TAGS)
    filters = {'shop': request.GET.get('shop', '').strip(),
               'q': request.GET.get('q', '').strip()}
    if filters['shop']:
        qs = qs.filter(unit_name__icontains=filters['shop'])
    if filters['q']:
        qs = qs.filter(Q(unit_name__icontains=filters['q']) |
                       Q(invoice_number__icontains=filters['q']))
    summary = qs.aggregate(cnt=Count('id'), amount=Sum('debit'))
    page_obj = Paginator(qs.order_by('-occur_date', '-id'), 50).get_page(request.GET.get('page'))
    qd = request.GET.copy(); qd.pop('page', None)
    return render(request, 'books/shop_invoice_list.html', {
        'page_obj': page_obj, 'rows': page_obj, 'filters': filters, 'summary': summary,
        'querystring': qd.urlencode(),
    })


# ===========================================================================
# 店铺往来账：催款 / 账期 / 对账单
# ===========================================================================
def _shop_receivables(user, as_of=None):
    """按店铺汇总未收应收与账龄桶；账期取店铺关联客户主体的 credit_term。"""
    as_of = as_of or timezone.localdate()
    qs = visible_order_queryset(user, Order.objects.select_related(
        'salesperson', 'customer_entity').exclude(shop_name=''))
    shops = {}
    for o in qs:
        shop = o.shop_name or '（未填店铺）'
        ent = o.customer_entity
        term = ent.credit_term if ent and ent.credit_term else 30
        rec = shops.setdefault(shop, {
            'shop_name': shop, 'entity': ent, 'credit_term': term,
            'salesperson': o.salesperson.name if o.salesperson_id else o.salesperson_raw,
            'items': [], 'total': 0,
        })
        remaining = float(o.net_amount) - float(o.paid_amount)
        if remaining > 1e-9 and o.order_date:
            rec['items'].append({'date': o.order_date, 'remaining': remaining})
        # 同一店铺可能有多业务员：以最后一单为准并标注多个
        if rec['salesperson'] and o.salesperson_id and rec['salesperson'] != o.salesperson.name:
            rec['salesperson'] = rec['salesperson'] + ' 等'
        rec['total'] += remaining
        if ent and ent.credit_term:
            rec['credit_term'] = ent.credit_term
    rows = []
    for shop, rec in shops.items():
        b1, b2, b3, b4, overdue = _aging_buckets(rec['items'], as_of, rec['credit_term'])
        rows.append({
            'shop_name': shop, 'entity': rec['entity'], 'credit_term': rec['credit_term'],
            'salesperson': rec['salesperson'] or '-', 'total': rec['total'],
            'b1': b1, 'b2': b2, 'b3': b3, 'b4': b4, 'overdue': overdue,
        })
    rows.sort(key=lambda r: -r['overdue'])
    return rows


@login_required
def shop_collection(request):
    """业务员往来催款管理：按店铺/业务员汇总应收与逾期，支持登记催款记录。"""
    as_of = to_date(request.GET.get('as_of', '')) or timezone.localdate()
    salesperson = request.GET.get('salesperson', '').strip()
    rows = _shop_receivables(request.user, as_of)
    if salesperson:
        rows = [r for r in rows if salesperson in (r['salesperson'] or '')]
    reminders = CollectionReminder.objects.select_related('salesperson', 'operator')
    if salesperson:
        reminders = reminders.filter(salesperson__name__icontains=salesperson)
    page_obj = Paginator(rows, 50).get_page(request.GET.get('page'))
    qd = request.GET.copy(); qd.pop('page', None)
    return render(request, 'books/shop_collection.html', {
        'page_obj': page_obj, 'rows': page_obj, 'as_of': as_of,
        'salesperson': salesperson, 'querystring': qd.urlencode(),
        'reminders': reminders[:20], 'role': get_user_role(request.user),
        'salespeople': SalespersonProfile.objects.filter(is_active=True),
    })


@require_POST
@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_SALESPERSON, ROLE_DEPARTMENT)
def shop_reminder_add(request):
    """登记催款记录。"""
    shop_name = request.POST.get('shop_name', '').strip()
    if not shop_name:
        messages.error(request, '请选择店铺。')
        return redirect('shop_collection')
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(request.POST.get('amount', '0') or '0')
    except (InvalidOperation, ValueError):
        amount = 0
    sp_id = request.POST.get('salesperson') or None
    r = CollectionReminder.objects.create(
        shop_name=shop_name, salesperson_id=sp_id, amount=amount,
        due_date=to_date(request.POST.get('due_date', '')) or None,
        remind_date=to_date(request.POST.get('remind_date', '')) or timezone.localdate(),
        method=request.POST.get('method', '').strip(),
        content=request.POST.get('content', '').strip(),
        operator=request.user,
    )
    log_operation(request.user, '登记催款记录', target_type='CollectionReminder',
                  target_id=str(r.pk), detail=shop_name, request=request)
    messages.success(request, '催款记录已登记。')
    return redirect(request.POST.get('next') or 'shop_collection')


@login_required
def shop_aging(request):
    """账期管理：按店铺查看账期与账龄，管理员可修改账期天数。"""
    as_of = to_date(request.GET.get('as_of', '')) or timezone.localdate()
    rows = _shop_receivables(request.user, as_of)
    page_obj = Paginator(rows, 50).get_page(request.GET.get('page'))
    qd = request.GET.copy(); qd.pop('page', None)
    return render(request, 'books/shop_aging.html', {
        'page_obj': page_obj, 'rows': page_obj, 'as_of': as_of,
        'querystring': qd.urlencode(), 'role': get_user_role(request.user),
    })


@require_POST
@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def shop_credit_term_update(request):
    """修改店铺对应客户主体的账期天数。"""
    entity_id = request.POST.get('entity')
    try:
        term = int(request.POST.get('credit_term', '30'))
    except (TypeError, ValueError):
        term = 30
    ent = CustomerEntity.objects.filter(pk=entity_id).first()
    if ent:
        old = ent.credit_term
        ent.credit_term = max(0, term)
        ent.save(update_fields=['credit_term', 'updated_at'])
        log_operation(request.user, '修改账期', target_type='CustomerEntity',
                      target_id=str(ent.pk), detail=f'{old} → {ent.credit_term} 天', request=request)
        messages.success(request, '账期已更新。')
    else:
        messages.error(request, '未找到对应客户主体，请先在客户主体中维护。')
    return redirect(request.POST.get('next') or 'shop_aging')


def _shop_statement_rows(shop_name, period_from, period_to, user=None):
    """按店铺+期间生成对账明细（订单按净额，回款按金额）。"""
    qs = Order.objects.filter(shop_name=shop_name, order_date__gte=period_from,
                              order_date__lte=period_to)
    if user is not None:
        qs = visible_order_queryset(user, qs)
    rows = []
    for o in qs.order_by('order_date', 'id'):
        rows.append({'date': o.order_date, 'type': '订单', 'no': o.external_order_no,
                     'amount': float(o.net_amount), 'paid': float(o.paid_amount),
                     'remark': o.goods_summary[:60]})
    for r in PaymentReceipt.objects.filter(shop_name=shop_name, pay_date__gte=period_from,
                                            pay_date__lte=period_to).order_by('pay_date', 'id'):
        rows.append({'date': r.pay_date, 'type': '回款', 'no': r.external_ref,
                     'amount': 0, 'paid': float(r.amount), 'remark': r.payer_name})
    return rows


@login_required
def shop_statements(request):
    """对账单管理：生成/查看对账单与发放编号。"""
    if request.method == 'POST':
        shop_name = request.POST.get('shop_name', '').strip()
        period_from = to_date(request.POST.get('period_from', ''))
        period_to = to_date(request.POST.get('period_to', ''))
        if not (shop_name and period_from and period_to):
            messages.error(request, '请填写店铺名称与起止日期。')
            return redirect('shop_statements')
        rows = _shop_statement_rows(shop_name, period_from, period_to, request.user)
        amount = sum(r['amount'] - r['paid'] for r in rows)
        prefix = 'ST-' + period_to.strftime('%Y%m') + '-'
        seq = ShopStatement.objects.filter(letter_no__startswith=prefix).count() + 1
        letter_no = prefix + f'{seq:04d}'
        while ShopStatement.objects.filter(letter_no=letter_no).exists():
            seq += 1
            letter_no = prefix + f'{seq:04d}'
        st = ShopStatement.objects.create(
            letter_no=letter_no, shop_name=shop_name, period_from=period_from,
            period_to=period_to, amount=amount, created_by=request.user,
            remark=request.POST.get('remark', '').strip())
        log_operation(request.user, '生成对账单', target_type='ShopStatement',
                      target_id=str(st.pk), detail=letter_no, request=request)
        messages.success(request, f'对账单 {letter_no} 已生成。')
        return redirect('shop_statement_detail', pk=st.pk)
    qs = ShopStatement.objects.all()
    shop_name = request.GET.get('shop_name', '').strip()
    if shop_name:
        qs = qs.filter(shop_name__icontains=shop_name)
    page_obj = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'books/shop_statements.html', {
        'page_obj': page_obj, 'rows': page_obj, 'shop_name': shop_name,
        'role': get_user_role(request.user),
        'shop_choices': ShopBusinessRegistry.objects.values_list('shop_name', flat=True),
    })


@login_required
def shop_statement_detail(request, pk):
    st = get_object_or_404(ShopStatement, pk=pk)
    rows = _shop_statement_rows(st.shop_name, st.period_from, st.period_to, request.user)
    return render(request, 'books/shop_statement_detail.html', {
        'st': st, 'rows': rows,
        'total_due': sum(r['amount'] for r in rows),
        'total_paid': sum(r['paid'] for r in rows),
        'role': get_user_role(request.user),
    })


@require_POST
@require_roles(ROLE_ADMIN, ROLE_EXECUTIVE)
def shop_statement_issue(request, pk):
    st = get_object_or_404(ShopStatement, pk=pk)
    st.status = '已发放'
    st.issued_at = timezone.now()
    st.save(update_fields=['status', 'issued_at'])
    log_operation(request.user, '发放对账单', target_type='ShopStatement',
                  target_id=str(pk), detail=st.letter_no, request=request)
    messages.success(request, '对账单已发放。')
    return redirect('shop_statement_detail', pk=pk)


@login_required
def shop_statement_export(request, pk):
    st = get_object_or_404(ShopStatement, pk=pk)
    rows = _shop_statement_rows(st.shop_name, st.period_from, st.period_to, request.user)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '对账单'
    ws.append([f'对账单号：{st.letter_no}', f'店铺：{st.shop_name}',
               f'期间：{st.period_from} ~ {st.period_to}'])
    ws.append(['日期', '类型', '单号', '应收金额', '已收金额', '备注'])
    for r in rows:
        ws.append([str(r['date'] or ''), r['type'], r['no'], r['amount'], r['paid'], r['remark']])
    ws.append(['合计', '', '', sum(r['amount'] for r in rows), sum(r['paid'] for r in rows), ''])
    out = BytesIO(); wb.save(out); out.seek(0)
    resp = HttpResponse(out.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{st.letter_no}.xlsx"'
    return resp
