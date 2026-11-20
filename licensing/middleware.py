"""授权中间件。

未到期时完全放行；处于只读状态（试用结束/授权到期/授权无效/时间回拨）时：
  - 放行所有读请求（GET/HEAD/OPTIONS），保证数据可看、可导出；
  - 拦截所有写请求（POST/PUT/PATCH/DELETE），除授权页、登录/登出、改密。
"""
from django.shortcuts import render
from django.urls import reverse

from .service import get_status

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


class LicenseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.exempt = (
            reverse('licensing:license'), reverse('login'), reverse('logout'),
            reverse('password_change'),
        )

    def __call__(self, request):
        status = get_status()
        request.license_status = status

        if status['readonly'] and request.method not in SAFE_METHODS:
            path = request.path
            if not any(path.startswith(p) for p in self.exempt) \
                    and not path.startswith('/static/') and not path.startswith('/media/'):
                return render(request, 'licensing/readonly.html',
                              {'status': status}, status=403)

        return self.get_response(request)
