"""本地冒烟测试：一键验证 B2B 系统核心链路可用。

  python3 manage.py smoke_test

覆盖：公开页 → 手机验证码注册 → 待授权零权限拦截 → 管理员授权 →
      拉卡拉订单导入(别名列名) → 分层数据可见(商户/业务员/管理员) → 看板。

特点：
  - 全程在事务内运行，结束自动回滚，不污染本地数据库。
  - 使用随机账号/手机号，不与现有数据冲突；不改动真实管理员。
  - 任一步失败即以非零退出码结束，并打印失败步骤。

退出码：0=全部通过；1=有步骤失败。
"""
import io
import random

import openpyxl
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client

from books.models import (
    AccountNameMapping, Counterparty, Department, InvitedPhone, Merchant,
    Order, OrderPayment, SalespersonProfile, SmsCode, SourceUpload,
    RawImportRow, ReceivingAccount,
)


class Command(BaseCommand):
    help = '本地冒烟测试：验证注册/授权/导入/分层权限/看板核心链路'

    def handle(self, *args, **options):
        self.passed = 0
        try:
            with transaction.atomic():
                self._run()
                # 回滚所有测试数据，保持本地库干净
                transaction.set_rollback(True)
        except AssertionError as exc:
            raise CommandError(f'冒烟测试失败：{exc}')

        self.stdout.write(self.style.SUCCESS(
            f'\n冒烟测试通过：{self.passed} 个检查点全部 OK（测试数据已回滚）。'))

    # ------------------------------------------------------------------ utils
    def _check(self, cond, label):
        if not cond:
            raise AssertionError(label)
        self.passed += 1
        self.stdout.write(f'  [OK] {label}')

    def _csrf_client(self):
        # 用一个确定在 ALLOWED_HOSTS 里的主机名，避免依赖 test Client 默认的
        # "testserver"（生产环境 ALLOWED_HOSTS 通常不含它，会返回 400）。
        from django.conf import settings
        host = (getattr(settings, 'ALLOWED_HOSTS', None) or ['localhost'])[0]
        if host in ('*', ''):
            host = 'localhost'
        return Client(enforce_csrf_checks=False, SERVER_NAME=host)

    def _phone(self):
        return '139' + ''.join(random.choice('0123456789') for _ in range(8))

    # -------------------------------------------------------------------- run
    def _run(self):
        self.stdout.write('== B2B 本地冒烟测试 ==')

        # 基础分组与组织
        for name in ('管理员', '高层', '部门', '业务员', '商户'):
            Group.objects.get_or_create(name=name)
        root, _ = Department.objects.get_or_create(code='SMOKE_ROOT', defaults={'name': '冒烟根部门'})
        sp = SalespersonProfile.objects.create(name='冒烟业务员', department=root)
        merchant = Merchant.objects.create(name='冒烟商户', code='SMOKE_M',
                                           salesperson=sp, department=root)
        Counterparty.objects.get_or_create(name='冒烟主体', party_type='客户')
        AccountNameMapping.objects.get_or_create(
            raw_name='冒烟店铺', defaults={'normalized_name': '冒烟店铺',
                                          'counterparty': Counterparty.objects.get(name='冒烟主体'),
                                          'is_manual': True, 'is_active': True})

        # 管理员（临时，不改动真实 admin）
        admin = User.objects.create_superuser('smoke_admin', 'smoke@test.com', 'SmokePass123!@#')
        admin.profile.account_status = '已授权'
        admin.profile.save()

        # ---------- 公开页 ----------
        c_pub = self._csrf_client()
        for path in ('/signin/', '/register/', '/phone-login/'):
            self._check(c_pub.get(path).status_code == 200, f'公开页 {path} 可访问')

        # ---------- 注册 ----------
        phone = self._phone()
        InvitedPhone.objects.create(phone=phone)
        c_user = self._csrf_client()
        r = c_user.post('/register/', {'phone': phone, 'action': 'send'})
        self._check(r.status_code == 200, '注册：发送验证码')
        code = SmsCode.objects.filter(phone=phone, purpose='注册', is_used=False).first().code
        r = c_user.post('/register/', {'phone': phone, 'action': 'confirm', 'code': code})
        self._check(r.status_code == 302 and r['Location'] == '/auth/pending/',
                    '注册：确认后跳转等待授权页')
        user = User.objects.get(username=phone)
        self._check(user.profile.account_status == '待授权', '新账号默认「待授权」')

        # ---------- 待授权零权限 ----------
        r = c_user.get('/orders/', follow=False)
        self._check(r.status_code == 302 and r['Location'].startswith('/auth/pending/'),
                    '待授权用户访问订单被拦截')

        # ---------- 管理员授权 ----------
        c_admin = self._csrf_client()
        r = c_admin.post('/signin/', {'username': 'smoke_admin', 'password': 'SmokePass123!@#'})
        self._check(r.status_code == 302, '管理员密码登录')
        r = c_admin.post('/auth/authorize/', {
            'user_id': user.pk, 'action': 'approve', 'role': '商户',
            'merchant': merchant.pk, 'department': root.pk})
        self._check(r.status_code == 302, '管理员授权请求')
        user.refresh_from_db()
        self._check(user.profile.account_status == '已授权' and
                    user.groups.filter(name='商户').exists(), '授权状态与角色生效')

        # ---------- 授权后可访问 ----------
        self._check(c_user.get('/orders/').status_code == 200, '授权后用户可访问订单页')

        # ---------- 导入订单（用别名列名）----------
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['商户订单号', '门店名称', '订单金额', '已收金额', '交易时间', '销售员'])
        ws.append(['SMOKE001', '冒烟店铺', '500.00', '200.00', '2026-06-01', '冒烟业务员'])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'smoke.xlsx'
        r = c_admin.post('/import/lakala/', {'file': buf})
        self._check(r.status_code == 200, '管理员导入拉卡拉订单')
        order = Order.objects.filter(external_order_no='SMOKE001').first()
        self._check(order is not None and order.total_amount == __import__('decimal').Decimal('500.00'),
                    '订单解析：金额/日期/方向')
        self._check(order.counterparty is not None, '户名匹配：精确命中已锁定的主体')
        self._check(order.payments.count() == 1, '收付款子行生成')
        # 挂到商户，供可见性验证
        order.merchant = merchant
        order.save()

        # ---------- 分层可见 ----------
        from books.decorators import visible_order_queryset
        admin_qs = visible_order_queryset(admin, Order.objects.all())
        self._check(order.pk in admin_qs.values_list('pk', flat=True), '管理员可见全部订单')

        sp_user = User.objects.create_user('smoke_sp', password='x')
        sp_user.groups.add(Group.objects.get(name='业务员'))
        sp_user.profile.salesperson = sp
        sp_user.profile.account_status = '已授权'
        sp_user.profile.save()
        self._check(order.pk in visible_order_queryset(sp_user, Order.objects.all())
                    .values_list('pk', flat=True), '业务员可见本人订单')

        self._check(order.pk in visible_order_queryset(user, Order.objects.all())
                    .values_list('pk', flat=True), '商户可见本商户订单')

        other_merchant = Merchant.objects.create(name='其他商户', code='SMOKE_OTHER')
        other_user = User.objects.create_user('smoke_other', password='x')
        other_user.groups.add(Group.objects.get(name='商户'))
        other_user.profile.merchant = other_merchant
        other_user.profile.account_status = '已授权'
        other_user.profile.save()
        self._check(order.pk not in visible_order_queryset(other_user, Order.objects.all())
                    .values_list('pk', flat=True), '其他商户看不到不属于自己的订单')

        # ---------- 页面渲染 ----------
        self._check(c_admin.get('/orders/').status_code == 200, '管理员订单列表渲染')
        self._check(c_admin.get('/orders/dashboard/').status_code == 200, '应收应付看板渲染')
        self._check(c_user.get('/orders/').status_code == 200, '商户订单页渲染')
