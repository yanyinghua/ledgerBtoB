"""往来单位归一引擎：基于 0028 的 CustomerAlias / Owner / OwnerAccountMap。

三条归一路径：
  1. 客户归一：alias_type + 原始值 → CustomerEntity
     - 锚点：店铺名 / 平台店铺ID / 付款人名称 / 付款人账号
     - 精确(normalized_value) → 人工锁定优先；未匹配返回 None
  2. 货主归一：platform_owner_id → Owner（精确）；名称可作兜底（模糊）
  3. 货主收款账户：(owner, channel_type, pay_method) → ReceivingAccount
     - 先查 OwnerAccountMap 三元组；再退到 (owner, channel_type, '') 或 (owner, '', pay_method)

只读取字典，不创建主体（避免自动建出垃圾主数据）；未匹配留给复核页人工处理。
计数更新（use_count/last_seen_at）为 best-effort，失败不影响归一结果。
"""
from difflib import SequenceMatcher
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from books.models import (
    CustomerAlias, CustomerEntity, Owner, OwnerAccountMap, ReceivingAccount,
)
from books.imports.utils import normalize_account_name

DEFAULT_THRESHOLD = 0.85


class NormalizeResult:
    """归一结果：obj 为归一到的对象（或 None），source/confidence 供审计。"""
    __slots__ = ('obj', 'source', 'confidence', 'status')

    def __init__(self, obj=None, source='', confidence=0, status='未匹配'):
        self.obj = obj
        self.source = source
        self.confidence = Decimal(str(confidence))
        self.status = status

    def __bool__(self):
        return self.obj is not None


def _bump(alias):
    try:
        alias.use_count += 1
        alias.last_seen_at = timezone.now()
        alias.save(update_fields=['use_count', 'last_seen_at'])
    except Exception:
        pass


def resolve_customer(value, alias_type=None, bump=True, threshold=DEFAULT_THRESHOLD):
    """把店铺名/付款人等锚点归一到 CustomerEntity。

    :param value: 原始值（店名、付款人名称/账号、平台店铺ID）
    :param alias_type: 限定锚点类型（CUSTOMER_ALIAS_TYPE_CHOICES 之一）；None=全部类型
    :return: NormalizeResult(obj=CustomerEntity 或 None)
    """
    if value is None or str(value).strip() == '':
        return NormalizeResult()
    norm = normalize_account_name(value)
    if not norm:
        return NormalizeResult()

    qs = CustomerAlias.objects.filter(is_active=True, normalized_value=norm)
    if alias_type:
        qs = qs.filter(alias_type=alias_type)
    # 人工锁定优先：is_manual 降序，保证同一归一化值下锁定项胜出
    alias = qs.order_by('-is_manual', 'id').first()
    if alias is not None and alias.entity is not None:
        if bump:
            _bump(alias)
        return NormalizeResult(alias.entity, '归一化', 1.0, '自动已匹配')

    # 模糊兜底：同类型内找高相似锚点，仅唯一高分才自动采用
    if norm:
        candidates = CustomerAlias.objects.filter(is_active=True, entity__isnull=False)
        if alias_type:
            candidates = candidates.filter(alias_type=alias_type)
        scored = []
        for a in candidates.only('id', 'normalized_value', 'entity')[:500]:
            ratio = SequenceMatcher(None, norm, a.normalized_value).ratio()
            if ratio >= threshold:
                scored.append((ratio, a))
        scored.sort(key=lambda x: -x[0])
        if len(scored) == 1 and scored[0][0] >= max(threshold, 0.92):
            ratio, a = scored[0]
            if bump:
                _bump(a)
            return NormalizeResult(a.entity, '模糊', round(ratio, 4), '自动已匹配')
        if scored:
            return NormalizeResult(None, '模糊', round(scored[0][0], 4), '待复核')
    return NormalizeResult()


def resolve_owner(platform_owner_id=None, owner_name=None, bump=False):
    """货主归一：平台货主ID精确优先；名称精确兜底。"""
    if platform_owner_id:
        owner = Owner.objects.filter(is_active=True,
                                     platform_owner_id=str(platform_owner_id).strip()).first()
        if owner:
            return NormalizeResult(owner, '精确', 1.0, '自动已匹配')
    if owner_name:
        name = str(owner_name).strip()
        owner = Owner.objects.filter(is_active=True, name=name).first()
        if owner:
            return NormalizeResult(owner, '名称精确', 0.95, '自动已匹配')
    return NormalizeResult()


def resolve_owner_account(owner, channel_type=None, pay_method=None):
    """(货主, 线上/线下, 收款方式) → 收款账户，逐级放宽匹配。"""
    if owner is None:
        return NormalizeResult()
    base = OwnerAccountMap.objects.filter(is_active=True, owner=owner)
    combos = [
        (channel_type, pay_method, '三元组', 1.0),
        (channel_type, '', '渠道', 0.9),
        ('', pay_method, '收款方式', 0.85),
        ('', '', '货主默认', 0.7),
    ]
    for ct, pm, source, score in combos:
        qs = base
        if ct:
            qs = qs.filter(channel_type=ct)
        if pm:
            qs = qs.filter(pay_method=pm)
        m = qs.order_by('-is_default', '-priority', 'id').first()
        if m is not None and m.account is not None:
            return NormalizeResult(m.account, source, score, '自动已匹配')
    return NormalizeResult()


def resolve_receiving_account(account_name=None, account_no=None):
    """收款户名/账号 → ReceivingAccount（精确），供收款明细落库用。"""
    if account_no:
        acc = ReceivingAccount.objects.filter(is_active=True,
                                              account_no=str(account_no).strip()).first()
        if acc:
            return NormalizeResult(acc, '账号精确', 1.0, '自动已匹配')
    if account_name:
        name = str(account_name).strip()
        acc = ReceivingAccount.objects.filter(is_active=True, account_name=name).first()
        if acc:
            return NormalizeResult(acc, '户名精确', 0.95, '自动已匹配')
        norm = normalize_account_name(account_name)
        for a in ReceivingAccount.objects.filter(is_active=True).only('id', 'account_name')[:500]:
            if normalize_account_name(a.account_name) == norm:
                return NormalizeResult(a, '户名归一化', 0.9, '自动已匹配')
    return NormalizeResult()
