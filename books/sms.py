"""短信发送后端（可配置）。

后端由 settings.DJANGO_SMS_BACKEND 决定：
  - console（默认）：把验证码打印到控制台/日志，便于开发测试与端到端验证。
  - aliyun / tencent：预留接口，配置密钥与模板后启用（见 send_sms 内注释）。

对外只暴露 send_sms(phone, code, purpose)；具体后端不抛异常给调用方，
发送失败时记录日志并返回 False，避免注册/登录流程因短信问题整体 500。
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _send_console(phone, code, purpose):
    logger.warning('[SMS console] 手机号=%s 用途=%s 验证码=%s', phone, purpose, code)
    return True


def _send_aliyun(phone, code, purpose):
    """预留：阿里云短信。配置 ALIYUN_SMS_* 后接入 dashscope/sdk。"""
    logger.warning('阿里云短信后端尚未配置，验证码未真实发送（phone=%s）', phone)
    return False


def _send_tencent(phone, code, purpose):
    """预留：腾讯云短信。配置 TENCENT_SMS_* 后接入 qcloudsms。"""
    logger.warning('腾讯云短信后端尚未配置，验证码未真实发送（phone=%s）', phone)
    return False


_BACKENDS = {
    'console': _send_console,
    'aliyun': _send_aliyun,
    'tencent': _send_tencent,
}


def send_sms(phone, code, purpose):
    backend = getattr(settings, 'DJANGO_SMS_BACKEND', 'console')
    fn = _BACKENDS.get(backend, _send_console)
    try:
        return fn(phone, code, purpose)
    except Exception:
        logger.exception('短信发送异常（backend=%s, phone=%s）', backend, phone)
        return False
