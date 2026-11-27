"""往来账管理系统 —— 数据模型"""
import ipaddress

from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

# ---------------------------------------------------------------------------
# 通用选项
# ---------------------------------------------------------------------------
SOURCE_TYPE_CHOICES = [
    ('sales_invoice', '销项发票'),
    ('purchase_invoice', '进项发票'),
    ('bank', '银行流水'),
    ('wechat', '微信'),
    ('acceptance', '承兑汇票'),
    ('other', '其他'),
    ('fund_handler', '资金明细业务员表'),
    # B2B：拉卡拉订单
    ('lakala_order', '拉卡拉订单'),
    ('lakala_order_api', '拉卡拉订单API'),
]

# 业务类型（主表 biz_type）：发票 / 银行 / 承兑 / 微信 / 其他
BIZ_TYPE_CHOICES = [
    ('发票', '发票'),
    ('银行', '银行'),
    ('承兑', '承兑'),
    ('微信', '微信'),
    ('其他', '其他'),
]

# 主表类别：发票来源取「发票状态」列原值（自由文本）；其余来源由规则按借贷方向填。
# 保留为 UI 提示用的可选值，不再作为字段硬约束。
CATEGORY_CHOICES = [
    ('销售', '销售'),
    ('采购', '采购'),
    ('借方', '借方'),
    ('贷方', '贷方'),
    ('申请', '申请'),
    ('签收', '签收'),
    ('转单', '转单'),
    ('收入', '收入'),
    ('支出', '支出'),
    ('其他', '其他'),
    ('正常', '正常'),
    ('红冲', '红冲'),
    ('作废', '作废'),
    ('空白作废', '空白作废'),
    ('已红冲', '已红冲'),
]

# 不参与往来记录与统计的标记：仅「作废」被排除（导入环节已过滤，不入库）。
# 注意：红冲（红字/负数发票）必须参与往来与统计，否则借贷余额、账期、回款等
# 数据统计不完整，故不再排除。
EXCLUDED_TAGS = ['作废']

# 主表标记（可编辑）
TAG_CHOICES = [
    ('', '（无）'),
    ('红冲', '红冲'),
    ('已红冲', '已红冲'),
    ('作废', '作废'),
    ('保证金', '保证金'),
    ('迪链', '迪链'),
    ('个人', '个人'),
]

# 列映射的转换方式
TRANSFORM_CHOICES = [
    ('none', '不转换'),
    ('fullwidth_brackets', '括号转全角'),
    ('regex_salesperson', '正则提取业务员'),
    ('regex_reviewer', '提取复核人后的人名'),
]

# 主表字段（列映射 target_field 可选值）
TARGET_FIELD_CHOICES = [
    ('unit_name', '单位名称'),
    ('occur_date', '发生时间'),
    ('debit', '借方金额'),
    ('credit', '贷方金额'),
    ('category', '类别'),
    ('handler', '经手人/业务员'),
    ('remark', '备注'),
    ('tax_no', '税号'),
    ('invoice_number', '发票号码'),
    # B2B 订单字段
    ('order_no', '订单号'),
    ('sub_order_no', '子订单号'),
    ('direction_raw', '购销方向'),
    ('order_date', '订单日期'),
    ('counterparty_raw_name', '订单户名(原始)'),
    ('total_amount', '订单金额'),
    ('paid_amount', '已收付金额'),
    ('receiving_account_name', '收付款账户名'),
    ('merchant_name', '商户名'),
    ('salesperson_name', '业务员名'),
    ('goods_summary', '货物摘要'),
    ('order_status', '订单状态'),
    ('payment_status', '收付款状态'),
    ('payment_date', '收付款日期'),
    ('payment_amount', '收付款金额'),
    ('payment_ref', '收付款流水号'),
    ('payment_account_name', '收款账户名'),
    # 归一扩展字段（0028）
    ('platform_owner_id', '货主ID'),
    ('platform_customer_id', '平台客户ID'),
    ('platform_shop_id', '平台店铺ID'),
    ('payer_name', '付款人名称'),
    ('payer_account', '付款人账号'),
    ('pay_method', '收款方式'),
    ('channel_type', '线上/线下'),
    ('owner_name', '货主名称'),
    ('receiving_account_name_raw', '收款户名(原始)'),
    # 店铺往来账改造字段
    ('shop_name', '店铺名称'),
    ('goods_amount', '商品金额'),
    ('discount_amount', '折扣'),
    ('refund_amount', '退款'),
    ('cost_amount', '成本金额'),
    ('region', '区域'),
    ('supervisor', '业务主管'),
    # 订单明细行（detail_ 前缀避免与订单头同名字段冲突）
    ('detail_no', '明细Id'),
    ('detail_goods_id', '商品Id'),
    ('detail_sku_id', '规格Id'),
    ('detail_sku', 'SKU编码'),
    ('detail_barcode', '条码'),
    ('detail_goods_name', '商品名'),
    ('detail_spec', '销售标题/规格'),
    ('detail_unit', '销售单位'),
    ('detail_qty', '数量'),
    ('detail_unit_price', '单价'),
    ('detail_subtotal', '小计'),
    ('detail_discount', '折扣'),
    ('detail_discounted_subtotal', '扣后小计'),
    ('detail_paid', '实付额'),
    ('detail_refund', '退款额'),
    ('detail_refund_qty', '退货数量'),
    ('detail_owner_id', '货主Id'),
]

# ---------------------------------------------------------------------------
# B2B 新增枚举
# ---------------------------------------------------------------------------
MERCHANT_TYPE_CHOICES = [
    ('个体户', '个体户'), ('公司', '公司'), ('超市', '超市'),
    ('连锁店', '连锁店'), ('电商店铺', '电商店铺'), ('其他', '其他'),
]
ACCOUNT_TYPE_CHOICES = [('对公', '对公'), ('对私', '对私')]
CHANNEL_CHOICES = [
    ('银行', '银行'), ('微信', '微信'), ('支付宝', '支付宝'),
    ('拉卡拉', '拉卡拉'), ('现金', '现金'), ('其他', '其他'),
]
MATCH_STATUS_CHOICES = [
    ('未匹配', '未匹配'), ('待复核', '待复核'),
    ('自动已匹配', '自动已匹配'), ('已人工确认', '已人工确认'),
]
MATCH_SOURCE_CHOICES = [
    ('精确', '精确'), ('归一化', '归一化'), ('别名', '别名'),
    ('模糊', '模糊'), ('人工', '人工'), ('API', 'API'),
]
# 收款方式（独立于支付载体 channel 与公私户 account_type）
PAY_METHOD_CHOICES = [
    ('平台扫码', '平台扫码'), ('公对公', '公对公'), ('私对公', '私对公'),
    ('微信', '微信'), ('支付宝', '支付宝'), ('现金', '现金'),
]
# 线上/线下渠道类型（不能从收款方式推导：微信既可能线上扫码也可能线下转账）
CHANNEL_TYPE_CHOICES = [('线上', '线上'), ('线下', '线下')]
# 客户归一字典的锚点类型
CUSTOMER_ALIAS_TYPE_CHOICES = [
    ('店铺名', '店铺名'), ('平台店铺ID', '平台店铺ID'),
    ('付款人名称', '付款人名称'), ('付款人账号', '付款人账号'),
]
ACCOUNT_STATUS_CHOICES = [
    ('待授权', '待授权'), ('已授权', '已授权'), ('已禁用', '已禁用'),
]


def _amount():
    return models.DecimalField(max_digits=18, decimal_places=2, default=0)


# ---------------------------------------------------------------------------
# 1. 用户档案（扩展 User）
# ---------------------------------------------------------------------------
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField('手机号', max_length=20, blank=True,
                             help_text='手机验证码登录/改密用；注册时写入')
    salesperson_name = models.CharField('绑定业务员姓名', max_length=50, blank=True,
                                        help_text='业务员角色据此过滤「自己客户」；管理员/查询员留空')
    must_change_password = models.BooleanField(
        '强制改密', default=False,
        help_text='为 True 时，已登录用户除改密/登出外被强制跳转改密页；首次改密成功后自动清除')
    # B2B：账号授权与归属（新用户默认「待授权」，零数据权限，管理员审核后激活）
    account_status = models.CharField(
        '账号状态', max_length=10, choices=ACCOUNT_STATUS_CHOICES, default='已授权', db_index=True,
        help_text='管理员/系统创建的用户默认已授权；手机注册流程显式设为「待授权」，需管理员激活')
    merchant = models.ForeignKey('books.Merchant', null=True, blank=True,
                                 on_delete=models.SET_NULL, verbose_name='绑定商户',
                                 help_text='商户角色用户绑定；商户仅可见本商户订单与往来款')
    salesperson = models.ForeignKey('books.SalespersonProfile', null=True, blank=True,
                                    on_delete=models.SET_NULL, verbose_name='绑定业务员',
                                    help_text='业务员角色用户绑定')
    department = models.ForeignKey('books.Department', null=True, blank=True,
                                   on_delete=models.SET_NULL, verbose_name='绑定部门',
                                   help_text='部门角色用户绑定；可见本部门及子部门数据')
    authorized_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='authorized_users', verbose_name='授权人')
    authorized_at = models.DateTimeField('授权时间', null=True, blank=True)

    class Meta:
        verbose_name = '用户档案'
        verbose_name_plural = '用户档案'

    def __str__(self):
        return f'{self.user.username} 档案'


# ---------------------------------------------------------------------------
# 2. 导入日志
# ---------------------------------------------------------------------------
class SourceUpload(models.Model):
    STATUS_CHOICES = [
        ('success', '成功'), ('partial', '部分成功'),
        ('failed', '失败'), ('empty', '无新增'),
    ]
    MODE_CHOICES = [('full', '全量'), ('incremental', '增量')]
    source_type = models.CharField('来源类型', max_length=20, choices=SOURCE_TYPE_CHOICES)
    file_name = models.CharField('文件名', max_length=255)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name='导入人')
    uploaded_at = models.DateTimeField('导入时间', auto_now_add=True)
    status = models.CharField('状态', max_length=10, choices=STATUS_CHOICES, default='success')
    row_count = models.IntegerField('成功行数', default=0)
    mode = models.CharField('导入方式', max_length=15, choices=MODE_CHOICES, default='full',
                            help_text='全量/增量；全量按文件名去重，增量按行哈希去重')
    note = models.TextField('备注', blank=True)

    class Meta:
        verbose_name = '导入日志'
        verbose_name_plural = '导入日志'
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.get_source_type_display()} - {self.file_name}'


# ---------------------------------------------------------------------------
# 3. 原始行暂存（用于按新映射重跑）
# ---------------------------------------------------------------------------
class RawImportRow(models.Model):
    source_upload = models.ForeignKey(SourceUpload, on_delete=models.CASCADE, related_name='raw_rows',
                                      verbose_name='导入日志')
    data = models.JSONField('原始行数据')
    row_hash = models.CharField('行哈希', max_length=64, blank=True, db_index=True,
                                help_text='原始行数据的 sha256，用于增量去重')

    class Meta:
        verbose_name = '原始导入行'
        verbose_name_plural = '原始导入行'


# ---------------------------------------------------------------------------
# 4. 可配置列映射
# ---------------------------------------------------------------------------
class ColumnMappingConfig(models.Model):
    source_type = models.CharField('来源类型', max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_column = models.CharField(
        '原始表头', max_length=100,
        help_text='Excel 中的列名。不同模板对同一含义可能用不同列名（如"销方名称/供应商名称"），'
                  '都建一条映射即可作为"别名"，不是重复。')
    target_field = models.CharField(
        '映射到主表字段', max_length=30, choices=TARGET_FIELD_CHOICES,
        help_text='多个来源列可映射到同一目标字段（别名）。导入时按 order 从小到大，取第一个非空值。')
    transform = models.CharField('转换方式', max_length=25, choices=TRANSFORM_CHOICES, default='none')
    is_active = models.BooleanField('启用', default=True)
    order = models.IntegerField('优先级', default=0, help_text='同字段多映射时数字小者优先；取第一个非空值')

    class Meta:
        verbose_name = '列映射配置'
        verbose_name_plural = '列映射配置'
        ordering = ['source_type', 'order']

    def __str__(self):
        return f'[{self.get_source_type_display()}] {self.source_column} -> {self.target_field}'


# ---------------------------------------------------------------------------
# 4.1 算子（积木）清单：代码注册表的后台镜像，作为 MappingStep.op 下拉来源
# ---------------------------------------------------------------------------
class Operator(models.Model):
    key = models.CharField('标识', max_length=40, unique=True)
    name = models.CharField('名称', max_length=60)
    description = models.TextField('说明', blank=True)
    params_schema = models.JSONField('参数定义', default=list, blank=True,
                                     help_text='由代码注册表同步，格式：[{name,label,type,default,choices?}]')

    class Meta:
        verbose_name = '算子(积木)'
        verbose_name_plural = '算子(积木)'
        ordering = ['key']

    def __str__(self):
        return f'{self.name} ({self.key})'


# ---------------------------------------------------------------------------
# 4.2 映射步骤：每条列映射可由多个算子串联成管道
# ---------------------------------------------------------------------------
class MappingStep(models.Model):
    mapping = models.ForeignKey(ColumnMappingConfig, on_delete=models.CASCADE,
                                related_name='steps', verbose_name='列映射')
    op = models.ForeignKey(Operator, on_delete=models.PROTECT, verbose_name='算子')
    params = models.JSONField('参数', default=dict, blank=True)
    order = models.IntegerField('顺序', default=0, help_text='同列多步骤按数字从小到大依次执行')

    class Meta:
        verbose_name = '映射步骤'
        verbose_name_plural = '映射步骤'
        ordering = ['order']

    def __str__(self):
        return f'{self.op.key} #{self.order}'


# ---------------------------------------------------------------------------
# 4.3 逻辑规则块（积木）清单：代码注册表的后台镜像，作为 LogicRule.rule_type 来源
# ---------------------------------------------------------------------------
class LogicRuleType(models.Model):
    key = models.CharField('标识', max_length=40, unique=True)
    name = models.CharField('名称', max_length=60)
    description = models.TextField('说明', blank=True)
    params_schema = models.JSONField('参数定义', default=list, blank=True,
                                     help_text='由代码注册表同步，格式：[{name,label,type,default,choices?}]')

    class Meta:
        verbose_name = '规则块(积木)'
        verbose_name_plural = '规则块(积木)'
        ordering = ['key']

    def __str__(self):
        return f'{self.name} ({self.key})'


# ---------------------------------------------------------------------------
# 4.4 逻辑规则：跨字段逻辑装配（按来源类型启用）
# ---------------------------------------------------------------------------
class LogicRule(models.Model):
    source_type = models.CharField('来源类型', max_length=20, choices=SOURCE_TYPE_CHOICES)
    rule_type = models.ForeignKey(LogicRuleType, on_delete=models.PROTECT, verbose_name='规则块')
    params = models.JSONField('参数', default=dict, blank=True)
    is_active = models.BooleanField('启用', default=True)
    order = models.IntegerField('顺序', default=0, help_text='同来源多规则按数字从小到大依次执行')

    class Meta:
        verbose_name = '逻辑规则'
        verbose_name_plural = '逻辑规则'
        ordering = ['source_type', 'order']

    def __str__(self):
        return f'[{self.get_source_type_display()}] {self.rule_type.key}'


# ---------------------------------------------------------------------------
# 5. 主表（核心）
# ---------------------------------------------------------------------------
class MasterRecord(models.Model):
    source_type = models.CharField('来源类型', max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_upload = models.ForeignKey(SourceUpload, on_delete=models.SET_NULL, null=True, blank=True,
                                      verbose_name='导入来源')
    raw_row = models.ForeignKey(RawImportRow, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='master_records', verbose_name='原始发票行',
                                help_text='指向该主表记录对应的原始导入行，用于穿透查看发票全部原始字段')
    unit_name = models.CharField('单位名称', max_length=200, db_index=True)
    biz_type = models.CharField('业务类型', max_length=10, choices=BIZ_TYPE_CHOICES)
    year = models.IntegerField('年份', db_index=True)
    period = models.CharField('期间', max_length=6, help_text='YYYYMM', db_index=True)
    occur_date = models.DateField('发生时间', db_index=True, null=True, blank=True)
    category = models.CharField('类别', max_length=40, blank=True,
                                help_text='发票来源取「发票状态」列原值；其余来源按借贷方向填充')
    debit = _amount()
    credit = _amount()
    tag = models.CharField('标记', max_length=20, choices=TAG_CHOICES, blank=True)
    handler = models.CharField('经手人/业务员', max_length=50, blank=True, db_index=True)
    remark = models.TextField('备注', blank=True)
    tax_no = models.CharField('税号', max_length=30, blank=True)
    invoice_number = models.CharField('发票号码', max_length=40, blank=True)
    package_no = models.CharField('票据包号码', max_length=64, blank=True, db_index=True,
                                  help_text='承兑汇票票据包号码；用于按包号汇总收款/付款/库存余额并穿透查看原始列')
    blue_invoice_no = models.CharField('对应蓝字发票号', max_length=40, blank=True, help_text='红冲备注解析得出')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '主表记录'
        verbose_name_plural = '主表记录'
        ordering = ['occur_date', 'id']

    def __str__(self):
        return f'{self.unit_name} {self.biz_type} 借{self.debit}/贷{self.credit}'


# ---------------------------------------------------------------------------
# 5.1 发票货物明细行（来自「信息汇总表」sheet，按发票号关联到主表记录）
# ---------------------------------------------------------------------------
class InvoiceLineItem(models.Model):
    source_upload = models.ForeignKey(SourceUpload, on_delete=models.CASCADE,
                                      related_name='line_items', verbose_name='导入来源')
    master_record = models.ForeignKey(MasterRecord, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='line_items', verbose_name='主表记录')
    invoice_number = models.CharField('发票号码', max_length=40, db_index=True,
                                      help_text='用于穿透查询；取自行票/数电票号码列（归一后）')
    goods_name = models.CharField('货物或应税劳务名称', max_length=255, blank=True)
    spec = models.CharField('规格型号', max_length=255, blank=True)
    unit = models.CharField('单位', max_length=40, blank=True)
    quantity = models.DecimalField('数量', max_digits=18, decimal_places=4, null=True, blank=True)
    price = models.DecimalField('单价', max_digits=18, decimal_places=4, null=True, blank=True)
    amount = models.DecimalField('金额', max_digits=18, decimal_places=2, null=True, blank=True)
    tax_rate = models.CharField('税率', max_length=20, blank=True)
    tax = models.DecimalField('税额', max_digits=18, decimal_places=2, null=True, blank=True)
    row_index = models.IntegerField('原表行序', default=0, help_text='明细 sheet 内行序，用于排序')

    class Meta:
        verbose_name = '发票货物明细行'
        verbose_name_plural = '发票货物明细行'
        ordering = ['invoice_number', 'row_index']

    def __str__(self):
        return f'{self.invoice_number} | {self.goods_name}'


# ---------------------------------------------------------------------------
# 5.1 资金明细业务员表（手工登记的银行收款经手人，独立存放避免成为主表重复交易）
# ---------------------------------------------------------------------------
class FundReceiptHandler(models.Model):
    source_upload = models.ForeignKey(SourceUpload, on_delete=models.SET_NULL, null=True,
                                      blank=True, verbose_name='导入来源')
    occur_date = models.DateField('发生时间', db_index=True, null=True, blank=True)
    unit_name = models.CharField('单位名称', max_length=200, db_index=True)
    amount = models.DecimalField('金额', max_digits=18, decimal_places=2, default=0)
    handler = models.CharField('经手人/业务员', max_length=50, blank=True, db_index=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '资金明细业务员表'
        verbose_name_plural = '资金明细业务员表'
        ordering = ['occur_date', 'id']

    def __str__(self):
        return f'{self.unit_name} {self.occur_date} {self.amount}'


# ---------------------------------------------------------------------------
# 6. 期初余额
# ---------------------------------------------------------------------------
class OpeningBalance(models.Model):
    unit_name = models.CharField('单位名称', max_length=200, db_index=True)
    biz_type = models.CharField('业务类型', max_length=10, choices=BIZ_TYPE_CHOICES)
    occur_date = models.DateField('发生时间', null=True, blank=True)
    category = models.CharField('类别', max_length=20, choices=CATEGORY_CHOICES, blank=True)
    debit = _amount()
    credit = _amount()
    handler = models.CharField('经手人', max_length=50, blank=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        verbose_name = '期初余额'
        verbose_name_plural = '期初余额'

    def __str__(self):
        return f'{self.unit_name} 借{self.debit}/贷{self.credit}'


# ---------------------------------------------------------------------------
# 6.1 承兑汇票期初库存余额（单一总额，用于收付款统计报表的滚动余额起点）
# ---------------------------------------------------------------------------
class AcceptanceOpeningBalance(models.Model):
    as_of_date = models.DateField('期初日期', help_text='该日期之前的承兑收付款已并入此期初余额')
    amount = _amount()
    remark = models.TextField('备注', blank=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '承兑期初库存余额'
        verbose_name_plural = '承兑期初库存余额'

    def __str__(self):
        return f'承兑期初 {self.as_of_date} {self.amount}'


# ---------------------------------------------------------------------------
# 7. 名称映射库
# ---------------------------------------------------------------------------
class NameMapping(models.Model):
    raw_name = models.CharField('原始名称', max_length=200)
    canonical_name = models.CharField('规范名称', max_length=200, db_index=True)
    tax_no = models.CharField('税号', max_length=30, blank=True)

    class Meta:
        verbose_name = '名称映射库'
        verbose_name_plural = '名称映射库'
        unique_together = [('raw_name', 'tax_no')]

    def __str__(self):
        return f'{self.raw_name} => {self.canonical_name}'


# ---------------------------------------------------------------------------
# 8. 客户-业务员映射
# ---------------------------------------------------------------------------
class CustomerSalesperson(models.Model):
    SOURCE_CHOICES = [
        ('发票复核人', '发票复核人'),
        ('收入流水经手人', '收入流水经手人'),
        ('手动', '手动'),
    ]
    unit_name = models.CharField('单位名称(规范名)', max_length=200, db_index=True)
    salesperson = models.CharField('业务员', max_length=50, db_index=True)
    is_current = models.BooleanField('现任', default=True)
    manual_override = models.BooleanField('手动锁定', default=False,
                                          help_text='勾选后导入/校准不再自动改写该客户的现任业务员')
    source = models.CharField('来源', max_length=20, choices=SOURCE_CHOICES, default='手动')
    effective_date = models.DateField('生效日期', null=True, blank=True)
    credit_term = models.IntegerField('账期天数', default=30, help_text='该客户信用账期，用于判断逾期')

    class Meta:
        verbose_name = '客户-业务员映射'
        verbose_name_plural = '客户-业务员映射'
        unique_together = [('unit_name', 'salesperson')]

    def __str__(self):
        return f'{self.unit_name} -> {self.salesperson}'


# ---------------------------------------------------------------------------
# 9. 业务员变更提醒（导入检测到「派生最新 ≠ 现任」时记录，人工确认后生效）
# ---------------------------------------------------------------------------
class SalespersonChangeReminder(models.Model):
    STATUS = [
        ('待处理', '待处理'),
        ('已采纳', '已采纳'),
        ('已忽略', '已忽略'),
    ]
    unit_name = models.CharField('单位名称', max_length=200, db_index=True)
    current_salesperson = models.CharField('现任业务员', max_length=50, blank=True)
    suggested_salesperson = models.CharField('建议业务员', max_length=50)
    source = models.CharField('来源', max_length=20, choices=CustomerSalesperson.SOURCE_CHOICES, default='手动')
    occur_date = models.DateField('业务日期', null=True, blank=True)
    source_upload = models.ForeignKey('books.SourceUpload', null=True, blank=True,
                                      on_delete=models.SET_NULL, verbose_name='导入来源')
    status = models.CharField('状态', max_length=10, choices=STATUS, default='待处理')
    note = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    resolved_at = models.DateTimeField('处理时间', null=True, blank=True)
    resolved_by = models.CharField('处理人', max_length=50, blank=True)

    class Meta:
        verbose_name = '业务员变更提醒'
        verbose_name_plural = '业务员变更提醒'
        unique_together = [('unit_name', 'suggested_salesperson', 'status')]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.unit_name}: {self.current_salesperson} → {self.suggested_salesperson} ({self.status})'


# ---------------------------------------------------------------------------
# 10. 登录日志（登陆 / 登出 / 登陆失败，由 auth 信号自动记录）
# ---------------------------------------------------------------------------
class LoginLog(models.Model):
    ACTION = [
        ('登录成功', '登录成功'),
        ('登录失败', '登录失败'),
        ('登出', '登出'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, verbose_name='用户')
    username = models.CharField('登录名', max_length=150, db_index=True)
    action = models.CharField('动作', max_length=10, choices=ACTION)
    ip = models.GenericIPAddressField('IP 地址', null=True, blank=True)
    user_agent = models.TextField('User-Agent', blank=True)
    created_at = models.DateTimeField('时间', auto_now_add=True)

    class Meta:
        verbose_name = '登录日志'
        verbose_name_plural = '登录日志'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.username} {self.action} {self.created_at:%Y-%m-%d %H:%M}'


def _client_ip(request):
    """取真实客户端 IP。

    只信任 Nginx 注入的 X-Real-IP（值为 $remote_addr，即 TCP 对端地址，
    客户端无法伪造）。不能用 X-Forwarded-For 的第一段：nginx 配的是
    $proxy_add_x_forwarded_for，会把客户端自带的 XFF 原样保留再追加真实 IP，
    因此攻击者发一个 "X-Forwarded-For: 8.8.8.8" 就能让日志记成 8.8.8.8，
    真实来源被掩盖、溯源失效。
    """
    if not request:
        return None
    # 依次尝试：反代注入的真实 IP -> 直连时的连接地址（本地 runserver、测试 Client）
    for key in ('HTTP_X_REAL_IP', 'REMOTE_ADDR'):
        ip = (request.META.get(key) or '').strip()
        if not ip:
            continue
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue  # 值不合法就试下一个来源
        return ip
    # GenericIPAddressField 存非法值会抛异常，宁可记 NULL 也不能让登录流程崩掉
    return None


@receiver(user_logged_in)
def _log_user_login(sender, request, user, **kwargs):
    LoginLog.objects.create(
        user=user, username=getattr(user, 'username', '') or '',
        action='登录成功', ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '')


@receiver(user_logged_out)
def _log_user_logout(sender, request, user, **kwargs):
    LoginLog.objects.create(
        user=user, username=getattr(user, 'username', '') if user else '',
        action='登出', ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '')


@receiver(user_login_failed)
def _log_user_login_failed(sender, credentials, request, **kwargs):
    LoginLog.objects.create(
        user=None, username=credentials.get('username', '') or '',
        action='登录失败', ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '')


# ---------------------------------------------------------------------------
# 11. 业务操作日志（客户映射编辑、提醒采纳/忽略、校准等，由视图/命令主动记录）
# ---------------------------------------------------------------------------
class OperationLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, verbose_name='操作人')
    username = models.CharField('操作人账号', max_length=150, blank=True, db_index=True)
    action = models.CharField('操作', max_length=50)
    target_type = models.CharField('对象类型', max_length=50, blank=True)
    target_id = models.CharField('对象标识', max_length=100, blank=True)
    detail = models.TextField('详情', blank=True)
    ip = models.GenericIPAddressField('IP 地址', null=True, blank=True)
    created_at = models.DateTimeField('时间', auto_now_add=True)

    class Meta:
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.username} {self.action} {self.target_type} {self.created_at:%Y-%m-%d %H:%M}'


class ReconcileLetter(models.Model):
    """对账函编号：按 年份 各自连续的流水号，公文式展示。

    - 同一 (单位, 年份, 起止月) 只会分配一个编号（出具后稳定不变，刷新/重打一致）。
    - 流水号 seq 在每一年内自增，展示形如「对账函字〔2022〕第0037号」。
    """
    unit_name = models.CharField('客户名称', max_length=255)
    year = models.IntegerField('年份')
    month_start = models.IntegerField('起始月')
    month_end = models.IntegerField('截止月')
    seq = models.IntegerField('年度流水号')
    issued_by = models.CharField('出具人', max_length=150, blank=True, default='')
    created_at = models.DateTimeField('出具时间', auto_now_add=True)

    class Meta:
        verbose_name = '对账函编号'
        verbose_name_plural = '对账函编号'
        # 同一函唯一；seq 在「年份」内连续唯一（防并发重复编号）
        unique_together = [
            ('unit_name', 'year', 'month_start', 'month_end'),
            ('year', 'seq'),
        ]

    @property
    def letter_no(self):
        return f'对账函字〔{self.year}〕第{self.seq:04d}号'

    def __str__(self):
        return self.letter_no


def log_operation(user=None, action='', target_type='', target_id='', detail='',
                  request=None, username=None):
    """记录一条业务操作日志。user 优先取账号名；无 user 时可用 username 显式指定（如系统任务）。"""
    uname = username if username is not None else (
        user.username if (user and getattr(user, 'is_authenticated', False)) else '')
    # 兜底截断：PostgreSQL 会强制校验 varchar 长度，超长直接 DataError 变 500
    # （SQLite 不校验，本地/测试环境看不出来）。
    max_id = OperationLog._meta.get_field('target_id').max_length
    max_action = OperationLog._meta.get_field('action').max_length
    tid = str(target_id) if target_id != '' else ''
    OperationLog.objects.create(
        user=user if (user and getattr(user, 'is_authenticated', False)) else None,
        username=uname, action=action[:max_action], target_type=target_type,
        target_id=tid[:max_id] if max_id else tid,
        detail=detail, ip=_client_ip(request) if request else None)


# ---------------------------------------------------------------------------
# 信号：创建 User 时自动建立 UserProfile
# ---------------------------------------------------------------------------
@receiver(post_save, sender=User)
def _create_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


# ===========================================================================
# B2B 改造：组织层级、商户账户、订单与户名匹配、认证授权
# 新数据围绕 Order 重建；旧 MasterRecord 体系保留但不写入、不进主导航。
# ===========================================================================

class Department(models.Model):
    """部门：可自关联成树，支持 高层→部门→子部门 的逐级汇总。"""
    name = models.CharField('部门名称', max_length=100)
    code = models.CharField('部门编码', max_length=50, unique=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='children', verbose_name='上级部门')
    manager = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='managed_departments', verbose_name='部门负责人')
    is_active = models.BooleanField('启用', default=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        verbose_name = '部门'
        verbose_name_plural = '部门'
        ordering = ['code']
        indexes = [models.Index(fields=['parent']), models.Index(fields=['is_active'])]

    def __str__(self):
        return f'{self.name} ({self.code})'


class SalespersonProfile(models.Model):
    """业务员档案：权威组织归属，替代旧 UserProfile.salesperson_name 自由文本。"""
    name = models.CharField('业务员姓名', max_length=50, unique=True)
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='salesperson_profile', verbose_name='登录账号')
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='salespeople', verbose_name='所属部门')
    phone = models.CharField('手机号', max_length=20, blank=True)
    is_active = models.BooleanField('启用', default=True)

    class Meta:
        verbose_name = '业务员档案'
        verbose_name_plural = '业务员档案'
        ordering = ['name']

    def __str__(self):
        return self.name


class Merchant(models.Model):
    """商户：B2B 销售对象（店铺/超市/公司等），可绑定登录账号并归属业务员/部门。"""
    name = models.CharField('商户名称', max_length=200, db_index=True)
    code = models.CharField('商户编码', max_length=50, unique=True)
    merchant_type = models.CharField('商户类型', max_length=30,
                                     choices=MERCHANT_TYPE_CHOICES, default='其他')
    login_user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='merchant_profile', verbose_name='登录账号')
    salesperson = models.ForeignKey(SalespersonProfile, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='managed_merchants',
                                    verbose_name='归属业务员')
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='merchants', verbose_name='所属部门')
    parent_merchant = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='sub_merchants', verbose_name='上级商户')
    is_active = models.BooleanField('启用', default=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        verbose_name = '商户'
        verbose_name_plural = '商户'
        ordering = ['code']

    def __str__(self):
        return f'{self.name} ({self.code})'


class ReceivingAccount(models.Model):
    """收付款账户：公私账户多，按账户核算。"""
    account_name = models.CharField('账户名称', max_length=200, db_index=True)
    account_no = models.CharField('账号', max_length=100, blank=True, db_index=True)
    channel = models.CharField('渠道', max_length=30, choices=CHANNEL_CHOICES, default='其他')
    channel_type = models.CharField('线上/线下', max_length=10, choices=CHANNEL_TYPE_CHOICES,
                                    blank=True, help_text='账户默认渠道属性，可空（一个账户可能两者都用）')
    account_type = models.CharField('账户类型', max_length=10,
                                    choices=ACCOUNT_TYPE_CHOICES, default='对公')
    account_holder = models.CharField('收款户名', max_length=200, blank=True,
                                      help_text='账户名与实际收款户名可能不同')
    owner_merchant = models.ForeignKey(Merchant, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='accounts', verbose_name='归属商户')
    is_active = models.BooleanField('启用', default=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        verbose_name = '收付款账户'
        verbose_name_plural = '收付款账户'
        ordering = ['account_name']

    def __str__(self):
        return f'{self.account_name}' + (f' ({self.account_no})' if self.account_no else '')


class Counterparty(models.Model):
    """往来主体：户名匹配归并后的客户/供应商。"""
    name = models.CharField('主体名称', max_length=200, db_index=True)
    party_type = models.CharField('主体类型', max_length=10,
                                  choices=[('客户', '客户'), ('供应商', '供应商')])
    tax_no = models.CharField('税号', max_length=30, blank=True, db_index=True)
    is_active = models.BooleanField('启用', default=True)

    class Meta:
        verbose_name = '往来主体'
        verbose_name_plural = '往来主体'
        ordering = ['name']
        unique_together = [('name', 'party_type')]

    def __str__(self):
        return f'{self.name} [{self.party_type}]'


class Order(models.Model):
    """B2B 核心订单：一行=一订单，强制 4 目标匹配，承载购销与收付款。"""
    source_type = models.CharField('来源类型', max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_upload = models.ForeignKey(SourceUpload, null=True, blank=True, on_delete=models.SET_NULL,
                                      verbose_name='导入来源')
    raw_row = models.ForeignKey(RawImportRow, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='orders', verbose_name='原始导入行')
    external_order_no = models.CharField('外部订单号', max_length=100, db_index=True)
    sub_order_no = models.CharField('子订单号', max_length=100, blank=True)
    direction = models.CharField('购销方向', max_length=10,
                                 choices=[('销售', '销售'), ('采购', '采购')], db_index=True)
    order_date = models.DateField('订单日期', null=True, blank=True, db_index=True)
    counterparty_raw_name = models.CharField('订单户名(原始)', max_length=200, db_index=True)
    # 归一扩展（0028）：平台原始 ID + 归一后的主体/货主
    platform_owner_id = models.CharField('货主ID(原始)', max_length=100, blank=True, db_index=True)
    platform_customer_id = models.CharField('平台客户ID(原始)', max_length=100, blank=True, db_index=True)
    owner = models.ForeignKey('Owner', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='orders', verbose_name='货主')
    customer_entity = models.ForeignKey('CustomerEntity', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='orders', verbose_name='客户主体')
    # 4 目标匹配结果
    counterparty = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='orders', verbose_name='往来主体')
    receiving_account = models.ForeignKey(ReceivingAccount, null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name='orders',
                                          verbose_name='收付款账户')
    merchant = models.ForeignKey(Merchant, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='orders', verbose_name='商户')
    salesperson = models.ForeignKey(SalespersonProfile, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='orders',
                                    verbose_name='业务员')
    salesperson_raw = models.CharField('业务员(原始)', max_length=50, blank=True, db_index=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='orders', verbose_name='部门')
    currency = models.CharField('币种', max_length=10, default='CNY')
    total_amount = models.DecimalField('订单金额', max_digits=18, decimal_places=2, default=0)
    paid_amount = models.DecimalField('已收付金额', max_digits=18, decimal_places=2, default=0)
    goods_summary = models.TextField('货物摘要', blank=True)
    order_status = models.CharField('订单状态', max_length=30, blank=True, db_index=True)
    payment_status = models.CharField('收付款状态', max_length=30, blank=True)
    match_status = models.CharField('匹配状态', max_length=20, choices=MATCH_STATUS_CHOICES,
                                    default='未匹配', db_index=True)
    match_confidence = models.DecimalField('匹配置信度', max_digits=5, decimal_places=4, default=0)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    # 店铺往来账改造：店铺与经营口径字段
    shop_name = models.CharField('店铺名称', max_length=200, blank=True, db_index=True)
    goods_amount = models.DecimalField('商品金额', max_digits=18, decimal_places=2, default=0)
    discount_amount = models.DecimalField('折扣', max_digits=18, decimal_places=2, default=0)
    refund_amount = models.DecimalField('退款', max_digits=18, decimal_places=2, default=0)
    cost_amount = models.DecimalField('成本金额', max_digits=18, decimal_places=2, default=0)
    round_off = models.DecimalField('抹零', max_digits=18, decimal_places=2, default=0)
    supervisor = models.CharField('业务主管', max_length=50, blank=True, db_index=True)
    region = models.CharField('区域', max_length=100, blank=True, db_index=True)

    class Meta:
        verbose_name = '订单'
        verbose_name_plural = '订单'
        ordering = ['-order_date', '-id']
        unique_together = [('source_type', 'external_order_no')]
        indexes = [
            models.Index(fields=['order_date', 'direction']),
            models.Index(fields=['merchant', 'order_date']),
            models.Index(fields=['department', 'order_date']),
            models.Index(fields=['match_status']),
            models.Index(fields=['shop_name', 'order_date']),
            models.Index(fields=['region', 'order_date']),
        ]

    @property
    def unpaid_amount(self):
        return self.total_amount - self.paid_amount

    @property
    def net_amount(self):
        """金额口径：订单总额 - 退款 - 抹零。"""
        return self.total_amount - self.refund_amount - self.round_off

    @property
    def gross_profit(self):
        return self.net_amount - self.cost_amount

    @property
    def gross_margin(self):
        return float(self.gross_profit / self.net_amount) if self.net_amount > 0 else 0

    @property
    def gross_margin_pct(self):
        return round(self.gross_margin * 100, 2)

    def __str__(self):
        return f'{self.external_order_no} {self.counterparty_raw_name} {self.direction} {self.total_amount}'


class OrderPayment(models.Model):
    """订单收付款明细：无独立收付款模板时，从订单内收款列或 API 逐笔落库。"""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments',
                              verbose_name='订单')
    pay_date = models.DateField('收付款日期', null=True, blank=True, db_index=True)
    amount = models.DecimalField('金额', max_digits=18, decimal_places=2, default=0)
    account = models.ForeignKey(ReceivingAccount, null=True, blank=True, on_delete=models.SET_NULL,
                                verbose_name='收付款账户')
    channel = models.CharField('渠道', max_length=30, blank=True)
    direction = models.CharField('方向', max_length=10, blank=True,
                                 help_text='收款/付款')
    external_ref = models.CharField('外部流水号', max_length=100, blank=True, db_index=True)
    raw_data = models.JSONField('原始数据', default=dict, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '订单收付款'
        verbose_name_plural = '订单收付款'
        ordering = ['pay_date', 'id']

    def __str__(self):
        return f'{self.order.external_order_no} {self.pay_date} {self.amount}'


class OrderDetail(models.Model):
    """订单明细行：CSV 一商品一行，按订单号归并后随订单落库，用于按订单id穿透。"""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='details',
                              verbose_name='订单')
    detail_no = models.CharField('明细Id', max_length=100, blank=True, db_index=True)
    goods_id = models.CharField('商品Id', max_length=100, blank=True)
    sku_id = models.CharField('规格Id', max_length=100, blank=True)
    sku = models.CharField('SKU编码', max_length=100, blank=True)
    barcode = models.CharField('条码', max_length=100, blank=True)
    goods_name = models.CharField('商品名', max_length=255, blank=True)
    spec = models.CharField('销售标题/规格', max_length=255, blank=True)
    unit = models.CharField('销售单位', max_length=20, blank=True)
    qty = models.DecimalField('数量', max_digits=18, decimal_places=4, default=0)
    unit_price = models.DecimalField('单价', max_digits=18, decimal_places=4, default=0)
    subtotal = models.DecimalField('小计', max_digits=18, decimal_places=2, default=0)
    discount = models.DecimalField('折扣', max_digits=18, decimal_places=2, default=0)
    discounted_subtotal = models.DecimalField('扣后小计', max_digits=18, decimal_places=2, default=0)
    paid_amount = models.DecimalField('实付额', max_digits=18, decimal_places=2, default=0)
    refund_amount = models.DecimalField('退款额', max_digits=18, decimal_places=2, default=0)
    refund_qty = models.DecimalField('退货数量', max_digits=18, decimal_places=4, default=0)
    owner_id = models.CharField('货主Id(原始)', max_length=100, blank=True)
    row_index = models.IntegerField('原表行序', default=0)
    raw_data = models.JSONField('原始数据', default=dict, blank=True)

    class Meta:
        verbose_name = '订单明细行'
        verbose_name_plural = '订单明细行'
        ordering = ['order_id', 'row_index']
        indexes = [models.Index(fields=['order', 'row_index'])]

    def __str__(self):
        return f'{self.order.external_order_no} #{self.row_index} {self.goods_name}'


class AccountNameMapping(models.Model):
    """户名匹配规则：原始户名 → 4 目标；is_manual 锁定后自动流程永不覆盖。"""
    raw_name = models.CharField('原始户名', max_length=200, db_index=True)
    normalized_name = models.CharField('归一化户名', max_length=200, db_index=True)
    counterparty = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.SET_NULL,
                                     verbose_name='往来主体')
    salesperson = models.ForeignKey(SalespersonProfile, null=True, blank=True, on_delete=models.SET_NULL,
                                    verbose_name='业务员')
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL,
                                   verbose_name='部门')
    merchant = models.ForeignKey(Merchant, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name='商户')
    receiving_account = models.ForeignKey(ReceivingAccount, null=True, blank=True, on_delete=models.SET_NULL,
                                          verbose_name='收付款账户')
    match_source = models.CharField('匹配来源', max_length=20, choices=MATCH_SOURCE_CHOICES, default='人工')
    confidence = models.DecimalField('置信度', max_digits=5, decimal_places=4, default=0)
    is_manual = models.BooleanField('人工锁定', default=False,
                                    help_text='勾选后自动匹配永不覆盖该映射')
    is_active = models.BooleanField('启用', default=True)
    use_count = models.IntegerField('使用次数', default=0)
    last_seen_at = models.DateTimeField('最近出现', null=True, blank=True)
    note = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '户名匹配映射'
        verbose_name_plural = '户名匹配映射'
        ordering = ['raw_name']
        constraints = [
            models.UniqueConstraint(fields=['raw_name', 'merchant'], name='uniq_rawname_merchant'),
            # PG 视 NULL 为不同值，需条件唯一约束保证「全局映射」(merchant 为空) 唯一
            models.UniqueConstraint(fields=['raw_name'], condition=models.Q(merchant__isnull=True),
                                    name='uniq_rawname_global'),
        ]

    def __str__(self):
        return f'{self.raw_name} → {self.counterparty or self.merchant or "?"}'


class AccountNameAlias(models.Model):
    """户名别名：把店铺名/超市名/账户名等不同写法指向规范原始户名。"""
    alias = models.CharField('别名', max_length=200, db_index=True)
    canonical_raw_name = models.CharField('规范原始户名', max_length=200, db_index=True,
                                          help_text='指向 AccountNameMapping.raw_name')
    source = models.CharField('来源', max_length=20, blank=True)
    is_active = models.BooleanField('启用', default=True)

    class Meta:
        verbose_name = '户名别名'
        verbose_name_plural = '户名别名'
        ordering = ['alias']
        unique_together = [('alias', 'canonical_raw_name')]

    def __str__(self):
        return f'{self.alias} → {self.canonical_raw_name}'


class InvitedPhone(models.Model):
    """邀请白名单：仅白名单内手机号可注册，防止陌生人灌水。"""
    phone = models.CharField('手机号', max_length=20, unique=True)
    role_hint = models.CharField('建议角色', max_length=20, blank=True,
                                 help_text='授权时建议分配的角色组')
    note = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '邀请白名单'
        verbose_name_plural = '邀请白名单'
        ordering = ['-created_at']

    def __str__(self):
        return self.phone


class SmsCode(models.Model):
    """短信验证码：注册/登录/改密用，5 分钟有效，用后置废。"""
    PURPOSE_CHOICES = [
        ('注册', '注册'), ('登录', '登录'), ('重置', '重置'),
    ]
    phone = models.CharField('手机号', max_length=20, db_index=True)
    code = models.CharField('验证码', max_length=6)
    purpose = models.CharField('用途', max_length=10, choices=PURPOSE_CHOICES)
    expires_at = models.DateTimeField('过期时间')
    is_used = models.BooleanField('已使用', default=False)
    sent_at = models.DateTimeField('发送时间', auto_now_add=True)
    ip = models.GenericIPAddressField('IP', null=True, blank=True)

    class Meta:
        verbose_name = '短信验证码'
        verbose_name_plural = '短信验证码'
        ordering = ['-sent_at']
        indexes = [models.Index(fields=['phone', 'purpose', 'sent_at'])]

    def __str__(self):
        return f'{self.phone} {self.purpose} {self.code}'


# ===========================================================================
# 往来单位归一（0028）：客户主体↔店铺/付款人，货主↔收款账户，独立收款明细
# 与旧 Counterparty / Merchant / AccountNameMapping 并存，互不干扰。
# ===========================================================================
class CustomerEntity(models.Model):
    """客户主体：多个店铺/付款人归一到同一税号主体。"""
    name = models.CharField('主体名称', max_length=200, db_index=True)
    tax_no = models.CharField('税号(主体ID)', max_length=30, blank=True, db_index=True)
    platform_customer_id = models.CharField('平台客户ID', max_length=100, blank=True, db_index=True)
    party_type = models.CharField('主体类型', max_length=10,
                                  choices=[('客户', '客户'), ('供应商', '供应商')], default='客户')
    credit_term = models.PositiveIntegerField('账期天数', default=30)
    is_active = models.BooleanField('启用', default=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '客户主体'
        verbose_name_plural = '客户主体'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['tax_no'], condition=models.Q(tax_no__gt=''),
                                    name='uniq_customer_entity_taxno'),
            models.UniqueConstraint(fields=['platform_customer_id'],
                                    condition=models.Q(platform_customer_id__gt=''),
                                    name='uniq_customer_entity_platform_id'),
        ]

    def __str__(self):
        return f'{self.name}' + (f' [{self.tax_no}]' if self.tax_no else '')


class CustomerAlias(models.Model):
    """客户归一字典：店铺名/平台店铺ID/付款人名称/付款人账号 → 客户主体。"""
    alias_type = models.CharField('锚点类型', max_length=20, choices=CUSTOMER_ALIAS_TYPE_CHOICES)
    alias_value = models.CharField('原始值', max_length=200)
    normalized_value = models.CharField('归一化值', max_length=200, db_index=True)
    entity = models.ForeignKey(CustomerEntity, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='aliases', verbose_name='客户主体')
    platform_shop_id = models.CharField('平台店铺ID', max_length=100, blank=True)
    is_manual = models.BooleanField('人工锁定', default=False,
                                    help_text='勾选后自动匹配永不覆盖')
    is_active = models.BooleanField('启用', default=True)
    use_count = models.IntegerField('使用次数', default=0)
    last_seen_at = models.DateTimeField('最近出现', null=True, blank=True)
    note = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '客户归一字典'
        verbose_name_plural = '客户归一字典'
        ordering = ['alias_type', 'normalized_value']
        constraints = [
            models.UniqueConstraint(fields=['alias_type', 'normalized_value'],
                                    name='uniq_customer_alias_type_value'),
        ]
        indexes = [models.Index(fields=['normalized_value', 'alias_type'])]

    def __str__(self):
        return f'{self.alias_type}:{self.alias_value} → {self.entity or "?"}'


class Owner(models.Model):
    """货主：供货方/卖家，决定收款账户；货主ID为平台唯一标识。"""
    platform_owner_id = models.CharField('货主ID', max_length=100, db_index=True)
    name = models.CharField('货主名称', max_length=200, db_index=True)
    tax_no = models.CharField('税号', max_length=30, blank=True, db_index=True)
    is_active = models.BooleanField('启用', default=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '货主'
        verbose_name_plural = '货主'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['platform_owner_id'],
                                    condition=models.Q(platform_owner_id__gt=''),
                                    name='uniq_owner_platform_id'),
        ]

    def __str__(self):
        return f'{self.name} [{self.platform_owner_id}]'


class OwnerAccountMap(models.Model):
    """货主-收款账户归一字典：(货主, 线上/线下, 收款方式) → 收款账户。"""
    owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='account_maps',
                              verbose_name='货主')
    account = models.ForeignKey(ReceivingAccount, on_delete=models.CASCADE,
                                related_name='owner_maps', verbose_name='收款账户')
    channel_type = models.CharField('线上/线下', max_length=10, choices=CHANNEL_TYPE_CHOICES)
    pay_method = models.CharField('收款方式', max_length=20, choices=PAY_METHOD_CHOICES)
    priority = models.IntegerField('优先级', default=0)
    is_default = models.BooleanField('默认账户', default=False)
    is_active = models.BooleanField('启用', default=True)
    note = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '货主收款账户字典'
        verbose_name_plural = '货主收款账户字典'
        ordering = ['owner', 'channel_type', 'pay_method', '-priority']
        constraints = [
            models.UniqueConstraint(fields=['owner', 'channel_type', 'pay_method', 'account'],
                                    name='uniq_owner_channel_method_account'),
        ]

    def __str__(self):
        return f'{self.owner} {self.channel_type} {self.pay_method} → {self.account}'


class PaymentReceipt(models.Model):
    """收款明细：独立导入（Excel/API），含付款人/收款账户/货主归一结果。"""
    source_type = models.CharField('来源类型', max_length=20, default='payment_detail')
    source_upload = models.ForeignKey(SourceUpload, null=True, blank=True, on_delete=models.SET_NULL,
                                      verbose_name='导入来源')
    raw_row = models.ForeignKey(RawImportRow, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='payment_receipts', verbose_name='原始行')
    external_ref = models.CharField('外部流水号', max_length=100, db_index=True)
    channel_type = models.CharField('线上/线下', max_length=10, choices=CHANNEL_TYPE_CHOICES)
    pay_method = models.CharField('收款方式', max_length=20, choices=PAY_METHOD_CHOICES)
    pay_date = models.DateField('收款日期', null=True, blank=True, db_index=True)
    payer_name = models.CharField('付款人名称', max_length=200, blank=True, db_index=True)
    payer_account = models.CharField('付款人账号', max_length=100, blank=True, db_index=True)
    amount = models.DecimalField('金额', max_digits=18, decimal_places=2, default=0)
    receiving_account_name = models.CharField('收款户名(原始)', max_length=200, blank=True)
    receiving_account = models.ForeignKey(ReceivingAccount, null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name='payment_receipts',
                                          verbose_name='收款账户')
    owner = models.ForeignKey(Owner, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='payment_receipts', verbose_name='货主')
    entity = models.ForeignKey(CustomerEntity, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='payment_receipts', verbose_name='客户主体')
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='payment_receipts', verbose_name='关联订单')
    match_status = models.CharField('匹配状态', max_length=20, choices=MATCH_STATUS_CHOICES,
                                    default='未匹配', db_index=True)
    note = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    # 回款表管理：店铺名称与编辑留痕
    shop_name = models.CharField('店铺名称', max_length=200, blank=True, db_index=True)
    edited_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='edited_receipts', verbose_name='编辑人')
    edited_at = models.DateTimeField('编辑时间', null=True, blank=True)

    class Meta:
        verbose_name = '收款明细'
        verbose_name_plural = '收款明细'
        ordering = ['-pay_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['source_type', 'external_ref'],
                                    name='uniq_payment_receipt_source_ref'),
        ]
        indexes = [
            models.Index(fields=['owner', 'pay_date']),
            models.Index(fields=['entity', 'pay_date']),
            models.Index(fields=['match_status']),
            models.Index(fields=['shop_name', 'pay_date']),
        ]

    @property
    def account_type(self):
        return self.receiving_account.account_type if self.receiving_account_id else '—'

    def __str__(self):
        return f'{self.pay_date} {self.payer_name} {self.amount}'


# ===========================================================================
# 店铺往来账：字典 / 催款 / 对账单
# ===========================================================================
class ShopBusinessRegistry(models.Model):
    """店铺名与工商登记名称、统一社会信用代码字典。"""
    shop_name = models.CharField('店铺名称', max_length=200, unique=True)
    registered_name = models.CharField('工商登记名称', max_length=300, blank=True)
    credit_code = models.CharField('统一社会信用代码', max_length=30, blank=True, db_index=True)
    entity = models.ForeignKey(CustomerEntity, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='shop_registries', verbose_name='客户主体')
    source = models.CharField('来源', max_length=20, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='created_registries', verbose_name='创建人')
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='updated_registries', verbose_name='更新人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '店铺工商字典'
        verbose_name_plural = '店铺工商字典'
        ordering = ['shop_name']
        constraints = [
            models.UniqueConstraint(fields=['credit_code'], condition=models.Q(credit_code__gt=''),
                                    name='uniq_shop_credit_code'),
        ]

    def __str__(self):
        return f'{self.shop_name} [{self.registered_name or self.credit_code}]'


class CollectionReminder(models.Model):
    """业务员往来催款记录。"""
    shop_name = models.CharField('店铺名称', max_length=200, blank=True)
    entity = models.ForeignKey(CustomerEntity, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='reminders', verbose_name='客户主体')
    salesperson = models.ForeignKey(SalespersonProfile, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='reminders',
                                    verbose_name='业务员')
    amount = models.DecimalField('催款金额', max_digits=18, decimal_places=2, default=0)
    due_date = models.DateField('到期日', null=True, blank=True)
    remind_date = models.DateField('催款日期', null=True, blank=True)
    method = models.CharField('催款方式', max_length=20, blank=True)
    content = models.TextField('催款内容', blank=True)
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name='登记人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '催款记录'
        verbose_name_plural = '催款记录'
        ordering = ['-remind_date', '-id']

    def __str__(self):
        return f'{self.shop_name} {self.remind_date} {self.amount}'


class ShopStatement(models.Model):
    """店铺对账单：期间与汇总落库，明细按期间实时计算。"""
    letter_no = models.CharField('对账单号', max_length=40, unique=True)
    shop_name = models.CharField('店铺名称', max_length=200, blank=True)
    entity = models.ForeignKey(CustomerEntity, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='statements', verbose_name='客户主体')
    period_from = models.DateField('起始日期')
    period_to = models.DateField('截止日期')
    amount = models.DecimalField('对账金额', max_digits=18, decimal_places=2, default=0)
    status = models.CharField('状态', max_length=20, default='待发放')
    issued_at = models.DateTimeField('发放时间', null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   verbose_name='创建人')
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '对账单'
        verbose_name_plural = '对账单'
        ordering = ['-period_to', '-id']

    def __str__(self):
        return f'{self.letter_no} {self.shop_name} {self.period_from}~{self.period_to}'
