"""货币格式模板过滤器：符号 + 千分位 + 固定小数，支持多币种。

默认人民币 `¥`；可通过参数指定其它币种（代码或符号均可）：
    {{ price|cny }}            -> ¥1,234.00
    {{ price|cny:"USD" }}      -> $1,234.00
    {{ price|cny:4:"EUR" }}    -> €1,234.0000
    {{ price|cny:"£" }}        -> £1,234.00
"""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()

# 常见币种代码 -> 符号；直接传符号亦可（不在表中则原样使用）
CURRENCY_SYMBOLS = {
    'CNY': '¥', 'RMB': '¥', 'JPY': '¥',
    'USD': '$', 'HKD': 'HK$', 'CAD': 'C$', 'AUD': 'A$',
    'EUR': '€', 'GBP': '£', 'KRW': '₩', 'RUB': '₽',
}


@register.filter
def cny(value, decimals=2, symbol='¥'):
    """把数字格式化为货币字符串，如 ¥1,234.50 / $1,234.50。

    - None / '' / 0 长度字符串 → 返回 ''（保持表格对齐/空态一致）。
    - 兼容 int / float / Decimal；解析失败时原样返回，避免页面崩溃。
    - 多币种：第二个参数为币种代码（如 USD/EUR）或符号（如 $/€）；
      也支持以 ``|cny:"USD"`` 形式把币种作为唯一参数传入（此时小数位用默认 2）。
    """
    # 允许 |cny:"USD" 这种只传币种的形式（decimals 位置收到了字符串）
    if isinstance(decimals, str):
        sym_arg = decimals
        decimals = 2
    else:
        sym_arg = symbol
    sym = CURRENCY_SYMBOLS.get(sym_arg, sym_arg) or '¥'
    try:
        decimals = int(decimals)
    except (ValueError, TypeError):
        decimals = 2

    if value is None or value == '':
        return ''
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return value
    return sym + format(d, ',.%df' % decimals)
