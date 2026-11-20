"""经手人/业务员 派生与校准辅助。

每条主表记录(MasterRecord)的 handler 即「经手人/业务员」（用户口中的复核人/开票人）。
历史记录里，某客户 occur_date 最新的一行的经手人，即其「最新经手人」，
可作为该客户的现任业务员（对应后台 CustomerSalesperson.is_current）。
开票员也能当业务员，故不再排除任何内部人员——是否最终成为现任由人工在提醒中裁定。
"""
from books.models import MasterRecord, FundReceiptHandler

# 来源类型 -> CustomerSalesperson.source 取值
SOURCE_LABEL = {
    'sales_invoice': '发票复核人',
    'bank': '收入流水经手人',
    'purchase_invoice': '手动',
    'other': '手动',
    'wechat': '手动',
    'acceptance': '手动',
    'fund_handler': '收入流水经手人',
}


def source_label_for(source_type):
    return SOURCE_LABEL.get(source_type, '手动')


def build_derived_latest_map():
    """返回 {unit_name: (latest_date, handler, source_type)}，
    取每个单位 occur_date 最新、handler 非空的一行。
    同时纳入主表各来源与资金明细业务员表(FundReceiptHandler)。"""
    best = {}
    for unit, date, handler, st in MasterRecord.objects.exclude(
            handler='').order_by('occur_date', 'id').values_list(
            'unit_name', 'occur_date', 'handler', 'source_type'):
        cur = best.get(unit)
        if cur is None or (date and (cur[0] is None or date > cur[0])):
            best[unit] = (date, handler, st)
    # 资金明细业务员表（独立于主表，原 other 来源，标签「收入流水经手人」）
    for unit, date, handler in FundReceiptHandler.objects.exclude(
            handler='').order_by('occur_date', 'id').values_list(
            'unit_name', 'occur_date', 'handler'):
        cur = best.get(unit)
        if cur is None or (date and (cur[0] is None or date > cur[0])):
            best[unit] = (date, handler, 'fund_handler')
    return best


def derived_latest_for(unit_name, cache=None):
    if cache is None:
        cache = build_derived_latest_map()
    rec = cache.get(unit_name)
    return rec[1] if rec else ''
