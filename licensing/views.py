"""授权页：查看状态、机器号，提交授权码激活。"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from books.decorators import get_user_role

from . import service
from .machine import fingerprint_parts, machine_id


@login_required
def license_page(request):
    if request.method == 'POST':
        if get_user_role(request.user) != '管理员':
            messages.error(request, '仅管理员可激活授权。')
            return redirect('licensing:license')
        code = request.POST.get('code', '').strip()
        if not code:
            messages.error(request, '请填写授权码。')
            return redirect('licensing:license')
        ok, msg = service.activate(code)
        messages.success(request, msg) if ok else messages.error(request, msg)
        return redirect('licensing:license')

    status = service.get_status(force=True)
    return render(request, 'licensing/license.html', {
        'status': status,
        'machine_id': machine_id(),
        'parts': fingerprint_parts(),
    })
