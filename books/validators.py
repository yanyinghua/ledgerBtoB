"""自定义密码强度校验器。

目标：在保留现有「密码登录」的前提下，把弱密码挡在注册/改密环节之外，
避免口令泄露带来的风险。校验器会被 Django 的 AUTH_PASSWORD_VALIDATORS
自动作用于：admin 建号、PasswordChangeView 改密等所有走
``django.contrib.auth.password_validation.validate_password`` 的入口。
"""
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class PasswordComplexityValidator:
    """要求密码满足最小长度，且同时包含大写、小写、数字、特殊字符。"""

    def __init__(self, min_length=12):
        self.min_length = min_length

    def validate(self, password, user=None):
        errors = []
        if len(password) < self.min_length:
            errors.append(
                _('密码长度至少需要 %(min_length)d 位。') % {'min_length': self.min_length}
            )
        if not any(c.isupper() for c in password):
            errors.append(_('密码需包含至少一个大写字母（A-Z）。'))
        if not any(c.islower() for c in password):
            errors.append(_('密码需包含至少一个小写字母（a-z）。'))
        if not any(c.isdigit() for c in password):
            errors.append(_('密码需包含至少一个数字（0-9）。'))
        # 特殊字符：非字母数字的任意字符（含中文标点、空格等）
        if not any(not c.isalnum() for c in password):
            errors.append(_('密码需包含至少一个特殊字符（如 !@#$%^&* 等）。'))

        if errors:
            raise ValidationError(errors, code='password_complexity')

    def get_help_text(self):
        return _(
            '密码至少 %(min_length)d 位，且须同时包含大写字母、小写字母、数字和特殊字符。'
        ) % {'min_length': self.min_length}
