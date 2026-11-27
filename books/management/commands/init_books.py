"""初始化：预置角色分组（管理员/查询员/业务员）。
列映射与算子/规则不再在此自动播种——由用户按真实表头在后台手工配置
（或运行 init_operators 播种算子/规则，外加本命令的 --seed-winshare-mappings 一键载入样例映射）。

可重复执行，已存在则跳过。
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group

from books.models import ColumnMappingConfig, MappingStep, Operator


# 来源类型 -> [(source_column, target_field, [ (op_key, params), ... ], order), ...]
# 这是依据 /mnt/win_share 实际表头准备好的样例映射，供一键起步；仍属手工确认的便捷入口。
WINSHARE_MAPPINGS = {
    'sales_invoice': [
        ('购方识别号', 'tax_no', [('none', {})], 0),
        ('购买方名称', 'unit_name', [('fullwidth_brackets', {})], 0),
        ('开票日期', 'occur_date', [('to_date', {})], 0),
        ('价税合计', 'debit', [('to_decimal', {})], 0),
        ('开票人', 'handler', [('regex_salesperson', {})], 0),
        ('发票状态', 'category', [('none', {})], 0),
        ('备注', 'remark', [('none', {})], 0),
    ],
    'purchase_invoice': [
        ('销方识别号', 'tax_no', [('none', {})], 0),
        ('销方名称', 'unit_name', [('fullwidth_brackets', {})], 0),
        ('开票日期', 'occur_date', [('to_date', {})], 0),
        ('价税合计', 'credit', [('to_decimal', {})], 0),
        ('发票状态', 'category', [('none', {})], 0),
        ('备注', 'remark', [('none', {})], 0),
    ],
    'bank': [
        ('对方户名', 'unit_name', [('fullwidth_brackets', {})], 0),
        ('交易时间', 'occur_date', [('to_date', {})], 0),
        ('借方发生额', 'debit', [('to_decimal', {})], 0),
        ('贷方发生额', 'credit', [('to_decimal', {})], 0),
        ('摘要', 'remark', [('none', {})], 0),
        ('用途', 'remark', [('none', {})], 0),
    ],
    'other': [
        ('收入说明', 'unit_name', [('fullwidth_brackets', {})], 0),
        ('收入金额', 'credit', [('to_decimal', {})], 0),
        ('日期', 'occur_date', [('to_date', {})], 0),
        ('经办人', 'handler', [('regex_salesperson', {})], 0),
        ('备注', 'remark', [('none', {})], 0),
        ('收入账户', 'remark', [('none', {})], 0),
    ],
}


def _ensure_operator(key):
    """确保算子存在（init_operators 应先执行；此处兜底）。"""
    op, _ = Operator.objects.get_or_create(key=key, defaults={'name': key, 'params_schema': []})
    return op


# B2B 拉卡拉订单列映射（常见别名占位）。
# 格式：(原始列名, 目标字段, [(算子key, 参数), ...], order)
#   - 同一目标字段可配多条 = 别名；extract_fields 按 order 从小到大取第一个非空值。
#   - 金额列用 to_decimal（兼容千分位/货币符号/括号负数），日期列用 to_date。
#   - 真实表头到位后只需在此增删别名/调整 order，不改导入代码。
#   - 注意：不要把「订单金额列」同时映射到 payment_amount，否则 _build_order 会把
#     整单金额当成单笔收款、误判为已全额收付；payment_amount 只映射独立的收付款列。
LAKALA_MAPPINGS = {
    'lakala_order': [
        # 依据真实导出 /home/yanyinghua/下载/订单_11-24_页1.csv（67列）校准。
        # 该表为明细级（一商品一行、订单头重复），导入器按 order_no 合并为 1 单。
        # —— 订单号 / 子单号 ——
        ('订单Id', 'order_no', [('none', {})], 0),
        ('运单ID', 'sub_order_no', [('none', {})], 0),
        # —— 客户归一锚点 ——
        ('客户名称', 'counterparty_raw_name', [('trim', {})], 0),
        ('客户Id', 'platform_customer_id', [('none', {})], 0),
        ('店铺Id', 'platform_shop_id', [('none', {})], 0),
        ('收货人', 'counterparty_raw_name', [('trim', {})], 50),  # 兼容旧导出
        ('客户名称', 'shop_name', [('trim', {})], 0),  # 店铺名称（与订单户名同源）
        # —— 金额 ——
        ('订单总额', 'total_amount', [('to_decimal', {})], 0),
        ('订单实收款', 'paid_amount', [('to_decimal', {})], 0),
        ('商品总额', 'goods_amount', [('to_decimal', {})], 0),
        ('总折扣', 'discount_amount', [('to_decimal', {})], 0),
        ('线上支付-退款额', 'refund_amount', [('sum_columns', {'columns': ['线上支付-退款额', '到货付款-退货额']})], 0),
        ('订单成本', 'cost_amount', [('to_decimal', {})], 0),
        ('实付额', 'paid_amount', [('to_decimal', {})], 20),
        # 订单中心 xlsx（一行一单）复合列别名
        ('总成本', 'cost_amount', [('to_decimal', {})], 10),
        ('退货额', 'refund_amount', [('to_decimal', {})], 10),
        ('客户Id', 'shop_name', [('trim', {})], 10),
        # —— 日期 ——
        ('下单时间', 'order_date', [('to_date', {})], 0),
        ('支付时间', 'payment_date', [('to_date', {})], 0),
        ('完成时间', 'order_status', [('to_date', {})], 30),
        # —— 货主 ——
        ('货主Id', 'platform_owner_id', [('none', {})], 0),
        # —— 支付/状态 ——
        ('支付通道', 'pay_method', [('none', {})], 0),
        ('支付方式', 'payment_status', [('none', {})], 0),
        ('状态', 'order_status', [('none', {})], 0),
        ('支付Id', 'payment_ref', [('none', {})], 0),
        # —— 商品 / 业务员 / 地址 ——
        ('商品名', 'goods_summary', [('none', {})], 0),
        ('销售标题', 'goods_summary', [('none', {})], 10),
        ('片区', 'region', [('none', {})], 0),
        ('下单人', 'salesperson_name', [('none', {})], 10),
        ('收货地址', 'remark', [('none', {})], 0),
        # —— 兼容旧 xlsx 导出（订单中心）：复合列用正则提取 ——
        ('id', 'order_no', [('none', {})], 50),
        ('金额', 'paid_amount', [('regex_extract', {'pattern': '实收[￥¥]([\\d.]+)', 'group': 1}), ('to_decimal', {})], 50),
        ('客户', 'counterparty_raw_name', [('regex_extract', {'pattern': '^([^\\s]+)', 'group': 1})], 60),
        ('客户明细Id', 'platform_customer_id', [('none', {})], 50),
        ('订单流水号', 'payment_ref', [('none', {})], 30),
        ('订单日期', 'order_date', [('to_date', {})], 20),
        ('客户经理', 'salesperson_name', [('none', {})], 20),
    ],
    # API（JSON）来源：常见 camelCase / snake_case 键名，显式列出以免与 Excel 映射互相污染。
    'lakala_order_api': [
        ('orderNo', 'order_no', [('none', {})], 100),
        ('order_no', 'order_no', [('none', {})], 110),
        ('merchantOrderNo', 'order_no', [('none', {})], 120),
        ('subOrderNo', 'sub_order_no', [('none', {})], 100),
        ('shopName', 'counterparty_raw_name', [('trim', {})], 100),
        ('storeName', 'counterparty_raw_name', [('trim', {})], 110),
        ('counterpartyRawName', 'counterparty_raw_name', [('trim', {})], 120),
        ('merchantName', 'merchant_name', [('trim', {})], 100),
        ('totalAmount', 'total_amount', [('to_decimal', {})], 100),
        ('paidAmount', 'paid_amount', [('to_decimal', {})], 100),
        ('paymentAmount', 'payment_amount', [('to_decimal', {})], 100),
        ('orderDate', 'order_date', [('to_date', {})], 100),
        ('transactionTime', 'order_date', [('to_date', {})], 110),
        ('salespersonName', 'salesperson_name', [('trim', {})], 100),
        ('receivingAccountName', 'receiving_account_name', [('trim', {})], 100),
        ('paymentAccountName', 'payment_account_name', [('trim', {})], 100),
        ('orderStatus', 'order_status', [('none', {})], 100),
        ('paymentStatus', 'payment_status', [('none', {})], 100),
        ('paymentDate', 'payment_date', [('to_date', {})], 100),
        ('paymentRef', 'payment_ref', [('none', {})], 100),
        ('goodsSummary', 'goods_summary', [('none', {})], 100),
        ('directionRaw', 'direction_raw', [('none', {})], 100),
    ],
}

# API 来源也适用真实中文表头：把订单映射并入 API 映射（英文键 order=100 兜底）。
LAKALA_MAPPINGS['lakala_order_api'] = (LAKALA_MAPPINGS['lakala_order'] +
                                       LAKALA_MAPPINGS['lakala_order_api'])

# 订单明细行映射（CSV 一商品一行）。detail_ 前缀避免与订单头同名字段冲突。
# 格式：(原始列名, 目标字段, [(算子key, 参数), ...], order)
LAKALA_DETAIL_MAPPINGS = {
    'lakala_order': [
        ('明细Id', 'detail_no', [('none', {})], 0),
        ('明细id', 'detail_no', [('none', {})], 10),  # 订单中心 xlsx
        ('商品Id', 'detail_goods_id', [('none', {})], 0),
        ('规格Id', 'detail_sku_id', [('none', {})], 0),
        ('SKU编码', 'detail_sku', [('none', {})], 0),
        ('规格编码', 'detail_sku', [('none', {})], 10),  # 订单中心 xlsx
        ('条形码', 'detail_barcode', [('none', {})], 0),
        ('条码', 'detail_barcode', [('none', {})], 10),  # 订单中心 xlsx
        ('商品名', 'detail_goods_name', [('none', {})], 0),
        ('销售标题', 'detail_spec', [('none', {})], 0),
        ('销售单位', 'detail_unit', [('none', {})], 0),
        ('数量', 'detail_qty', [('to_decimal', {})], 0),
        ('单价', 'detail_unit_price', [('to_decimal', {})], 0),
        ('小计', 'detail_subtotal', [('to_decimal', {})], 0),
        ('折扣', 'detail_discount', [('to_decimal', {})], 0),
        ('扣后小计', 'detail_discounted_subtotal', [('to_decimal', {})], 0),
        ('实付额', 'detail_paid', [('to_decimal', {})], 0),
        ('退款额', 'detail_refund', [('to_decimal', {})], 0),
        ('退货数量', 'detail_refund_qty', [('to_decimal', {})], 0),
        ('货主Id', 'detail_owner_id', [('none', {})], 0),
    ],
}
LAKALA_DETAIL_MAPPINGS['lakala_order_api'] = LAKALA_DETAIL_MAPPINGS['lakala_order']


# 收款明细占位列映射：真实表头到位后只需在此增删别名/调整 order。
PAYMENT_MAPPINGS = {
    'payment_detail': [
        ('收款流水号', 'payment_ref', [('none', {})], 0),
        ('流水号', 'payment_ref', [('none', {})], 10),
        ('交易流水号', 'payment_ref', [('none', {})], 20),
        ('订单号', 'order_no', [('none', {})], 0),
        ('商户订单号', 'order_no', [('none', {})], 10),
        ('收款方式', 'pay_method', [('none', {})], 0),
        ('支付方式', 'pay_method', [('none', {})], 10),
        ('线上/线下', 'channel_type', [('none', {})], 0),
        ('渠道类型', 'channel_type', [('none', {})], 10),
        ('收款日期', 'payment_date', [('to_date', {})], 0),
        ('交易时间', 'payment_date', [('to_date', {})], 10),
        ('付款人名称', 'payer_name', [('trim', {})], 0),
        ('付款方名称', 'payer_name', [('trim', {})], 10),
        ('付款人账号', 'payer_account', [('none', {})], 0),
        ('付款账号', 'payer_account', [('none', {})], 10),
        ('金额', 'payment_amount', [('to_decimal', {})], 0),
        ('收款金额', 'payment_amount', [('to_decimal', {})], 10),
        ('收款户名', 'receiving_account_name_raw', [('trim', {})], 0),
        ('收款账户', 'receiving_account_name_raw', [('trim', {})], 10),
        ('收款账户名', 'receiving_account_name', [('trim', {})], 0),
        ('货主ID', 'platform_owner_id', [('none', {})], 0),
        ('货主名称', 'owner_name', [('trim', {})], 0),
        ('平台客户ID', 'platform_customer_id', [('none', {})], 0),
        ('平台店铺ID', 'platform_shop_id', [('none', {})], 0),
    ],
}


def seed_lakala_mappings(self, reset=False):
    """幂等播种：新增或更新列映射的 order 与算子步骤，并清理多余步骤。

    同时处理拉卡拉订单与收款明细映射；重跑本命令即可生效，无需手工删旧映射。
    reset=True 时额外删除这些来源下「配置里已不再声明」的映射（用于从占位别名
    切换到真实表头时清理旧别名，避免同 target 多条 order 相同导致取错列）。
    """
    created = updated = 0
    # 同源的订单头映射与明细映射必须合并，不能互相覆盖
    all_mappings = {k: list(v) for k, v in LAKALA_MAPPINGS.items()}
    for source_type, rows in LAKALA_DETAIL_MAPPINGS.items():
        all_mappings.setdefault(source_type, []).extend(rows)
    for source_type, rows in PAYMENT_MAPPINGS.items():
        all_mappings.setdefault(source_type, []).extend(rows)

    if reset:
        wanted = set()
        for source_type, rows in all_mappings.items():
            for source_column, target_field, steps, order in rows:
                wanted.add((source_type, source_column, target_field))
        removed = 0
        for m in ColumnMappingConfig.objects.filter(source_type__in=all_mappings):
            if (m.source_type, m.source_column, m.target_field) not in wanted:
                m.delete()
                removed += 1
        self.stdout.write(self.style.WARNING(f'重置模式：删除旧映射 {removed} 条'))

    for source_type, rows in all_mappings.items():
        if not rows:
            continue
        for source_column, target_field, steps, order in rows:
            mapping, was = ColumnMappingConfig.objects.get_or_create(
                source_type=source_type, source_column=source_column, target_field=target_field,
                defaults={'transform': 'none', 'is_active': True, 'order': order})
            if was:
                created += 1
            else:
                # 已存在则同步优先级/启用状态
                changed = []
                if mapping.order != order:
                    mapping.order = order; changed.append('order')
                if not mapping.is_active:
                    mapping.is_active = True; changed.append('is_active')
                if changed:
                    mapping.save(update_fields=changed)
                    updated += 1
            # 算子步骤：按 (order, op) 对齐，删除不再声明的步骤
            wanted = []
            for i, (op_key, params) in enumerate(steps):
                op = _ensure_operator(op_key)
                step, _ = MappingStep.objects.get_or_create(
                    mapping=mapping, op=op, order=i, defaults={'params': params})
                if step.params != params:
                    step.params = params
                    step.save(update_fields=['params'])
                wanted.append(step.pk)
            mapping.steps.exclude(pk__in=wanted).delete()
    self.stdout.write(self.style.SUCCESS(
        f'拉卡拉占位映射：新增 {created} 条，更新 {updated} 条'))


def seed_winshare_mappings(self):
    created = 0
    for source_type, rows in WINSHARE_MAPPINGS.items():
        for source_column, target_field, steps, order in rows:
            mapping, was = ColumnMappingConfig.objects.get_or_create(
                source_type=source_type, source_column=source_column, target_field=target_field,
                defaults={'transform': 'none', 'is_active': True, 'order': order})
            if was:
                mapping.transform = 'none'
                mapping.is_active = True
                mapping.order = order
                mapping.save(update_fields=['transform', 'is_active', 'order'])
                created += 1
            # 步骤
            for i, (op_key, params) in enumerate(steps):
                op = _ensure_operator(op_key)
                MappingStep.objects.get_or_create(
                    mapping=mapping, op=op, order=i,
                    defaults={'params': params})
    self.stdout.write(self.style.SUCCESS(f'样例映射（win_share）：新增/更新 {created} 条列映射'))


class Command(BaseCommand):
    help = '预置角色分组；可选 --seed-winshare-mappings 一键载入样例列映射'

    def add_arguments(self, parser):
        parser.add_argument('--seed-winshare-mappings', action='store_true',
                            help='载入依据 /mnt/win_share 实表头准备的样例列映射（默认不执行）')
        parser.add_argument('--seed-lakala-mappings', action='store_true',
                            help='载入 B2B 拉卡拉订单/收款明细列映射（默认不执行）')
        parser.add_argument('--reset-lakala-mappings', action='store_true',
                            help='重置模式：先删除这些来源下配置中已不再声明的旧映射，再播种（真实表头校准后用）')

    def handle(self, *args, **options):
        groups = ['管理员', '高层', '部门', '查询员', '业务员', '商户']
        for g in groups:
            grp, was = Group.objects.get_or_create(name=g)
            if was:
                self.stdout.write(self.style.SUCCESS(f'创建分组：{g}'))
            else:
                self.stdout.write(f'分组已存在：{g}')

        # B2B：种子根部门（供组织树起步）
        from books.models import Department
        if not Department.objects.filter(code='ROOT').exists():
            Department.objects.create(name='总部', code='ROOT')
            self.stdout.write(self.style.SUCCESS('创建根部门：总部 (ROOT)'))

        if options.get('seed_winshare_mappings'):
            seed_winshare_mappings(self)
        if options.get('seed_lakala_mappings') or options.get('reset_lakala_mappings'):
            seed_lakala_mappings(self, reset=options.get('reset_lakala_mappings', False))
        if not (options.get('seed_winshare_mappings') or options.get('seed_lakala_mappings')
                or options.get('reset_lakala_mappings')):
            self.stdout.write('（跳过样例映射：列映射请在后台手工维护；'
                              '一键起步可加 --seed-winshare-mappings 或 --seed-lakala-mappings）')
