"""授权状态判定。

状态：
    active   已激活且在有效期内
    trial    未激活，试用期内（默认 30 天）
    expired  试用结束或授权到期 —— 只读（可查看、可导出），禁止写入
    invalid  授权码签名不合法 / 机器指纹不匹配
    tamper   检测到系统时间回拨 —— 同 expired，只读

结果在进程内缓存 5 分钟，避免每次请求都读写库。
"""
import datetime
import logging
import time

from django.db import DatabaseError, models
from django.utils import timezone

from .crypto import verify_code
from .machine import machine_id

logger = logging.getLogger(__name__)

TRIAL_DAYS = 30
CACHE_TTL = 300
TOLERANCE = datetime.timedelta(hours=1)   # 时间回拨容忍度（小于此值视为时钟漂移）
HEARTBEAT_MIN = datetime.timedelta(minutes=1)

READONLY_STATES = ('expired', 'invalid', 'tamper')

_cache = {'at': 0.0, 'state': None}


def _today():
    return timezone.localdate()


def _expiry_date(payload):
    exp = (payload or {}).get('exp') or '0'
    try:
        return datetime.datetime.strptime(str(exp), '%Y%m%d').date()
    except ValueError:
        return None


def _evaluate():
    try:
        from .models import LicenseState
        row = LicenseState.load()
    except DatabaseError:
        # 迁移未完成 / 库不可用时放行，避免把部署过程锁死
        logger.warning('licensing: 授权表不可用，本次跳过校验')
        return _make('active', None, '授权表未就绪')

    now = timezone.now()

    # 时间回拨检测
    if row.last_seen_at and now < row.last_seen_at - TOLERANCE:
        try:
            LicenseState.objects.filter(pk=row.pk).update(
                tamper_count=models.F('tamper_count') + 1)
        except DatabaseError:
            pass
        return _make('tamper', None, '检测到系统时间被回拨，请恢复正确时间后重试')

    if not row.last_seen_at or now - row.last_seen_at > HEARTBEAT_MIN:
        try:
            LicenseState.objects.filter(pk=row.pk).update(last_seen_at=now)
            row.last_seen_at = now
        except DatabaseError:
            pass

    payload = verify_code(row.license_code) if row.license_code else None

    if row.license_code and payload is None:
        return _make('invalid', None, '授权码无效')

    if payload:
        bound = payload.get('m') or ''
        if bound and bound != machine_id():
            return _make('invalid', None, '授权码与本机不匹配，请重新申请')
        exp = _expiry_date(payload)
        if exp and _today() > exp:
            return _make('expired', payload, '授权已于 %s 到期' % exp.isoformat())
        days = (exp - _today()).days if exp else None
        return _make('active', payload, '已激活', days_left=days)

    # 未激活：试用期
    if row.first_run_at is None:
        row.first_run_at = now
        try:
            LicenseState.objects.filter(pk=row.pk).update(first_run_at=now)
        except DatabaseError:
            pass
    used = (now - row.first_run_at).days
    if used >= TRIAL_DAYS:
        return _make('expired', None, '试用期（%d 天）已结束，请激活授权' % TRIAL_DAYS)
    return _make('trial', None, '试用中', days_left=TRIAL_DAYS - used)


def _make(state, payload, message, days_left=None):
    return {
        'state': state,
        'payload': payload or {},
        'message': message,
        'days_left': days_left,
        'readonly': state in READONLY_STATES,
        'machine_id': machine_id(),
    }


def get_status(force=False):
    """返回授权状态字典；进程内缓存 5 分钟。"""
    if force or _cache['state'] is None or time.time() - _cache['at'] > CACHE_TTL:
        _cache['state'] = _evaluate()
        _cache['at'] = time.time()
    return _cache['state']


def invalidate_cache():
    _cache['state'] = None
    _cache['at'] = 0.0


def activate(code):
    """校验并保存授权码。返回 (ok, message)。"""
    from .models import LicenseState

    payload = verify_code(code)
    if payload is None:
        return False, '授权码无效或已损坏'
    bound = payload.get('m') or ''
    if bound and bound != machine_id():
        return False, '该授权码绑定了其他机器（本机机器号 %s）' % machine_id()
    exp = _expiry_date(payload)
    if exp and _today() > exp:
        return False, '该授权码已于 %s 到期' % exp.isoformat()

    row = LicenseState.load()
    row.license_code = code.strip()
    row.payload = payload
    row.activated_at = timezone.now()
    row.last_seen_at = timezone.now()   # 重新起算心跳，避免历史时钟漂移一直锁死
    row.save(update_fields=['license_code', 'payload', 'activated_at', 'last_seen_at'])
    invalidate_cache()
    if exp:
        return True, '激活成功，有效期至 %s' % exp.isoformat()
    return True, '激活成功（永久授权）'
