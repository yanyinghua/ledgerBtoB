"""重建「承兑汇票」来源的列映射与逻辑规则。

背景：此前 acceptance 的 11 条列映射是照着一份并不存在的表样手工建的
（交易对手/申请金额/签收金额/承兑人…），与真实导出文件的列名对不上，
导致 _sheet_usable() 找不到 unit_name+金额列，整表被跳过 → 导入状态「部分」、行数 0。

本迁移按真实表样重建配置，并把迪链文件并入同一来源（用 tag=迪链 区分）：

- 承兑（汇总承兑_合并.xlsx）：交易对手名称 / 交易日期 / 交易金额（元） / 操作类型 / 备注
- 迪链（迪链.xlsx）：单位名称 / 交易时间 / 借方 / 贷方 / 分类 / 业务员 / 类别

金额方向：签收(收款)=贷方，申请(付款)=借方。承兑文件只有一列「交易金额（元）」，
先落借方，再由 acceptance_direction 规则按操作类型搬到贷方。

幂等：先清空 acceptance 的旧配置再按下表重建；pk 与 fixtures/column_mappings.json
保持一致，避免部署时 loaddata 与本迁移互相覆盖产生漂移。
"""
from django.db import migrations

# (pk, source_column, target_field, order, transform, operator_key)
MAPPINGS = [
    (46, '交易对手名称', 'unit_name', 0, 'fullwidth_brackets', 'fullwidth_brackets'),
    (47, '单位名称', 'unit_name', 10, 'fullwidth_brackets', 'fullwidth_brackets'),
    (48, '交易日期', 'occur_date', 0, 'none', 'to_date'),
    (49, '交易时间', 'occur_date', 10, 'none', 'to_date'),
    (50, '票据出票日期', 'occur_date', 20, 'none', 'to_date'),
    (51, '交易金额（元）', 'debit', 0, 'none', 'to_decimal'),
    (52, '借方', 'debit', 10, 'none', 'to_decimal'),
    (53, '贷方', 'credit', 0, 'none', 'to_decimal'),
    (54, '操作类型', 'category', 0, 'none', 'trim'),
    (55, '分类', 'category', 10, 'none', 'trim'),
    (56, '备注', 'remark', 0, 'none', 'none'),
    (205, '业务员', 'handler', 0, 'regex_salesperson', 'regex_salesperson'),
]

# 规则块清单（代码注册表的后台镜像，loaddata 不含这张表，故在此补齐）
RULE_TYPE_SPECS = {
    'acceptance_direction': (
        '承兑借贷方向', '按操作类型把交易金额记入借方(申请)或贷方(签收)；仅承兑行生效。',
        [
            {'name': 'op_field', 'label': '方向判定列', 'type': 'str', 'default': '操作类型'},
            {'name': 'recv_values', 'label': '收款判定值(逗号)', 'type': 'str', 'default': '收'},
            {'name': 'pay_values', 'label': '付款判定值(逗号)', 'type': 'str', 'default': '付,申请'},
        ],
    ),
    'dilian_tag': (
        '迪链标记', '标记列值等于指定值时打 tag（区分迪链与普通承兑）。',
        [
            {'name': 'mark_field', 'label': '标记列', 'type': 'str', 'default': '类别'},
            {'name': 'mark_value', 'label': '标记值', 'type': 'str', 'default': '迪链'},
            {'name': 'tag', 'label': '打上的标记', 'type': 'str', 'default': '迪链'},
        ],
    ),
}

# (rule_type_key, order, params)
LOGIC_RULES = [
    ('category_default', 0, {'debit_category': '申请', 'credit_category': '签收',
                             'default_category': '其他'}),
    # 票交所处理失败 / 对方拒绝签收 / 已撤回 的交易并未真实发生，按作废在导入环节过滤，
    # 否则这些金额会虚增借方。若需保留，在后台「逻辑规则」把本条停用即可。
    ('red_detect', 10, {'void_fields': '处理结果', 'void_keywords': '处理失败,拒绝签收,已撤回'}),
    ('acceptance_direction', 20, {}),
    ('dilian_tag', 30, {}),
]


def seed(apps, schema_editor):
    ColumnMappingConfig = apps.get_model('books', 'ColumnMappingConfig')
    MappingStep = apps.get_model('books', 'MappingStep')
    Operator = apps.get_model('books', 'Operator')
    LogicRule = apps.get_model('books', 'LogicRule')
    LogicRuleType = apps.get_model('books', 'LogicRuleType')

    # 1) 清掉旧的承兑配置（MappingStep 随 ColumnMappingConfig 级联删除）
    ColumnMappingConfig.objects.filter(source_type='acceptance').delete()
    LogicRule.objects.filter(source_type='acceptance').delete()

    # 2) 重建列映射 + 单步算子管道
    for pk, col, target, order, transform, op_key in MAPPINGS:
        cfg = ColumnMappingConfig.objects.create(
            pk=pk, source_type='acceptance', source_column=col, target_field=target,
            transform=transform, is_active=True, order=order)
        op = Operator.objects.filter(key=op_key).first()
        if op:
            MappingStep.objects.create(mapping=cfg, op=op, params={}, order=0)

    # 3) 规则块清单：新规则可能尚未被 init_operators 同步过，这里补齐
    for key, (name, desc, schema) in RULE_TYPE_SPECS.items():
        LogicRuleType.objects.update_or_create(
            key=key, defaults={'name': name, 'description': desc, 'params_schema': schema})

    # 4) 重建承兑的逻辑规则链
    for key, order, params in LOGIC_RULES:
        rt = LogicRuleType.objects.filter(key=key).first()
        if not rt:
            # red_detect / category_default 理论上已由 init_operators 播种；缺失则建占位
            rt = LogicRuleType.objects.create(key=key, name=key, description='', params_schema=[])
        LogicRule.objects.create(source_type='acceptance', rule_type=rt,
                                 params=params, is_active=True, order=order)


def unseed(apps, schema_editor):
    ColumnMappingConfig = apps.get_model('books', 'ColumnMappingConfig')
    LogicRule = apps.get_model('books', 'LogicRule')
    ColumnMappingConfig.objects.filter(source_type='acceptance').delete()
    LogicRule.objects.filter(source_type='acceptance').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0015_alter_columnmappingconfig_order_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
