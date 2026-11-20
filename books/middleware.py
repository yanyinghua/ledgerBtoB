"""强制改密中间件。

当已登录用户的 ``UserProfile.must_change_password`` 为 True 时，除改密页与登出
外，所有请求一律跳转到改密页，直到用户成功修改密码（由 PasswordChangeView 清除标记）。
"""
from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # 预解析路径，避免在每次请求时重复 reverse
        self.password_change_path = reverse('password_change')
        self.logout_path = reverse('logout')

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            must_change = False
            profile = getattr(user, 'profile', None)
            if profile is not None:
                must_change = bool(getattr(profile, 'must_change_password', False))

            if must_change and request.path not in (self.password_change_path, self.logout_path):
                return redirect(self.password_change_path)

        return self.get_response(request)


class AccountAuthorizationMiddleware:
    """账号授权门禁：未授权用户零数据权限。

    已登录但 account_status != '已授权' 的用户，除「等待授权 / 改密 / 登出」
    外一律重定向到等待授权页，确保新注册用户在管理员授权前无法访问任何业务数据。
    与 ForcePasswordChangeMiddleware 并存（本中间件放在其后）。
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed_paths = None  # 延迟解析，避免 import 时 URL 尚未加载

    def _allowed(self):
        if self.allowed_paths is None:
            from django.urls import reverse
            self.allowed_paths = {
                reverse('auth_pending'),
                reverse('password_change'),
                reverse('logout'),
            }
        return self.allowed_paths

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, 'profile', None)
            status = getattr(profile, 'account_status', '待授权') if profile else '待授权'
            allowed = self._allowed()
            if status != '已授权' and request.path not in allowed:
                return redirect('auth_pending')
        return self.get_response(request)
