"""短信发送后端（可配置）。

后端由 settings.DJANGO_SMS_BACKEND 决定：
  - console（默认）：把验证码打印到控制台/日志，便于开发测试与端到端验证。
  - tencent：腾讯云短信（sms.v20210111 SDK），需配置环境变量：
        TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY  （API 密钥）
        TENCENT_SMS_SDK_APP_ID                            （短信应用 ID）
        TENCENT_SMS_SIGN_NAME                             （已审核签名）
        TENCENT_SMS_TEMPLATE_ID                           （已审核验证码模板 ID）
        TENCENT_SMS_REGION                                （可选，默认 ap-guangzhou）
  - aliyun：预留接口。

对外只暴露 send_sms(phone, code, purpose)；具体后端不抛异常给调用方，
发送失败时记录日志并返回 False，避免注册/登录流程因短信问题整体 500。

注意：调用方目前忽略返回值，故真实发送失败不会阻断注册/登录流程——
用户会在输入验证码时失败，日志中可查具体原因。
"""
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)


def _send_console(phone, code, purpose):
    logger.warning('[SMS console] 手机号=%s 用途=%s 验证码=%s', phone, purpose, code)
    return True


def _send_aliyun(phone, code, purpose):
    """预留：阿里云短信。配置 ALIYUN_SMS_* 后接入。"""
    logger.warning('阿里云短信后端尚未配置，验证码未真实发送（phone=%s）', phone)
    return False


def _to_e164(phone):
    """中国大陆手机号转 E.164：13900001234 / +8613900001234 / 8613900001234 → +8613900001234。"""
    p = str(phone).strip()
    if p.startswith('+'):
        return p
    digits = p.lstrip('+').strip()
    if digits.startswith('86') and len(digits) == 13:
        return '+' + digits
    return '+86' + digits


def _send_tencent(phone, code, purpose):
    """腾讯云短信：SendSms（验证码模板，模板变量 ${1}=验证码）。"""
    secret_id = os.environ.get('TENCENTCLOUD_SECRET_ID', '').strip()
    secret_key = os.environ.get('TENCENTCLOUD_SECRET_KEY', '').strip()
    sdk_app_id = os.environ.get('TENCENT_SMS_SDK_APP_ID', '').strip()
    sign_name = os.environ.get('TENCENT_SMS_SIGN_NAME', '').strip()
    template_id = os.environ.get('TENCENT_SMS_TEMPLATE_ID', '').strip()
    region = os.environ.get('TENCENT_SMS_REGION', 'ap-guangzhou').strip() or 'ap-guangzhou'

    missing = [k for k, v in (
        ('TENCENTCLOUD_SECRET_ID', secret_id),
        ('TENCENTCLOUD_SECRET_KEY', secret_key),
        ('TENCENT_SMS_SDK_APP_ID', sdk_app_id),
        ('TENCENT_SMS_SIGN_NAME', sign_name),
        ('TENCENT_SMS_TEMPLATE_ID', template_id),
    ) if not v]
    if missing:
        logger.error('腾讯云短信配置缺失 %s，验证码未真实发送（phone=%s）', missing, phone)
        return False

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.sms.v20210111 import sms_client, models
    except ImportError:
        logger.error('未安装 tencentcloud-sdk-python-sms，验证码未真实发送（phone=%s）', phone)
        return False

    try:
        cred = credential.Credential(secret_id, secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = 'sms.tencentcloudapi.com'
        http_profile.reqTimeout = 10
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = sms_client.SmsClient(cred, region, client_profile)

        req = models.SendSmsRequest()
        req.SmsSdkAppId = sdk_app_id
        req.SignName = sign_name
        req.TemplateId = template_id
        req.TemplateParamSet = [str(code)]
        req.PhoneNumberSet = [_to_e164(phone)]

        resp = client.SendSms(req)
        status_set = getattr(resp, 'SendStatusSet', None) or []
        if not status_set:
            logger.error('腾讯云短信返回空状态（phone=%s）：%s', phone, resp.to_json_string())
            return False
        first = status_set[0]
        if getattr(first, 'Code', '') == 'Ok':
            logger.info('腾讯云短信发送成功 phone=%s purpose=%s serial=%s',
                        phone, purpose, getattr(first, 'SerialNo', ''))
            return True
        logger.error('腾讯云短信发送失败 phone=%s purpose=%s code=%s message=%s',
                     phone, purpose, getattr(first, 'Code', ''), getattr(first, 'Message', ''))
        return False
    except TencentCloudSDKException as exc:
        logger.error('腾讯云短信 SDK 异常 phone=%s：%s', phone, exc)
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
