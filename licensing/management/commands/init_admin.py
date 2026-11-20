"""首次安装时创建管理员账号。

仅在系统中没有任何用户时执行，已存在则跳过（重复部署不会覆盖既有账号）。
账号口令从环境变量 INIT_ADMIN_USER / INIT_ADMIN_PASSWORD 取，缺省 admin / admin123。
"""
import os

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '首次安装时创建管理员账号（无用户时才创建）'

    def handle(self, *args, **options):
        if User.objects.exists():
            self.stdout.write('已有用户，跳过创建管理员')
            return
        username = os.environ.get('INIT_ADMIN_USER', 'admin')
        password = os.environ.get('INIT_ADMIN_PASSWORD', 'admin123')
        user = User.objects.create_superuser(username=username, password=password, email='')
        group = Group.objects.filter(name='管理员').first()
        if group:
            user.groups.add(group)
        self.stdout.write(self.style.SUCCESS(
            '已创建管理员：%s / %s（请登录后立即修改密码）' % (username, password)))
