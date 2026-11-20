"""初始化：播种算子(积木)清单、规则块(积木)清单、以及各来源类型的默认逻辑规则。
可重复执行，已存在则跳过/更新。
"""
from django.core.management.base import BaseCommand

from books.models import Operator, LogicRuleType, LogicRule
from books.imports.operators import OPERATORS
from books.imports.rules import RULE_TYPES

# 来源类型 -> [(rule_type_key, order, params), ...]
DEFAULT_LOGIC_RULES = {
    'sales_invoice': [
        ('category_default', 0, {'debit_category': '销售', 'credit_category': '销售', 'default_category': '销售'}),
        ('red_detect', 10, {}),
        ('salesperson_map', 30, {'source_field': 'handler', 'require_category': '',
                                 'min_year': 2025, 'source_label': '发票复核人'}),
    ],
    'purchase_invoice': [
        ('category_default', 0, {'debit_category': '采购', 'credit_category': '采购', 'default_category': '采购'}),
        ('red_detect', 10, {}),
    ],
    'bank': [
        ('category_default', 0, {'debit_category': '借方', 'credit_category': '贷方', 'default_category': '其他'}),
    ],
    'wechat': [
        ('category_default', 0, {'debit_category': '支出', 'credit_category': '收入', 'default_category': '其他'}),
        ('direction_swap', 20, {}),
    ],
    'acceptance': [
        ('category_default', 0, {'debit_category': '申请', 'credit_category': '签收', 'default_category': '其他'}),
        # 票交所处理失败/对方拒绝签收/已撤回 的交易并未真实发生，按作废在导入环节过滤
        ('red_detect', 10, {'void_fields': '处理结果', 'void_keywords': '处理失败,拒绝签收,已撤回'}),
        ('acceptance_direction', 20, {}),
        ('dilian_tag', 30, {}),
    ],
    'other': [
        ('category_default', 0, {'debit_category': '借方', 'credit_category': '贷方', 'default_category': '其他'}),
        ('salesperson_map', 30, {'source_field': 'handler', 'require_category': '贷方,收入',
                                 'min_year': 0, 'source_label': '收入流水经手人'}),
    ],
}


class Command(BaseCommand):
    help = '播种算子清单、规则块清单与默认逻辑规则'

    def handle(self, *args, **options):
        # 1) 算子清单
        for key, spec in OPERATORS.items():
            Operator.objects.update_or_create(
                key=key, defaults={'name': spec['name'], 'description': spec.get('description', ''),
                                    'params_schema': spec.get('params_schema', [])})
        self.stdout.write(self.style.SUCCESS(f'算子清单：{len(OPERATORS)} 条'))

        # 2) 规则块清单
        for key, spec in RULE_TYPES.items():
            LogicRuleType.objects.update_or_create(
                key=key, defaults={'name': spec['name'], 'description': spec.get('description', ''),
                                    'params_schema': spec.get('params_schema', [])})
        self.stdout.write(self.style.SUCCESS(f'规则块清单：{len(RULE_TYPES)} 条'))

        # 3) 默认逻辑规则
        rc = 0
        for source_type, rules in DEFAULT_LOGIC_RULES.items():
            for rt_key, order, params in rules:
                rt, _ = LogicRuleType.objects.get_or_create(key=rt_key)
                obj, was = LogicRule.objects.get_or_create(
                    source_type=source_type, rule_type=rt,
                    defaults={'order': order, 'params': params, 'is_active': True})
                if was:
                    obj.order = order
                    obj.params = params
                    obj.is_active = True
                    obj.save(update_fields=['order', 'params', 'is_active'])
                    rc += 1
        self.stdout.write(self.style.SUCCESS(f'默认逻辑规则：{rc} 条（已按最新参数更新）'))
