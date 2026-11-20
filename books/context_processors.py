"""向模板注入当前用户角色与菜单可见性标志。"""
from books.decorators import get_user_role, ROLE_ADMIN, ROLE_EXECUTIVE, ROLE_DEPARTMENT


def user_role(request):
    role = ''
    if request.user.is_authenticated:
        role = get_user_role(request.user)
    return {
        'user_role': role,
        'is_admin': role == ROLE_ADMIN,
        'is_executive': role == ROLE_EXECUTIVE,
        'is_department': role == ROLE_DEPARTMENT,
        'is_exec_or_admin': role in (ROLE_ADMIN, ROLE_EXECUTIVE),
        'can_view': bool(request.user.is_authenticated and
                         getattr(getattr(request.user, 'profile', None), 'account_status', '') == '已授权'),
    }
