"""把授权状态注入所有模板，用于顶部横幅提示。"""
from .service import get_status


def license_status(request):
    return {'license_status': get_status()}
