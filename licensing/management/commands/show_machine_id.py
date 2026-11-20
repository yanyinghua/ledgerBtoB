"""打印本机机器号，买方报给你用于签发授权码。"""
from django.core.management.base import BaseCommand

from licensing.machine import fingerprint_parts, machine_id


class Command(BaseCommand):
    help = '显示本机机器号（签发授权码时用）'

    def handle(self, *args, **options):
        self.stdout.write('机器号: ' + machine_id())
        for k, v in fingerprint_parts().items():
            self.stdout.write('  %-12s %s' % (k, v or '(空)'))
