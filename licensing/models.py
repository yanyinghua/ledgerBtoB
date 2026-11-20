"""授权状态（单实例行，pk 固定为 1）。"""
import uuid

from django.db import models


class LicenseState(models.Model):
    """记录首次运行时间、已激活的授权码与最近一次心跳时间。

    last_seen_at 用于检测系统时间回拨：若当前时间早于上次记录，判定为篡改。
    """

    first_run_at = models.DateTimeField(null=True, blank=True, verbose_name='首次运行')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='最近心跳')
    license_code = models.TextField(blank=True, verbose_name='授权码')
    payload = models.JSONField(null=True, blank=True, verbose_name='授权内容')
    activated_at = models.DateTimeField(null=True, blank=True, verbose_name='激活时间')
    tamper_count = models.PositiveIntegerField(default=0, verbose_name='时间回拨次数')
    install_id = models.CharField(max_length=32, blank=True, verbose_name='安装标识')

    class Meta:
        verbose_name = '授权状态'
        verbose_name_plural = '授权状态'

    def __str__(self):
        return '授权状态'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        if not obj.install_id:
            # 安装标识：首次运行生成一次并落库。容器重建、换镜像都不会变，
            # 只有清库/重新部署才会变（届时授权码需重新签发）。
            obj.install_id = uuid.uuid4().hex[:12].upper()
            cls.objects.filter(pk=obj.pk).update(install_id=obj.install_id)
        return obj
