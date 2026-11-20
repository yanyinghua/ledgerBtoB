"""往来账管理系统 —— 导入引擎底层纯函数工具。

集中放与 DB 无关的解析/规范化函数，以及需要访问模型的规范化逻辑，
供 operators / rules / engine 复用，避免循环依赖。
"""
import datetime
import re
import unicodedata
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.db.models import Q

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _isna(v):
    try:
        return pd.isna(v)
    except (TypeError, ValueError):
        return v is None


def to_decimal(v):
    if v is None or v == '':
        return Decimal('0')
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).strip()
    if not s:
        return Decimal('0')
    # 去掉货币符号、千分位、空格
    s = re.sub(r'[¥￥,\s]', '', s)
    s = s.replace('（', '(').replace('）', ')')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    if not m:
        return Decimal('0')
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return Decimal('0')


def to_date(v):
    if _isna(v) or v == '':
        return None
    if isinstance(v, (datetime.datetime, datetime.date, pd.Timestamp)):
        return v.date() if hasattr(v, 'date') else v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y-%m-%d %H:%M:%S',
                '%Y/%m/%d %H:%M:%S', '%Y年%m月%d日',
                '%Y%m%d', '%Y%m%d %H:%M:%S', '%Y%m%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r'(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})', s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 紧凑格式 YYYYMMDD（建行等对公流水常用，无分隔符）
    m = re.search(r'(\d{4})(\d{2})(\d{2})', s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def fullwidth_brackets(s):
    """半角括号转全角，并顺带做常见全角标点归一。"""
    if not s:
        return s
    s = s.replace('(', '（').replace(')', '）')
    return s


def normalize_account_name(s):
    """B2B 户名归一化：用于户名匹配的归一化层。

    - 全角→半角（NFKC）
    - 去除空白与常见标点（括号、顿号、逗号、句号、连字符、斜杠等）
    - 拉丁字母转大写
    不剥离公司后缀（避免误合并不同主体）。
    """
    if s is None:
        return ''
    s = str(s).strip()
    if not s:
        return ''
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[\s（）()【】\[\]、,，.。\-—_/\\]', '', s)
    s = s.upper()
    return s


def extract_salesperson(s):
    """从复核人/经手人等字段用正则提取业务员姓名（中文 2-4 字）。

    适用于「字段本身就是姓名」（如发票的『复核人/经办』列直接是姓名）的场景：
    去首部常见责任人前缀后取首个中文姓名片段。
    注意：开票人/收款人 不是责任人，不得作为提取前缀。
    """
    if not s:
        return ''
    s = s.replace('（', '(').replace('）', ')')
    # 去掉常见责任人前缀（不含 开票/开票人/收款人）
    s = re.sub(r'^(复核人|复核|经办人|经办|经手人|经手|业务员)[：:\s]*', '', s)
    # 取首个中文姓名片段
    m = re.search(r'[\u4e00-\u9fa5]{2,4}', s)
    return m.group(0) if m else ''


# 复核/审核/经办/经手/业务员 等“责任人”前缀（可出现在字符串任意位置）。
# 注意：开票人/收款人 不是责任人（经手人），不得作为提取前缀，否则会把“开票人”误当经手人。
_REVIEWER_PREFIX = r'(?:复核人|复核|审核人|审核|经办人|经办|经手人|经手|业务员)'


def extract_reviewer_name(s):
    """从备注等自由文本中提取『复核人/经办/…』后紧跟的业务员姓名（中文 2-4 字）。

    与 extract_salesperson 不同：本函数只认带前缀的人名，且前缀可出现在字符串
    任意位置（如『对账单号：... 复核人:魏家荣』→ 魏家荣），不会误取『对账单号』
    等前缀文字。找不到则返回空。
    """
    if not s:
        return ''
    s = str(s).replace('（', '(').replace('）', ')')
    m = re.search(_REVIEWER_PREFIX + r'[：:\s]*([\u4e00-\u9fa5]{2,4})', s)
    return m.group(1) if m else ''


def normalize_unit_name(raw, tax_no):
    """按税号优先、否则按原始名称，从名称映射库取规范名。"""
    from books.models import NameMapping
    raw = (raw or '').strip()
    if not raw:
        return ''
    if tax_no:
        nm = NameMapping.objects.filter(tax_no=tax_no).first()
        if nm:
            return nm.canonical_name
    nm = NameMapping.objects.filter(raw_name=raw).first()
    if nm:
        return nm.canonical_name
    return raw


def apply_name_mapping_to_history(source_types=None):
    """按当前名称映射库，把历史单位名称原地归一（不重读文件、不删主表记录）。

    source_types: 可选的来源类型列表（MasterRecord.source_type）。提供时仅对
    主表中这些来源的行做归一；其余单位级表（经手人/期初/客户业务员）无来源
    概念，始终全部归一。
    语义与 normalize_unit_name 一致（平表、不链式传递）：
    仅对「当前库里实际存在的单位名」查 raw_name→canonical，命中则改；
    对带税号的表再按 tax_no→canonical 归一（税号优先）。
    CustomerSalesperson 有唯一约束 (unit_name, salesperson)，重命名可能产生
    重复，最后统一去重（保留 is_current=True 的那条）。
    返回各表更新条数字典。
    """
    from books.models import (
        MasterRecord, FundReceiptHandler, OpeningBalance, CustomerSalesperson,
        SalespersonChangeReminder, NameMapping,
    )
    from django.db.models import Count

    mappings = list(NameMapping.objects.all())
    by_raw = {m.raw_name: m.canonical_name for m in mappings if m.raw_name}
    by_tax = {m.tax_no: m.canonical_name for m in mappings if m.tax_no}

    # 仅对带 source_type 字段的表（主表）应用来源筛选
    summary = {}
    for model in (MasterRecord, FundReceiptHandler, OpeningBalance, CustomerSalesperson,
                 SalespersonChangeReminder):
        scope = {'source_type__in': source_types} if (source_types and hasattr(model, 'source_type')) else {}
        has_tax = any(
            f.name == 'tax_no' for f in model._meta.get_fields() if not f.auto_created
        )
        updated = 0
        # 按原始名归一：基于当前实际存在的名称查表，避免链式传递
        present = set(model.objects.filter(**scope).values_list('unit_name', flat=True).distinct())
        for u in present:
            canon = by_raw.get(u)
            if canon and canon != u:
                updated += model.objects.filter(unit_name=u, **scope).update(unit_name=canon)
        # 按税号归一（税号优先）
        if has_tax:
            for tax, canon in by_tax.items():
                updated += model.objects.filter(tax_no=tax, **scope).update(unit_name=canon)
        summary[model.__name__] = updated

    # CustomerSalesperson 唯一约束去重（重命名可能制造重复）
    dups = (CustomerSalesperson.objects.values('unit_name', 'salesperson')
            .annotate(c=Count('id')).filter(c__gt=1))
    removed = 0
    for d in dups:
        rows = list(CustomerSalesperson.objects.filter(
            unit_name=d['unit_name'], salesperson=d['salesperson']
        ).order_by('-is_current'))
        for r in rows[1:]:
            r.delete()
            removed += 1
    if removed:
        summary['CustomerSalesperson_dedup_removed'] = removed

    # SalespersonChangeReminder 唯一约束去重（重命名单位名可能制造重复）
    rdups = (SalespersonChangeReminder.objects.values('unit_name', 'suggested_salesperson', 'status')
             .annotate(c=Count('id')).filter(c__gt=1))
    r_removed = 0
    for d in rdups:
        rows = list(SalespersonChangeReminder.objects.filter(
            unit_name=d['unit_name'], suggested_salesperson=d['suggested_salesperson'],
            status=d['status']).order_by('-created_at'))
        for r in rows[1:]:
            r.delete()
            r_removed += 1
    if r_removed:
        summary['SalespersonChangeReminder_dedup_removed'] = r_removed
    return summary


def detect_red(remark):
    """识别红冲备注，返回 (tag, 蓝字发票号)。仅解析备注文本。"""
    if not remark:
        return '', ''
    if any(k in remark for k in ('红冲', '红字', '负数')):
        tag = '红冲'
    elif '蓝字' in remark:
        tag = '红冲'
    else:
        return '', ''
    # 蓝字数电发票号码（通常 20 位）
    m = re.search(r'(\d{20})', remark)
    if not m:
        m = re.search(r'(?:号码|No\.?|NO\.?)\s*[:：]?\s*(\d{8,20})', remark)
    blue = m.group(1) if m else ''
    return tag, blue


def upsert_customer_salesperson(unit_name, salesperson, source, occur_date, auto_current=False):
    """维护客户-业务员映射。

    - 始终记录候选行（unit, salesperson）并刷新其来源/生效日期（历史）。
    - auto_current=True（校准 / 后台「应用派生最新」）：按 effective_date 最新者翻现任（原行为）。
    - auto_current=False（导入）：不自动翻现任；若派生业务员与现任不一致，
      写入/刷新「业务员变更提醒」，由人工在后台确认后才生效。
      新客户（该单位尚无现任）首次导入直接归属为现任（无既有值可改，不提醒）。
      后台手动锁定（manual_override）的单位：既不翻现任，也不写提醒。
    """
    from books.models import CustomerSalesperson
    if not salesperson:
        return
    obj, created = CustomerSalesperson.objects.get_or_create(
        unit_name=unit_name, salesperson=salesperson,
        defaults={'source': source, 'effective_date': occur_date, 'is_current': False})
    if not created:
        changed = False
        if source and obj.source != source:
            obj.source = source
            changed = True
        if occur_date and (not obj.effective_date or occur_date > obj.effective_date):
            obj.effective_date = occur_date
            changed = True
        if changed:
            obj.save(update_fields=['source', 'effective_date'])

    rows = list(CustomerSalesperson.objects.filter(unit_name=unit_name))
    # 管理员手动锁定的客户：不自动改写现任，也不写提醒
    if any(r.manual_override for r in rows):
        return
    current = next((r for r in rows if r.is_current), None)
    if current is None:
        # 新客户：首次导入直接归属为现任（无冲突可提醒）
        if not obj.is_current:
            obj.is_current = True
            obj.save(update_fields=['is_current'])
        return
    if current.salesperson == salesperson:
        # 与现任一致：无需改动
        return
    if auto_current:
        # 校准 / 后台应用：按 effective_date 最新者翻现任
        latest = None
        for r in rows:
            if r.effective_date:
                if latest is None or r.effective_date > latest.effective_date:
                    latest = r
        if latest is None:
            latest = rows[0]
        for r in rows:
            is_cur = (r.pk == latest.pk)
            if r.is_current != is_cur:
                r.is_current = is_cur
                r.save(update_fields=['is_current'])
    else:
        # 导入：与现任不一致 → 记录/刷新提醒，不改现任
        record_salesperson_reminder(
            unit_name, current.salesperson, salesperson, source, occur_date)


def record_salesperson_reminder(unit_name, current_sp, suggested_sp, source, occur_date, upload=None):
    """记录/刷新一条「业务员变更提醒」（派生最新 ≠ 现任）。

    同一 (单位, 建议业务员, 待处理) 仅保留一条，避免每次导入的每一行都刷屏。
    """
    from books.models import SalespersonChangeReminder
    if not suggested_sp:
        return
    obj, created = SalespersonChangeReminder.objects.get_or_create(
        unit_name=unit_name, suggested_salesperson=suggested_sp, status='待处理',
        defaults={
            'current_salesperson': current_sp or '',
            'source': source,
            'occur_date': occur_date,
            'source_upload': upload,
        })
    if not created:
        obj.current_salesperson = current_sp or ''
        obj.source = source
        obj.occur_date = occur_date
        obj.source_upload = upload
        obj.save(update_fields=['current_salesperson', 'source', 'occur_date', 'source_upload'])
