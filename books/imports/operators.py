"""往来账管理系统 —— 算子（积木）注册表。

每个算子是一个可复用的转换单元，由用户在后台「列映射 → 映射步骤」里选块并填参，
串联成每列的转换管道。代码是权威来源，Operator 模型只是它的后台清单镜像。

apply(value, params, context) -> new_value
- value: 当前单元格（可能已被上一步算子处理过）
- params: 该步骤的参数 dict
- context: {'raw': 整行原始 dict}（供 lookup_name 等跨列算子使用）
"""
import re

from .utils import (
    to_date, to_decimal, fullwidth_brackets, extract_salesperson,
    normalize_unit_name,
)


def op_none(value, params, context):
    return value


def op_trim(value, params, context):
    return str(value).strip() if value not in (None, '') else value


def op_fullwidth_brackets(value, params, context):
    return fullwidth_brackets(str(value))


def op_replace(value, params, context):
    s = str(value)
    find = params.get('find', '') or ''
    repl = params.get('replace', '') or ''
    return s.replace(find, repl)


def op_regex_extract(value, params, context):
    s = str(value)
    pattern = params.get('pattern', '') or ''
    if not pattern:
        return ''
    group = int(params.get('group', 0) or 0)
    m = re.search(pattern, s)
    if m:
        try:
            return m.group(group)
        except (IndexError, re.error):
            return m.group(0)
    return ''


def op_regex_salesperson(value, params, context):
    return extract_salesperson(str(value))


def op_to_date(value, params, context):
    d = to_date(value)
    return d.isoformat() if d else ''


def op_to_decimal(value, params, context):
    return to_decimal(value)


def op_negate(value, params, context):
    return -to_decimal(value)


def op_abs(value, params, context):
    return abs(to_decimal(value))


def op_sum_columns(value, params, context):
    """跨列求和：把 params.columns 列出的原始列全部 to_decimal 后相加。

    用于「退款」这类由多列相加的字段（如线上支付-退款额 + 到货付款-退货额）。
    """
    total = 0
    raw = context.get('raw') or {}
    cols = params.get('columns') or []
    if isinstance(cols, str):
        cols = [c.strip() for c in cols.replace('，', ',').split(',') if c.strip()]
    for col in cols:
        total += to_decimal(raw.get(col))
    return total


def op_lookup_name(value, params, context):
    """按原始名查名称映射库；若 use_tax_no，则从同行原始税号列取税号。"""
    raw = context.get('raw', {}) or {}
    tax_no = ''
    if params.get('use_tax_no'):
        col = params.get('tax_no_column') or 'tax_no'
        tax_no = str(raw.get(col, '') or '').strip()
    return normalize_unit_name(str(value), tax_no)


def op_constant(value, params, context):
    return params.get('val', '') or ''


# ---------------------------------------------------------------------------
# 注册表：key -> {name, description, params_schema, apply}
# params_schema: [{name, label, type(str|int|bool|choice), default, choices?, required?}]
# ---------------------------------------------------------------------------
OPERATORS = {
    'none': {
        'name': '原样', 'description': '不做任何转换，直接透传。',
        'params_schema': [],
        'apply': op_none,
    },
    'trim': {
        'name': '去首尾空白', 'description': '去掉字符串首尾空白。',
        'params_schema': [],
        'apply': op_trim,
    },
    'fullwidth_brackets': {
        'name': '半角括号转全角', 'description': '将 () 转为全角（），并归一常见全角标点。',
        'params_schema': [],
        'apply': op_fullwidth_brackets,
    },
    'replace': {
        'name': '文本替换', 'description': '把单元格中的 find 替换为 replace。',
        'params_schema': [
            {'name': 'find', 'label': '查找', 'type': 'str', 'default': ''},
            {'name': 'replace', 'label': '替换为', 'type': 'str', 'default': ''},
        ],
        'apply': op_replace,
    },
    'regex_extract': {
        'name': '正则提取', 'description': '用正则表达式从单元格提取片段（支持 group 参数指定分组）。',
        'params_schema': [
            {'name': 'pattern', 'label': '正则', 'type': 'str', 'default': ''},
            {'name': 'group', 'label': '分组号', 'type': 'int', 'default': 0},
        ],
        'apply': op_regex_extract,
    },
    'regex_salesperson': {
        'name': '提取业务员姓名', 'description': '去常见前缀（复核/经办/开票等）并取中文 2-4 字姓名。',
        'params_schema': [],
        'apply': op_regex_salesperson,
    },
    'to_date': {
        'name': '日期解析', 'description': '将多种格式日期解析为标准 ISO 日期字符串。',
        'params_schema': [],
        'apply': op_to_date,
    },
    'to_decimal': {
        'name': '金额解析', 'description': '去除货币符号/千分位/空格，解析为金额（Decimal）。',
        'params_schema': [],
        'apply': op_to_decimal,
    },
    'negate': {
        'name': '取负', 'description': '对金额取负（用于红字/冲减方向）。',
        'params_schema': [],
        'apply': op_negate,
    },
    'abs': {
        'name': '绝对值', 'description': '对金额取绝对值。',
        'params_schema': [],
        'apply': op_abs,
    },
    'lookup_name': {
        'name': '名称查表归一', 'description': '按原始名（或税号）查名称映射库，归一到规范名。',
        'params_schema': [
            {'name': 'use_tax_no', 'label': '使用税号查表', 'type': 'bool', 'default': False},
            {'name': 'tax_no_column', 'label': '税号来源列', 'type': 'str', 'default': 'tax_no'},
        ],
        'apply': op_lookup_name,
    },
    'constant': {
        'name': '置常量', 'description': '忽略原值，直接写入固定常量（用于固定类别等）。',
        'params_schema': [
            {'name': 'val', 'label': '常量值', 'type': 'str', 'default': ''},
        ],
        'apply': op_constant,
    },
    'sum_columns': {
        'name': '跨列求和', 'description': '把指定原始列的数值相加（用于退款等多列相加字段）。',
        'params_schema': [
            {'name': 'columns', 'label': '列名列表(逗号分隔)', 'type': 'str', 'default': ''},
        ],
        'apply': op_sum_columns,
    },
}


def apply_operator(key, value, params, context):
    """按算子 key 执行转换；未知 key 透传。"""
    spec = OPERATORS.get(key)
    if not spec:
        return value
    return spec['apply'](value, params or {}, context or {})
