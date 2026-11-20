"""从 JSON 文件/目录注入拉卡拉订单（API 路径脚手架）。

每个 JSON 为一个订单 payload（字段按 ColumnMappingConfig 的 source_column 命名），
或 {"data": {...}}。走与 Excel 相同的 _build_order 逻辑，便于将来接 webhook。

  python3 manage.py ingest_lakala_api --src /path/to/json --dry-run
"""
import glob
import json
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from books import api_ingest


class Command(BaseCommand):
    help = '从 JSON 注入拉卡拉订单（API 脚手架）'

    def add_arguments(self, parser):
        parser.add_argument('--src', required=True, help='JSON 文件或目录')
        parser.add_argument('--dry-run', action='store_true')

    def _user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        return user

    def handle(self, *args, **options):
        src = options['src']
        if os.path.isdir(src):
            files = sorted(glob.glob(os.path.join(src, '*.json')))
        else:
            files = [src]

        if options['dry_run']:
            self.stdout.write(f'将注入 {len(files)} 个 payload')
            return

        user = self._user()
        ok = 0
        for path in files:
            try:
                with open(path, encoding='utf-8') as f:
                    payload = json.load(f)
                if isinstance(payload, dict) and 'data' in payload:
                    payload = payload['data']
                api_ingest.ingest_order_payload(payload, user)
                ok += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'{path}: {exc}'))
        self.stdout.write(self.style.SUCCESS(f'注入成功 {ok}/{len(files)}'))
