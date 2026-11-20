"""机器指纹。

取 /etc/machine-id（容器里常为空，则退回主机名）+ 主网卡 MAC + 系统盘 UUID，
sha256 后取前 12 位作为机器号。换网卡不会误判，换主机/重建容器会变，需要重新发码。
"""
import hashlib
import os
import platform
import socket
import subprocess


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _primary_mac():
    try:
        base = '/sys/class/net'
        for name in sorted(os.listdir(base)):
            if name == 'lo':
                continue
            mac = _read(os.path.join(base, name, 'address'))
            if mac and mac != '00:00:00:00:00:00':
                return mac
    except OSError:
        pass
    return ''


def _root_uuid():
    try:
        out = subprocess.run(['findmnt', '-no', 'UUID', '-T', '/'],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def fingerprint_parts():
    machine_id = _read('/etc/machine-id') or _read('/var/lib/dbus/machine-id')
    return {
        'machine_id': machine_id,
        'hostname': '' if machine_id else socket.gethostname(),
        'mac': _primary_mac(),
        'disk': _root_uuid(),
        'platform': platform.system(),
    }


# 安装标识（保存在数据库，首次运行生成）优先于硬件指纹：
# 容器重建、换镜像、迁移到新机（带库一起迁）都不会变，避免误判换机。
_STORED = {'value': '', 'at': 0.0}
_STORED_TTL = 300


def _stored_install_id():
    import time
    if _STORED['value'] and time.time() - _STORED['at'] < _STORED_TTL:
        return _STORED['value']
    try:
        from django.db import DatabaseError
        from .models import LicenseState
        value = LicenseState.objects.filter(pk=1).values_list('install_id', flat=True).first() or ''
    except Exception:
        value = ''
    _STORED['value'] = value
    _STORED['at'] = time.time()
    return value


def fingerprint_id():
    """纯硬件/主机名指纹（容器里可能不稳定，仅供参考与兜底）。"""
    parts = fingerprint_parts()
    raw = '|'.join(str(v) for v in parts.values())
    return hashlib.sha256(raw.encode('utf-8')).hexdigest().upper()[:12]


def machine_id():
    return _stored_install_id() or fingerprint_id()
