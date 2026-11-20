"""角色权限与行级数据过滤工具。"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin

ROLE_ADMIN = '管理员'
ROLE_EXECUTIVE = '高层'
ROLE_DEPARTMENT = '部门'
ROLE_QUERY = '查询员'
ROLE_SALESPERSON = '业务员'
ROLE_MERCHANT = '商户'


class AdminRequiredMixin(AccessMixin):
    """仅管理员可访问的视图混入（用于写操作）。"""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if get_user_role(request.user) != ROLE_ADMIN:
            messages.error(request, '无权限，需要管理员角色。')
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)


def _user_roles(user):
    if not user or not user.is_authenticated:
        return set()
    return set(user.groups.values_list('name', flat=True))


def require_roles(*roles):
    """仅允许指定角色（分组名）访问；未登录跳登录页。"""
    def decorator(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            # 超级用户视为拥有全部角色
            if getattr(request.user, 'is_superuser', False):
                return view(request, *args, **kwargs)
            if roles and not (_user_roles(request.user) & set(roles)):
                messages.error(request, '无权限访问该页面。')
                return redirect('/')
            return view(request, *args, **kwargs)
        return _wrapped
    return decorator


def require_admin(view):
    """仅管理员可访问（用于导入、删除、重跑等写操作）。"""
    return require_roles(ROLE_ADMIN)(view)


def get_user_role(user):
    # 超级用户未加入任何分组时，按管理员处理（createsuperuser 不会自动入组）
    if user and user.is_authenticated and getattr(user, 'is_superuser', False):
        return ROLE_ADMIN
    roles = _user_roles(user)
    for r in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_DEPARTMENT, ROLE_QUERY, ROLE_SALESPERSON, ROLE_MERCHANT):
        if r in roles:
            return r
    return ''


def visible_unit_names(user):
    """返回该用户可查看的单位名称集合（业务员为本人客户，其余为 None 表示全部）。"""
    role = get_user_role(user)
    if role == ROLE_SALESPERSON:
        from books.models import UserProfile, CustomerSalesperson
        name = getattr(user.profile, 'salesperson_name', '') if hasattr(user, 'profile') else ''
        if not name:
            return set()  # 未绑定业务员，看不到任何客户
        return set(CustomerSalesperson.objects.filter(salesperson=name)
                   .values_list('unit_name', flat=True))
    if role in (ROLE_ADMIN, ROLE_QUERY):
        return None  # None 表示不限制
    return set()


def get_visible_queryset(user, qs):
    """按角色过滤主表查询集（行级权限）。"""
    units = visible_unit_names(user)
    if units is None:
        return qs
    if not units:
        return qs.none()
    return qs.filter(unit_name__in=units)


# ===========================================================================
# B2B 层级数据可见性：商户→业务员→部门→高层/管理员
# 仅对「已授权」账号返回数据；否则一律空集（兜底防越权）。
# ===========================================================================

def _is_authorized(user):
    profile = getattr(user, 'profile', None) if user and user.is_authenticated else None
    return profile is not None and getattr(profile, 'account_status', '') == '已授权'


def _department_subtree_ids(root_id):
    """返回部门及其所有后代部门 id（迭代避免深递归）。"""
    from books.models import Department
    if not root_id:
        return set()
    ids = {root_id}
    frontier = [root_id]
    while frontier:
        children = list(Department.objects.filter(parent_id__in=frontier).values_list('id', flat=True))
        new = [c for c in children if c not in ids]
        ids.update(new)
        frontier = new
    return ids


def visible_department_ids(user):
    """可见部门 id 集合；None 表示全部。"""
    if not _is_authorized(user):
        return set()
    role = get_user_role(user)
    if role in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_QUERY):
        return None
    profile = user.profile
    if role == ROLE_DEPARTMENT:
        return _department_subtree_ids(profile.department_id)
    # 业务员/商户按各自归属过滤（不走部门维度）
    return None


def visible_salesperson_ids(user):
    """可见业务员 id 集合；None 表示全部。"""
    if not _is_authorized(user):
        return set()
    role = get_user_role(user)
    if role in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_QUERY):
        return None
    if role == ROLE_DEPARTMENT:
        from books.models import SalespersonProfile
        dept_ids = _department_subtree_ids(user.profile.department_id)
        return set(SalespersonProfile.objects.filter(department_id__in=dept_ids)
                   .values_list('id', flat=True))
    if role == ROLE_SALESPERSON:
        sid = user.profile.salesperson_id
        return {sid} if sid else set()
    if role == ROLE_MERCHANT:
        merchant = user.profile.merchant
        return {merchant.salesperson_id} if merchant and merchant.salesperson_id else set()
    return set()


def visible_merchant_ids(user):
    """可见商户 id 集合；None 表示全部。"""
    if not _is_authorized(user):
        return set()
    role = get_user_role(user)
    if role in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_QUERY):
        return None
    from books.models import Merchant
    if role == ROLE_DEPARTMENT:
        dept_ids = _department_subtree_ids(user.profile.department_id)
        return set(Merchant.objects.filter(department_id__in=dept_ids).values_list('id', flat=True))
    if role == ROLE_SALESPERSON:
        sid = user.profile.salesperson_id
        if not sid:
            return set()
        return set(Merchant.objects.filter(salesperson_id=sid).values_list('id', flat=True))
    if role == ROLE_MERCHANT:
        mid = user.profile.merchant_id
        return {mid} if mid else set()
    return set()


def visible_order_queryset(user, qs):
    """按层级角色过滤订单查询集（行级权限，核心入口）。

    按角色选最合适的维度，用 OR 兼容「订单只填了商户或只填了业务员」的情况，
    避免 AND 导致漏数据。
    """
    from django.db.models import Q
    if not _is_authorized(user):
        return qs.none()
    role = get_user_role(user)
    if role in (ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_QUERY):
        return qs

    if role == ROLE_DEPARTMENT:
        dept_ids = _department_subtree_ids(user.profile.department_id)
        return qs.filter(department_id__in=dept_ids) if dept_ids else qs.none()

    if role == ROLE_SALESPERSON:
        sid = user.profile.salesperson_id
        from books.models import Merchant
        m_ids = set(Merchant.objects.filter(salesperson_id=sid).values_list('id', flat=True)) if sid else set()
        cond = Q()
        if sid:
            cond |= Q(salesperson_id=sid) | Q(salesperson_raw=user.profile.salesperson.name)
        if m_ids:
            cond |= Q(merchant_id__in=m_ids)
        return qs.filter(cond) if (sid or m_ids) else qs.none()

    if role == ROLE_MERCHANT:
        mid = user.profile.merchant_id
        return qs.filter(merchant_id=mid) if mid else qs.none()

    return qs.none()
