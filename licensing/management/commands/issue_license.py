"""签发授权码（只在卖方机器上运行，需要私钥）。

用法：
    python manage.py issue_license --days 365                  # 不绑定机器，365 天
    python manage.py issue_license --machine A1B2C3D4E5F6 --perpetual
    python manage.py issue_license --expiry 20271231 --edition pro --seats 10
    python manage.py issue_license --generate-key              # 首次生成密钥对
"""
import base64
import datetime
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.management.base import BaseCommand, CommandError

from licensing.crypto import sign_code

DEFAULT_KEY_PATH = os.path.expanduser('~/.ledger-license/private.key')


def _load_private_key(path):
    try:
        with open(path, 'rb') as fh:
            raw = base64.b64decode(fh.read().strip())
    except OSError as exc:
        raise CommandError('读取私钥失败（%s）：%s' % (path, exc))
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise CommandError('私钥格式错误：%s' % exc)


class Command(BaseCommand):
    help = '签发授权码'

    def add_arguments(self, parser):
        parser.add_argument('--machine', default='', help='机器号，留空表示不绑定机器')
        parser.add_argument('--days', type=int, default=0, help='有效期天数，从今天起算')
        parser.add_argument('--expiry', default='', help='到期日 YYYYMMDD')
        parser.add_argument('--perpetual', action='store_true', help='永久授权')
        parser.add_argument('--edition', default='pro', help='版本标识，如 pro/standard')
        parser.add_argument('--seats', type=int, default=0, help='允许的用户数上限，0=不限')
        parser.add_argument('--private-key', default=DEFAULT_KEY_PATH, help='私钥文件路径')
        parser.add_argument('--generate-key', action='store_true', help='生成新的密钥对')

    def handle(self, *args, **options):
        if options['generate_key']:
            self._generate(options['private_key'])
            return

        expiry = options['expiry'].strip()
        if options['perpetual']:
            expiry = '0'
        elif not expiry:
            if not options['days']:
                raise CommandError('请指定 --days / --expiry / --perpetual 之一')
            expiry = (datetime.date.today() + datetime.timedelta(days=options['days'])).strftime('%Y%m%d')
        if expiry != '0':
            try:
                datetime.datetime.strptime(expiry, '%Y%m%d')
            except ValueError:
                raise CommandError('--expiry 必须是 YYYYMMDD')

        payload = {
            'e': options['edition'],
            'exp': expiry,
            'm': options['machine'].strip().upper(),
            's': int(options['seats']),
        }
        code = sign_code(payload, _load_private_key(options['private_key']))
        self.stdout.write(self.style.SUCCESS(code))
        self.stdout.write('内容: %s' % payload)

    def _generate(self, path):
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(serialization.Encoding.Raw,
                                 serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, base64.b64encode(raw))
        os.close(fd)
        self.stdout.write('私钥已写入: %s （务必离线备份，切勿随产品分发）' % path)
        self.stdout.write(self.style.SUCCESS('把下面这行填进 licensing/keys.py 的 PUBLIC_KEY_B64：'))
        self.stdout.write(base64.b64encode(pub).decode())
