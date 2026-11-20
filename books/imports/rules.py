"""往来账管理系统 —— 跨字段逻辑规则（积木）注册表。

规则块作用于「已构建的基础记录」之上，做跨字段判断（红冲/借贷方向/业务员映射/类别默认）。
与算子不同，规则不针对单格，而是整行级别的二次加工。代码是权威来源，
LogicRuleType 模型只是它的后台清单镜像。

apply(record, params, context)
- record: 尚未保存的 MasterRecord 实例（可被原地修改）
- params: 规则参数 dict
- context: {'raw': 整行原始 dict, 'source_type': ...}
"""
from decimal import Decimal

from .utils import detect_red, upsert_customer_salesperson


def _csv(val, default=''):
    """把逗号分隔参数字符串拆成列表。"""
    return [x.strip() for x in (val or default).split(',') if x.strip()]


def rule_red_detect(record, params, context):
    """红冲/作废识别：扫描指定来源列关键字，或负数发票标志列。"""
    raw = context.get('raw', {}) or {}

    red_fields = _csv(params.get('red_fields'), '备注')
    red_keywords = _csv(params.get('red_keywords'), '红冲,红字,负数')
    negate_field = (params.get('negate_field') or '是否正数发票').strip()
    negate_value = (params.get('negate_value') or '否').strip()
    void_fields = _csv(params.get('void_fields'), '发票状态')
    void_keywords = _csv(params.get('void_keywords'), '作废')

    def _hit(fields, keywords):
        for f in fields:
            val = str(raw.get(f, '') or '')
            for kw in keywords:
                if kw and kw in val:
                    return True
        return False

    if _hit(void_fields, void_keywords):
        record.tag = '作废'
        return

    is_red = _hit(red_fields, red_keywords)
    if not is_red and negate_field:
        is_red = str(raw.get(negate_field, '') or '').strip() == negate_value
    if is_red:
        record.tag = '红冲'
        _, blue = detect_red(str(raw.get('备注', '') or ''))
        if blue:
            record.blue_invoice_no = blue


def rule_direction_swap(record, params, context):
    """借贷方向互换（微信收/支等）：支出类把 credit 移到 debit。"""
    outflow = _csv(params.get('outflow_values'), '支,付,转出')
    inflow = _csv(params.get('inflow_values'), '收,入,转账收款')
    cat = record.category or ''
    if any(k and k in cat for k in outflow):
        if record.debit == 0 and record.credit > 0:
            record.debit, record.credit = record.credit, Decimal('0')
        record.category = '支出'
    elif any(k and k in cat for k in inflow):
        record.category = '收入'


def rule_salesperson_map(record, params, context):
    """客户-业务员映射：按来源字段（通常为 handler）+ 可选年份/类别门槛构建映射。"""
    handler = (record.handler or '').strip()
    if not handler:
        return
    min_year = int(params.get('min_year', 0) or 0)
    if min_year:
        # 缺日期或年份早于门槛都不满足「自某年起」条件，跳过
        if not record.occur_date or record.occur_date.year < min_year:
            return
    req = _csv(params.get('require_category'))
    if req and (record.category or '') not in req:
        return
    label = params.get('source_label') or '手动'
    # 导入路径恒为「只提醒、不自动改」：与现任不一致时写入业务员变更提醒，
    # 由人工在后台确认后才生效（auto_current=False）。
    upsert_customer_salesperson(record.unit_name, handler, label, record.occur_date,
                               auto_current=False)


def rule_category_default(record, params, context):
    """当类别为空时，按借贷方向给默认类别。"""
    if record.category:
        return
    d, c = record.debit, record.credit
    if d > 0 and c > 0:
        record.category = params.get('default_category') or '其他'
    elif d > 0:
        record.category = params.get('debit_category') or '借方'
    elif c > 0:
        record.category = params.get('credit_category') or '贷方'
    else:
        record.category = params.get('default_category') or '其他'


def rule_acceptance_direction(record, params, context):
    """承兑借贷方向：按「操作类型」把交易金额（元）记入借方(申请/付款)或贷方(签收/收款)。

    仅对含「操作类型」列的承兑行生效；迪链行无该列，本规则跳过（方向已由借方/贷方列直接给出）。
    约定：签收(收款)=贷方、申请(付款)=借方，与迪链文件口径一致。
    """
    raw = context.get('raw', {}) or {}
    op_field = (params.get('op_field') or '操作类型').strip()
    recv = _csv(params.get('recv_values'), '收')
    pay = _csv(params.get('pay_values'), '付,申请')
    op = str(raw.get(op_field, '') or '').strip()
    if not op:
        return
    if any(k and k in op for k in recv):
        record.credit = record.debit
        record.debit = Decimal('0')
        record.category = '签收'
    elif any(k and k in op for k in pay):
        record.credit = Decimal('0')
        record.category = '申请'


def rule_dilian_tag(record, params, context):
    """迪链标记：当标记列（默认「类别」）值等于指定值时，打上 tag（默认「迪链」）。

    迪链文件首列「类别」恒为「迪链」，据此把混入 acceptance 来源的迪链记录与普通承兑区分。
    """
    raw = context.get('raw', {}) or {}
    mark_field = (params.get('mark_field') or '类别').strip()
    mark_value = (params.get('mark_value') or '迪链').strip()
    tag = (params.get('tag') or '迪链').strip()
    if str(raw.get(mark_field, '') or '').strip() == mark_value:
        record.tag = tag


RULE_TYPES = {
    'red_detect': {
        'name': '红冲/作废识别', 'description': '扫描备注/标志列关键字，或负数发票标志，标记红冲/作废。',
        'params_schema': [
            {'name': 'red_fields', 'label': '红冲扫描列(逗号)', 'type': 'str', 'default': '备注'},
            {'name': 'red_keywords', 'label': '红冲关键字(逗号)', 'type': 'str', 'default': '红冲,红字,负数'},
            {'name': 'negate_field', 'label': '负数标志列', 'type': 'str', 'default': '是否正数发票'},
            {'name': 'negate_value', 'label': '负数标志值', 'type': 'str', 'default': '否'},
            {'name': 'void_fields', 'label': '作废扫描列(逗号)', 'type': 'str', 'default': '发票状态'},
            {'name': 'void_keywords', 'label': '作废关键字(逗号)', 'type': 'str', 'default': '作废'},
        ],
        'apply': rule_red_detect,
    },
    'direction_swap': {
        'name': '借贷方向互换', 'description': '微信等：支出类把贷方金额移到借方（或反之）。',
        'params_schema': [
            {'name': 'outflow_values', 'label': '支出判定值(逗号)', 'type': 'str', 'default': '支,付,转出'},
            {'name': 'inflow_values', 'label': '收入判定值(逗号)', 'type': 'str', 'default': '收,入,转账收款'},
        ],
        'apply': rule_direction_swap,
    },
    'salesperson_map': {
        'name': '客户-业务员映射', 'description': '按来源字段（业务员）维护客户-业务员映射，可设年份/类别门槛。',
        'params_schema': [
            {'name': 'source_field', 'label': '来源字段', 'type': 'str', 'default': 'handler'},
            {'name': 'require_category', 'label': '限定类别(逗号,空=不限)', 'type': 'str', 'default': ''},
            {'name': 'min_year', 'label': '最早年份(0=不限)', 'type': 'int', 'default': 0},
            {'name': 'source_label', 'label': '来源标签', 'type': 'str', 'default': '发票复核人'},
        ],
        'apply': rule_salesperson_map,
    },
    'category_default': {
        'name': '默认类别', 'description': '当类别为空时，按借贷方向赋予默认类别。',
        'params_schema': [
            {'name': 'debit_category', 'label': '借方类别', 'type': 'str', 'default': '借方'},
            {'name': 'credit_category', 'label': '贷方类别', 'type': 'str', 'default': '贷方'},
            {'name': 'default_category', 'label': '兜底类别', 'type': 'str', 'default': '其他'},
        ],
        'apply': rule_category_default,
    },
    'acceptance_direction': {
        'name': '承兑借贷方向', 'description': '按操作类型把交易金额记入借方(申请)或贷方(签收)；仅承兑行生效。',
        'params_schema': [
            {'name': 'op_field', 'label': '方向判定列', 'type': 'str', 'default': '操作类型'},
            {'name': 'recv_values', 'label': '收款判定值(逗号)', 'type': 'str', 'default': '收'},
            {'name': 'pay_values', 'label': '付款判定值(逗号)', 'type': 'str', 'default': '付,申请'},
        ],
        'apply': rule_acceptance_direction,
    },
    'dilian_tag': {
        'name': '迪链标记', 'description': '标记列值等于指定值时打 tag（区分迪链与普通承兑）。',
        'params_schema': [
            {'name': 'mark_field', 'label': '标记列', 'type': 'str', 'default': '类别'},
            {'name': 'mark_value', 'label': '标记值', 'type': 'str', 'default': '迪链'},
            {'name': 'tag', 'label': '打上的标记', 'type': 'str', 'default': '迪链'},
        ],
        'apply': rule_dilian_tag,
    },
}


def apply_rule(key, record, params, context):
    spec = RULE_TYPES.get(key)
    if not spec:
        return
    spec['apply'](record, params or {}, context or {})
