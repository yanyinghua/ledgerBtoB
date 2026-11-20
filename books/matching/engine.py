"""户名匹配引擎：把订单户名归并到 4 个目标。

匹配层级（置信度从高到低）：
  1. 人工锁定（is_manual）  —— 永不自动覆盖
  2. 精确（raw_name / normalized_name）
  3. 别名（AccountNameAlias）
  4. 模糊（difflib，阈值可配）

作用域：同名户名在不同商户/账户下可指向不同主体，按 merchant/account_scope 过滤。
"""
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from decimal import Decimal

from django.db.models import Q

from books.models import (
    AccountNameMapping, AccountNameAlias, Counterparty, ReceivingAccount,
    Merchant, SalespersonProfile,
)
from books.imports.utils import normalize_account_name

DEFAULT_THRESHOLD = 0.85


@dataclass
class MatchResult:
    counterparty: object = None
    salesperson: object = None
    department: object = None
    merchant: object = None
    receiving_account: object = None
    source: str = ''
    confidence: object = Decimal('0')
    status: str = '未匹配'
    suggestions: object = field(default_factory=list)


def _mapping_to_result(mapping, source, confidence):
    return MatchResult(
        counterparty=mapping.counterparty,
        salesperson=mapping.salesperson,
        department=mapping.department,
        merchant=mapping.merchant,
        receiving_account=mapping.receiving_account,
        source=source,
        confidence=Decimal(str(confidence)),
        status='自动已匹配',
    )


def _scope_filter(qs, merchant=None, account_scope=None):
    """作用域过滤：映射的 merchant/account 为空表示全局（通配）。"""
    q = Q(merchant__isnull=True)
    if merchant is not None:
        q |= Q(merchant_id=merchant)
    qs = qs.filter(q)
    # receiving_account 作用域：映射账户为空或匹配指定账户
    q2 = Q(receiving_account__isnull=True)
    if account_scope is not None:
        q2 |= Q(receiving_account_id=account_scope)
    return qs.filter(q2)


def resolve_account_name(raw_name, merchant=None, account_scope=None,
                         direction=None, auto_create=True, threshold=DEFAULT_THRESHOLD,
                         bump_usage=True):
    """解析原始户名，返回 MatchResult。"""
    result = MatchResult()
    if not raw_name:
        return result
    norm = normalize_account_name(raw_name)

    # 1. 精确 + 人工锁定优先
    manual = _scope_filter(AccountNameMapping.objects.filter(
        raw_name=raw_name, is_manual=True, is_active=True), merchant, account_scope)
    manual = manual.order_by('merchant_id', 'id').first()
    if manual is None and norm:
        manual = _scope_filter(AccountNameMapping.objects.filter(
            normalized_name=norm, is_manual=True, is_active=True), merchant, account_scope)
        manual = manual.order_by('merchant_id', 'id').first()
    if manual is not None:
        if bump_usage:
            _bump(manual)
        return _mapping_to_result(manual, '人工', 1.0)

    # 2. 精确 / 归一化
    exact = _scope_filter(AccountNameMapping.objects.filter(
        raw_name=raw_name, is_active=True), merchant, account_scope)
    exact = exact.order_by('merchant_id', 'id').first()
    source, score = '精确', 0.95
    if exact is None and norm:
        exact = _scope_filter(AccountNameMapping.objects.filter(
            normalized_name=norm, is_active=True), merchant, account_scope)
        exact = exact.order_by('merchant_id', 'id').first()
        source, score = '归一化', 0.9
    if exact is not None:
        if bump_usage:
            _bump(exact)
        return _mapping_to_result(exact, source, score)

    # 3. 别名
    alias_qs = AccountNameAlias.objects.filter(is_active=True)
    alias = alias_qs.filter(alias=raw_name).first()
    if alias is None and norm:
        alias = alias_qs.filter(alias__in=[raw_name]).first()
    if alias is not None:
        canonical = _scope_filter(AccountNameMapping.objects.filter(
            raw_name=alias.canonical_raw_name, is_active=True), merchant, account_scope).first()
        if canonical is not None:
            if bump_usage:
                _bump(canonical)
            return _mapping_to_result(canonical, '别名', 0.9)

    # 4. 模糊：比对已知主体/账户/商户名
    candidates = []
    for cp in Counterparty.objects.filter(is_active=True):
        candidates.append(('主体', cp, cp.name))
    for acc in ReceivingAccount.objects.filter(is_active=True):
        candidates.append(('账户', acc, acc.account_name))
    for m in Merchant.objects.filter(is_active=True):
        candidates.append(('商户', m, m.name))

    scored = []
    for kind, obj, name in candidates:
        ratio = SequenceMatcher(None, norm, normalize_account_name(name)).ratio()
        if ratio >= threshold:
            scored.append((ratio, kind, obj))
    scored.sort(key=lambda x: -x[0])

    if len(scored) == 1:
        ratio, kind, obj = scored[0]
        result.confidence = Decimal(str(round(ratio, 4)))
        result.source = '模糊'
        result.status = '自动已匹配'
        if kind == '主体':
            result.counterparty = obj
        elif kind == '账户':
            result.receiving_account = obj
        elif kind == '商户':
            result.merchant = obj
        return result
    elif len(scored) > 1:
        result.status = '待复核'
        result.source = '模糊'
        result.confidence = Decimal(str(round(scored[0][0], 4)))
        result.suggestions = [
            {'kind': k, 'id': o.pk, 'name': getattr(o, 'name', getattr(o, 'account_name', '')),
             'score': round(r, 4)} for r, k, o in scored[:5]
        ]
        return result

    # 5. 未匹配：对唯一高相似候选给出建议（即使低于阈值也提示人工确认）
    soft = []
    for kind, obj, name in candidates:
        ratio = SequenceMatcher(None, norm, normalize_account_name(name)).ratio()
        if ratio >= 0.6:
            soft.append((round(ratio, 4), kind, getattr(obj, 'name', getattr(obj, 'account_name', ''))))
    soft.sort(key=lambda x: -x[0])
    result.status = '未匹配'
    result.suggestions = [
        {'kind': k, 'name': n, 'score': s} for s, k, n in soft[:5]
    ]
    return result


def _bump(mapping):
    from django.utils import timezone
    mapping.use_count += 1
    mapping.last_seen_at = timezone.now()
    mapping.save(update_fields=['use_count', 'last_seen_at'])
