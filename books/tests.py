import csv
import os
import tempfile
import datetime
from unittest import skipIf
from django.test import TestCase, Client, RequestFactory
from django.core.management import call_command
from django.contrib.auth.models import User, Group
from django.urls import reverse
from django.db import connections
from urllib.parse import quote

from books.models import MasterRecord, RawImportRow, SourceUpload, ColumnMappingConfig, InvoiceLineItem, EXCLUDED_TAGS, LoginLog, OperationLog, AcceptanceOpeningBalance, UserProfile, FundReceiptHandler, OpeningBalance
from books.imports.engine import _build_record, run_import
from books.imports.rules import rule_red_detect
from books.imports.utils import upsert_customer_salesperson, to_date
from books.models import CustomerSalesperson, SalespersonChangeReminder, NameMapping
from books.models import (Order, OrderDetail, PaymentReceipt, ShopBusinessRegistry,
                          CollectionReminder, ShopStatement, CustomerEntity,
                          SalespersonProfile, Department)
from books.views import _customer_aging, _perf_for_customer, sales_perf_export
from books.imports.operators import op_sum_columns
from books.imports.lakala import _build_order
from books.imports.payment_receipt import SimpleMapping


class ToDateCompactTest(TestCase):
    """to_date 需支持建行等对公流水的紧凑日期格式（无分隔符）。"""

    def test_compact_date(self):
        self.assertEqual(to_date('20230615'), datetime.date(2023, 6, 15))

    def test_compact_datetime(self):
        self.assertEqual(to_date('20230615 18:34:08'), datetime.date(2023, 6, 15))

    def test_compact_datetime_no_seconds(self):
        self.assertEqual(to_date('20230615 18:34'), datetime.date(2023, 6, 15))

    def test_compact_in_long_string(self):
        # 落在实际内容中的紧凑日期也能被正则回退抓到
        self.assertEqual(to_date('流水号20230615abc'), datetime.date(2023, 6, 15))

    def test_existing_formats_unchanged(self):
        self.assertEqual(to_date('2023-06-15'), datetime.date(2023, 6, 15))
        self.assertEqual(to_date('2023-06-15 18:34:08'), datetime.date(2023, 6, 15))


class CleanHeadersTest(TestCase):
    """clean_headers 命令的单元测试：清理表头泄漏、幂等、dry-run、不误删真实记录。"""

    HEADER_LABEL = '购买方名称'

    def setUp(self):
        # 提供「表头列名」判定所需的列映射配置（真实环境由后台维护）
        ColumnMappingConfig.objects.create(
            source_type='sales_invoice', source_column=self.HEADER_LABEL,
            target_field='unit_name', is_active=True)
        self.upload = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')

    def _make_master(self, unit_name, **kw):
        defaults = dict(
            source_type='sales_invoice', source_upload=self.upload,
            unit_name=unit_name, biz_type='发票', year=0, period='',
            occur_date=None, debit=0, credit=0, category='', tag='',
            handler='', remark='', tax_no='', invoice_number='', blue_invoice_no='')
        defaults.update(kw)
        return MasterRecord.objects.create(**defaults)

    def _make_raw(self, unit_val):
        return RawImportRow.objects.create(
            source_upload=self.upload,
            data={self.HEADER_LABEL: unit_val, '备注': 'x'},
            row_hash='rh_%s' % unit_val)

    def test_removes_header_leak_but_keeps_real(self):
        header_m = self._make_master(self.HEADER_LABEL)              # 应删
        real_m = self._make_master('湖北真实有限公司', year=2024)     # 应留
        real_m2 = self._make_master('另一真实公司', year=0)           # 应留（year=0 但非表头）
        header_r = self._make_raw(self.HEADER_LABEL)                 # 应删
        real_r = self._make_raw('湖北真实有限公司')                   # 应留

        call_command('clean_headers')

        self.assertFalse(MasterRecord.objects.filter(id=header_m.id).exists())
        self.assertTrue(MasterRecord.objects.filter(id=real_m.id).exists())
        self.assertTrue(MasterRecord.objects.filter(id=real_m2.id).exists())
        self.assertFalse(RawImportRow.objects.filter(id=header_r.id).exists())
        self.assertTrue(RawImportRow.objects.filter(id=real_r.id).exists())

    def test_idempotent(self):
        m = self._make_master(self.HEADER_LABEL)
        r = self._make_raw(self.HEADER_LABEL)

        call_command('clean_headers')
        self.assertFalse(MasterRecord.objects.filter(id=m.id).exists())

        # 第二次运行不应再删除任何东西
        before_m = MasterRecord.objects.count()
        before_r = RawImportRow.objects.count()
        call_command('clean_headers')
        self.assertEqual(MasterRecord.objects.count(), before_m)
        self.assertEqual(RawImportRow.objects.count(), before_r)

    def test_dry_run_does_not_delete(self):
        m = self._make_master(self.HEADER_LABEL)
        r = self._make_raw(self.HEADER_LABEL)

        call_command('clean_headers', dry_run=True)

        self.assertTrue(MasterRecord.objects.filter(id=m.id).exists())
        self.assertTrue(RawImportRow.objects.filter(id=r.id).exists())


class HandlerAssignmentTest(TestCase):
    """经手人(业务员)派生规则：开票员也能当业务员，不再硬性排除；
    仅 2024-09 之前的销项发票 复核人 因当时无「复核人=业务员」规定而被忽略。"""

    def _build(self, handler_val, occur_date=None):
        mappings = [
            ColumnMappingConfig(source_type='sales_invoice', source_column='购买方名称',
                                 target_field='unit_name', is_active=True),
            ColumnMappingConfig(source_type='sales_invoice', source_column='复核人',
                                 target_field='handler', is_active=True),
            ColumnMappingConfig(source_type='sales_invoice', source_column='金额',
                                 target_field='debit', is_active=True),
            ColumnMappingConfig(source_type='sales_invoice', source_column='业务日期',
                                 target_field='occur_date', is_active=True),
        ]
        for m in mappings:
            m.save()
        upload = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        data = {'购买方名称': '测试公司', '复核人': handler_val, '金额': '100'}
        if occur_date is not None:
            data['业务日期'] = occur_date.strftime('%Y-%m-%d')
        return _build_record('sales_invoice', data, mappings, upload, [])

    def test_staff_name_now_kept(self):
        # 开票员（如郝玉）也能当业务员：不再被清空
        rec = self._build('郝玉', date(2025, 1, 1))
        self.assertEqual(rec.handler, '郝玉')

    def test_real_salesperson_kept(self):
        rec = self._build('张三', date(2025, 1, 1))
        self.assertEqual(rec.handler, '张三')

    def test_pre_2024_09_ignored(self):
        # 2024-09 之前销项发票 复核人 不作为经手人（当时无此规定）
        rec = self._build('李四', date(2024, 1, 1))
        self.assertEqual(rec.handler, '')


from datetime import date, timedelta
from books.models import SourceUpload, CustomerSalesperson


class CalibrateHandlersTest(TestCase):
    """calibrate_handlers：清内部人员、按历史刷新现任、尊重手动锁定。"""

    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        # 同一客户三笔历史：郝玉(2024-06, 较早) + 张三(2025, 最新真实) + 李四(2024-01)
        MasterRecord.objects.create(unit_name='测试客户', source_type='sales_invoice',
                                     source_upload=self.up, handler='郝玉', occur_date=date(2024, 6, 1),
                                     biz_type='发票', year=2024, period='202406', debit=1, credit=0)
        MasterRecord.objects.create(unit_name='测试客户', source_type='sales_invoice',
                                     source_upload=self.up, handler='张三', occur_date=date(2025, 1, 1),
                                     biz_type='发票', year=2025, period='202501', debit=1, credit=0)
        MasterRecord.objects.create(unit_name='测试客户', source_type='sales_invoice',
                                     source_upload=self.up, handler='李四', occur_date=date(2024, 1, 1),
                                     biz_type='发票', year=2024, period='202401', debit=1, credit=0)

    def test_calibrate_refreshes_current(self):
        from django.core.management import call_command
        call_command('calibrate_handlers')
        # 内部人员(郝玉)不再从主表清除，但校准后「现任」取历史最新真实经手人
        self.assertEqual(MasterRecord.objects.filter(handler='郝玉').count(), 1)
        self.assertEqual(CustomerSalesperson.objects.filter(salesperson='郝玉').count(), 0)
        cur = CustomerSalesperson.objects.filter(unit_name='测试客户', is_current=True).first()
        self.assertIsNotNone(cur)
        self.assertEqual(cur.salesperson, '张三')  # 历史最新真实经手人

    def test_dry_run_no_write(self):
        from django.core.management import call_command
        call_command('calibrate_handlers', dry_run=True)
        self.assertEqual(MasterRecord.objects.filter(handler='郝玉').count(), 1)  # 未清
        self.assertEqual(CustomerSalesperson.objects.count(), 0)                  # 未建

    def test_manual_override_preserved(self):
        from django.core.management import call_command
        CustomerSalesperson.objects.create(unit_name='测试客户', salesperson='王五',
                                            is_current=True, manual_override=True,
                                            effective_date=date(2020, 1, 1))
        call_command('calibrate_handlers')
        cur = CustomerSalesperson.objects.filter(unit_name='测试客户', is_current=True).first()
        self.assertEqual(cur.salesperson, '王五')  # 不被派生张三覆盖


class ReminderOnlyTest(TestCase):
    """导入路径：派生业务员 ≠ 现任 时只写「业务员变更提醒」、不翻现任；
    一致时不写提醒；auto_current=True（校准/后台）才直接翻现任。"""

    def setUp(self):
        CustomerSalesperson.objects.create(unit_name='提醒客户', salesperson='张三',
                                           is_current=True, effective_date=date(2025, 1, 1))

    def test_mismatch_records_reminder_only(self):
        upsert_customer_salesperson('提醒客户', '李四', '发票复核人', date(2025, 6, 1),
                                    auto_current=False)
        # 现任未被改动
        cur = CustomerSalesperson.objects.get(unit_name='提醒客户', is_current=True)
        self.assertEqual(cur.salesperson, '张三')
        # 已写入一条待处理提醒
        rem = SalespersonChangeReminder.objects.get(unit_name='提醒客户', status='待处理')
        self.assertEqual(rem.current_salesperson, '张三')
        self.assertEqual(rem.suggested_salesperson, '李四')

    def test_same_no_reminder(self):
        upsert_customer_salesperson('提醒客户', '张三', '发票复核人', date(2025, 6, 1),
                                    auto_current=False)
        self.assertFalse(SalespersonChangeReminder.objects.filter(
            unit_name='提醒客户', status='待处理').exists())

    def test_autocurrent_flips_and_no_reminder(self):
        upsert_customer_salesperson('提醒客户', '李四', '发票复核人', date(2025, 6, 1),
                                    auto_current=True)
        cur = CustomerSalesperson.objects.get(unit_name='提醒客户', is_current=True)
        self.assertEqual(cur.salesperson, '李四')  # 校准/后台路径直接翻现任
        self.assertFalse(SalespersonChangeReminder.objects.filter(
            unit_name='提醒客户', status='待处理').exists())


class ConflictFlagTest(TestCase):
    """客户-业务员映射后台应标出 派生最新 ≠ 现任 的冲突。"""

    def test_conflict_flag(self):
        from books.admin import CustomerSalespersonAdmin
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        MasterRecord.objects.create(unit_name='冲突客户', source_type='sales_invoice',
                                     source_upload=up, handler='赵六', occur_date=date(2025, 1, 1),
                                     biz_type='发票', year=2025, period='202501', debit=1, credit=0)
        CustomerSalesperson.objects.create(unit_name='冲突客户', salesperson='钱七',
                                            is_current=True, effective_date=date(2024, 1, 1))
        admin = CustomerSalespersonAdmin(CustomerSalesperson, None)
        obj = CustomerSalesperson.objects.get(unit_name='冲突客户', is_current=True)
        flag = admin.conflict_flag(obj)
        self.assertIn('冲突', flag)
        self.assertIn('赵六', flag)

    def test_no_conflict_when_same(self):
        from books.admin import CustomerSalespersonAdmin
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        MasterRecord.objects.create(unit_name='一致客户', source_type='sales_invoice',
                                     source_upload=up, handler='孙八', occur_date=date(2025, 1, 1),
                                     biz_type='发票', year=2025, period='202501', debit=1, credit=0)
        CustomerSalesperson.objects.create(unit_name='一致客户', salesperson='孙八',
                                            is_current=True, effective_date=date(2024, 1, 1))
        admin = CustomerSalespersonAdmin(CustomerSalesperson, None)
        obj = CustomerSalesperson.objects.get(unit_name='一致客户', is_current=True)
        self.assertEqual(admin.conflict_flag(obj), '')


class SalespersonEditCreditTermTest(TestCase):
    """业务员仅能编辑自己客户账期；管理员可全改；查询员不可改。"""

    def setUp(self):
        self.g_sp = Group.objects.create(name='业务员')
        self.g_admin = Group.objects.create(name='管理员')
        self.g_query = Group.objects.create(name='查询员')

        self.admin = User.objects.create_user('admin1', password='x')
        self.admin.groups.add(self.g_admin)

        self.sp = User.objects.create_user('gaowei', password='x')
        self.sp.groups.add(self.g_sp)
        self.sp.profile.salesperson_name = '高维'
        self.sp.profile.save()

        self.query = User.objects.create_user('query1', password='x')
        self.query.groups.add(self.g_query)

        self.own = CustomerSalesperson.objects.create(
            unit_name='高维客户A', salesperson='高维', is_current=True, credit_term=30)
        self.other = CustomerSalesperson.objects.create(
            unit_name='李四客户B', salesperson='李四', is_current=True, credit_term=45)

        self.client = Client()

    def test_salesperson_can_edit_own_credit_term(self):
        self.client.login(username='gaowei', password='x')
        resp = self.client.post(reverse('customer_edit', args=[self.own.id]),
                                {'credit_term': 60})
        self.assertEqual(resp.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.credit_term, 60)
        self.assertEqual(self.own.salesperson, '高维')   # 其它字段不被改动
        self.assertTrue(self.own.is_current)

    def test_salesperson_cannot_edit_other_get(self):
        self.client.login(username='gaowei', password='x')
        resp = self.client.get(reverse('customer_edit', args=[self.other.id]))
        self.assertEqual(resp.status_code, 302)

    def test_salesperson_cannot_edit_other_post(self):
        self.client.login(username='gaowei', password='x')
        resp = self.client.post(reverse('customer_edit', args=[self.other.id]),
                                {'credit_term': 99})
        self.assertEqual(resp.status_code, 302)
        self.other.refresh_from_db()
        self.assertEqual(self.other.credit_term, 45)     # 未变更

    def test_salesperson_cannot_change_other_fields(self):
        # 即便 POST 里夹带 salesperson，受限表单也应忽略
        self.client.login(username='gaowei', password='x')
        self.client.post(reverse('customer_edit', args=[self.own.id]),
                         {'credit_term': 75, 'salesperson': '黑客', 'is_current': False})
        self.own.refresh_from_db()
        self.assertEqual(self.own.credit_term, 75)
        self.assertEqual(self.own.salesperson, '高维')
        self.assertTrue(self.own.is_current)

    def test_query_cannot_edit(self):
        self.client.login(username='query1', password='x')
        resp = self.client.post(reverse('customer_edit', args=[self.own.id]),
                                {'credit_term': 99})
        self.assertEqual(resp.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.credit_term, 30)      # 未变更

    def test_admin_can_edit_all_fields(self):
        self.client.login(username='admin1', password='x')
        resp = self.client.post(reverse('customer_edit', args=[self.other.id]),
                                {'unit_name': '李四客户B', 'salesperson': '李四',
                                 'is_current': 'on', 'source': '手动', 'credit_term': 90})
        self.assertEqual(resp.status_code, 302)
        self.other.refresh_from_db()
        self.assertEqual(self.other.credit_term, 90)



# ---------------------------------------------------------------------------
# 登录日志：auth 信号自动记录 登录成功 / 登出 / 登录失败
# ---------------------------------------------------------------------------
class LoginLogTest(TestCase):
    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('logger', password='x')
        self.user.groups.add(g)
        self.client = Client()

    def test_success_and_logout_logged(self):
        self.client.login(username='logger', password='x')
        self.assertEqual(
            LoginLog.objects.filter(username='logger', action='登录成功').count(), 1)
        self.client.logout()
        self.assertEqual(
            LoginLog.objects.filter(username='logger', action='登出').count(), 1)

    def test_failed_login_logged(self):
        self.client.login(username='logger', password='wrong')
        self.assertEqual(
            LoginLog.objects.filter(username='logger', action='登录失败').count(), 1)


# ---------------------------------------------------------------------------
# 业务操作日志：编辑账期 / 系统任务 写入 OperationLog
# ---------------------------------------------------------------------------
class OperationLogTest(TestCase):
    def setUp(self):
        self.g_sp = Group.objects.get_or_create(name='业务员')[0]
        self.sp = User.objects.create_user('zsc', password='x')
        self.sp.groups.add(self.g_sp)
        self.sp.profile.salesperson_name = '张三'
        self.sp.profile.save()
        self.sp_row = CustomerSalesperson.objects.create(
            unit_name='操作日志客户', salesperson='张三', is_current=True, credit_term=30)
        self.client = Client()

    def test_credit_term_edit_logged(self):
        self.client.login(username='zsc', password='x')
        self.client.post(reverse('customer_edit', args=[self.sp_row.id]),
                         {'credit_term': 60})
        self.sp_row.refresh_from_db()
        self.assertEqual(self.sp_row.credit_term, 60)
        log = OperationLog.objects.filter(action='编辑客户映射').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'zsc')
        self.assertIn('账期', log.detail)

    def test_system_log_with_username(self):
        from books.models import log_operation
        log_operation(None, '校准现任', username='系统(calibrate_handlers)', detail='x')
        log = OperationLog.objects.get(action='校准现任')
        self.assertEqual(log.username, '系统(calibrate_handlers)')
        self.assertIsNone(log.user)

    def _make_admin(self, name):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user(name, password='x')
        u.groups.add(g)
        return u

    def test_create_logged(self):
        admin = self._make_admin('opadmin2')
        self.client.login(username='opadmin2', password='x')
        self.client.post(reverse('customer_add'), {
            'unit_name': '新增日志客户', 'salesperson': '王五', 'source': '手动',
            'is_current': 'on', 'credit_term': '30', 'effective_date': '2025-01-01'})
        self.assertTrue(CustomerSalesperson.objects.filter(unit_name='新增日志客户').exists())
        log = OperationLog.objects.filter(action='新增客户映射').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'opadmin2')
        self.assertIn('新增日志客户', log.detail)

    def test_delete_logged(self):
        admin = self._make_admin('opadmin3')
        row = CustomerSalesperson.objects.create(
            unit_name='待删客户', salesperson='李四', is_current=True)
        self.client.login(username='opadmin3', password='x')
        self.client.post(reverse('customer_delete', args=[row.id]))
        self.assertFalse(CustomerSalesperson.objects.filter(pk=row.pk).exists())
        log = OperationLog.objects.filter(action='删除客户映射').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'opadmin3')
        self.assertIn('待删客户', log.detail)

    def test_namemap_create_logged(self):
        self._make_admin('oplog_nm')
        self.client.login(username='oplog_nm', password='x')
        self.client.post(reverse('namemap_add'),
                         {'raw_name': '北京分公司', 'canonical_name': '北京总公司', 'tax_no': ''})
        self.assertTrue(NameMapping.objects.filter(raw_name='北京分公司').exists())
        log = OperationLog.objects.filter(action='新增名称映射').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'oplog_nm')
        self.assertIn('北京分公司', log.detail)

    def test_apply_all_logged(self):
        self._make_admin('oplog_apply')
        self.client.login(username='oplog_apply', password='x')
        self.client.post(reverse('namemap_apply_all'), {'source_types': []})
        log = OperationLog.objects.filter(action='应用名称映射').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'oplog_apply')

    def test_acceptance_opening_logged(self):
        self._make_admin('oplog_ob')
        self.client.login(username='oplog_ob', password='x')
        self.client.post(reverse('acceptance_report_set_opening'),
                         {'as_of_date': '2025-01-01', 'amount': '1000', 'remark': '期初'})
        log = OperationLog.objects.filter(action='设置承兑期初').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'oplog_ob')
        # 第二次为更新
        self.client.post(reverse('acceptance_report_set_opening'),
                         {'as_of_date': '2025-01-01', 'amount': '2000', 'remark': '调整'})
        self.assertIsNotNone(OperationLog.objects.filter(action='更新承兑期初').first())

    def test_record_tag_update_logged(self):
        self._make_admin('oplog_tag')
        self.client.login(username='oplog_tag', password='x')
        rec = MasterRecord.objects.create(
            source_type='sales_invoice', unit_name='标注客户', biz_type='发票',
            year=2025, period='', occur_date=None, debit=0, credit=0,
            category='', tag='', handler='', remark='', tax_no='',
            invoice_number='', blue_invoice_no='')
        self.client.post(reverse('record_tag_update'),
                         {'tag_%d' % rec.pk: '红冲', 'next': '/'})
        log = OperationLog.objects.filter(action='更新标注/备注').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'oplog_tag')
        self.assertIn(str(rec.pk), log.target_id)

    def test_import_delete_logged(self):
        self._make_admin('oplog_imp')
        self.client.login(username='oplog_imp', password='x')
        upload = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        MasterRecord.objects.create(
            source_type='sales_invoice', source_upload=upload, unit_name='导入客户',
            biz_type='发票', year=2025, period='', occur_date=None, debit=0, credit=0,
            category='', tag='', handler='', remark='', tax_no='',
            invoice_number='', blue_invoice_no='')
        self.client.post(reverse('import_delete', args=[upload.pk]))
        self.assertFalse(SourceUpload.objects.filter(pk=upload.pk).exists())
        log = OperationLog.objects.filter(action='删除导入').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.username, 'oplog_imp')
        self.assertEqual(log.target_id, str(upload.pk))

    def test_trace_oplog_resolves_target(self):
        # 写一条指向 MasterRecord 的操作日志，验证 trace_oplog 能反查到业务记录
        from django.core.management import call_command
        from io import StringIO
        rec = MasterRecord.objects.create(
            source_type='sales_invoice', unit_name='追溯客户', biz_type='银行',
            year=2024, period='', occur_date=None, debit=0, credit=24000,
            category='贷方', tag='', handler='', remark='辅料1', tax_no='',
            invoice_number='', blue_invoice_no='')
        OperationLog.objects.create(
            username='admin', action='更新标注/备注', target_type='MasterRecord',
            target_id=str(rec.pk), detail='更新 1 条记录的标注或备注')
        out = StringIO()
        call_command('trace_oplog', str(rec.pk), stdout=out)
        output = out.getvalue()
        self.assertIn('追溯客户', output)
        self.assertIn('MasterRecord', output)
        self.assertIn('24000', output)


# ---------------------------------------------------------------------------
# OperationLogAdmin.target_link：对象标识渲染为可点击跳转链接
# ---------------------------------------------------------------------------
class OperationLogLinkTest(TestCase):
    def _admin(self):
        from books.admin import OperationLogAdmin
        from books.models import OperationLog
        return OperationLogAdmin(OperationLog, None)

    def test_masterrecord_links_to_invoice_detail(self):
        html = self._admin().target_link(
            OperationLog(target_type='MasterRecord', target_id='216863'))
        self.assertIn('/invoice/216863/', html)
        self.assertIn('target="_blank"', html)

    def test_other_types_link_to_their_pages(self):
        cases = [
            ('CustomerSalesperson', '5', '/data/customer/5/edit/'),
            ('NameMapping', '3', '/data/namemap/3/edit/'),
            ('SourceUpload', '7', '/import/list/'),
            ('AcceptanceOpeningBalance', '1', '/acceptance/report/'),
        ]
        for ttype, tid, expected in cases:
            html = self._admin().target_link(
                OperationLog(target_type=ttype, target_id=tid))
            self.assertIn(expected, html)

    def test_system_or_empty_shows_dash(self):
        admin = self._admin()
        self.assertEqual(
            admin.target_link(OperationLog(target_type='系统', target_id='')), '—')
        self.assertEqual(
            admin.target_link(OperationLog(target_type='MasterRecord', target_id='')), '—')

    def test_multiple_pks_split_by_separator(self):
        html = self._admin().target_link(
            OperationLog(target_type='MasterRecord', target_id='216863、216864'))
        self.assertIn('/invoice/216863/', html)
        self.assertIn('/invoice/216864/', html)


# ---------------------------------------------------------------------------
# OperationLog 导出(admin 动作) / 清理(clean_oplog 命令)
# ---------------------------------------------------------------------------
class OperationLogExportTest(TestCase):
    def _make(self, username, when):
        # created_at 是 auto_now_add，创建时会被强制覆盖，故先建后单独更新该字段
        from django.utils import timezone
        o = OperationLog.objects.create(
            username=username, action='更新标注/备注', target_type='MasterRecord',
            target_id='1', detail='x')
        o.created_at = timezone.make_aware(when)
        o.save(update_fields=['created_at'])
        return o

    def test_admin_export_csv_action(self):
        from django.test import RequestFactory
        from books.admin import OperationLogAdmin
        from datetime import datetime
        self._make('alice', datetime(2024, 1, 1))
        self._make('bob', datetime(2024, 6, 1))
        qs = OperationLog.objects.all()
        req = RequestFactory().get('/')
        resp = OperationLogAdmin(OperationLog, None).export_selected_csv(req, qs)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        body = resp.content.decode('utf-8-sig')
        self.assertIn('alice', body)
        self.assertIn('bob', body)
        self.assertIn('id,时间,操作人', body)

    def test_clean_oplog_exports_then_deletes(self):
        from datetime import datetime
        from django.core.management import call_command
        import tempfile, os
        self._make('old1', datetime(2020, 1, 1))
        self._make('old2', datetime(2020, 6, 1))
        self._make('new1', datetime(2030, 1, 1))  # 未来，不会被删
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        try:
            call_command('clean_oplog', '--before', '2021-01-01', '--output', path)
            with open(path, encoding='utf-8-sig') as f:
                rows = f.read().strip().splitlines()
            # 表头 + 2 条旧记录
            self.assertEqual(len(rows), 3)
            self.assertIn('old1', rows[1] + rows[2])
            # 仅剩未来那条
            self.assertEqual(OperationLog.objects.count(), 1)
            self.assertEqual(OperationLog.objects.first().username, 'new1')
        finally:
            os.remove(path)

    def test_clean_oplog_dry_run_keeps_records(self):
        from datetime import datetime
        from django.core.management import call_command
        import tempfile, os
        self._make('old1', datetime(2020, 1, 1))
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        try:
            call_command('clean_oplog', '--before', '2021-01-01',
                         '--output', path, '--dry-run')
            self.assertTrue(OperationLog.objects.filter(username='old1').exists())
            self.assertFalse(os.path.getsize(path) > 0)  # dry-run 不写文件
        finally:
            os.remove(path)

    def test_clean_oplog_export_only_keeps_records(self):
        from datetime import datetime
        from django.core.management import call_command
        import tempfile, os
        self._make('old1', datetime(2020, 1, 1))
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        try:
            call_command('clean_oplog', '--before', '2021-01-01',
                         '--output', path, '--export-only')
            # 导出文件有内容
            with open(path, encoding='utf-8-sig') as f:
                self.assertIn('old1', f.read())
            # 但记录保留
            self.assertTrue(OperationLog.objects.filter(username='old1').exists())
        finally:
            os.remove(path)

    def test_export_oplog_by_timerange(self):
        from datetime import datetime
        from django.core.management import call_command
        import tempfile, os
        self._make('old1', datetime(2020, 1, 1))
        self._make('old2', datetime(2020, 6, 1))
        self._make('new1', datetime(2030, 1, 1))  # 超出 before，不应被导出
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        try:
            call_command('export_oplog', '--before', '2021-01-01', '--output', path)
            with open(path, encoding='utf-8-sig') as f:
                rows = f.read().strip().splitlines()
            self.assertEqual(len(rows), 3)  # 表头 + 2 条旧记录
            body = rows[1] + rows[2]
            self.assertIn('old1', body)
            self.assertIn('old2', body)
            self.assertNotIn('new1', body)
            # 纯导出，不影响线上
            self.assertEqual(OperationLog.objects.count(), 3)
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# 货币格式过滤器 / 明细近1年默认 / 催收穿透链接
# ---------------------------------------------------------------------------
from books.templatetags.money import cny


class MoneyFilterTest(TestCase):
    def test_none_and_empty(self):
        self.assertEqual(cny(None), '')
        self.assertEqual(cny(''), '')

    def test_format(self):
        self.assertEqual(cny(1234.5), '¥1,234.50')
        self.assertEqual(cny(0), '¥0.00')
        self.assertEqual(cny('9876.5'), '¥9,876.50')

    def test_decimals_param(self):
        self.assertEqual(cny('1.2345', 4), '¥1.2345')

    def test_invalid_passthrough(self):
        self.assertEqual(cny('abc'), 'abc')

    def test_currency_code(self):
        # 传入 ISO 货币代码时映射为对应符号
        self.assertEqual(cny(1234.5, 'USD'), '$1,234.50')
        self.assertEqual(cny(1234.5, 'EUR'), '€1,234.50')
        self.assertEqual(cny(1234.5, 'GBP'), '£1,234.50')
        self.assertEqual(cny(1234.5, 'HKD'), 'HK$1,234.50')
        # CNY / RMB / JPY 均映射到 ¥
        self.assertEqual(cny(1234.5, 'CNY'), '¥1,234.50')
        self.assertEqual(cny(1234.5, 'RMB'), '¥1,234.50')

    def test_currency_code_with_decimals(self):
        # 第一个位置参数仍为 decimals 时，第二个位置参数为货币代码
        self.assertEqual(cny(1234.5, 4, 'EUR'), '€1,234.5000')
        self.assertEqual(cny(1234.5, 4, 'USD'), '$1,234.5000')

    def test_raw_symbol_passthrough(self):
        # 未知代码或原始符号直接透传为前缀
        self.assertEqual(cny(1234.5, '£'), '£1,234.50')
        self.assertEqual(cny(1234.5, 'AU$'), 'AU$1,234.50')
        self.assertEqual(cny(1234.5, 'XYZ'), 'XYZ1,234.50')

    def test_empty_symbol_falls_back_to_yen(self):
        # 空符号回退到 ¥，避免前缀为空
        self.assertEqual(cny(1234.5, ''), '¥1,234.50')


class StatementDefaultRangeTest(TestCase):
    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('stmt', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='stmt', password='x')
        self.up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        old = date.today() - timedelta(days=800)
        new = date.today()
        MasterRecord.objects.create(unit_name='范围客户', source_type='sales_invoice',
                                     source_upload=self.up, handler='', occur_date=old,
                                     biz_type='发票', year=old.year, period=old.strftime('%Y%m'),
                                     debit=100, credit=0)
        MasterRecord.objects.create(unit_name='范围客户', source_type='sales_invoice',
                                     source_upload=self.up, handler='', occur_date=new,
                                     biz_type='发票', year=new.year, period=new.strftime('%Y%m'),
                                     debit=200, credit=0)

    def test_no_default_date_includes_old(self):
        resp = self.client.get('/statement/?unit_name=范围客户')
        self.assertContains(resp, '¥200.00')         # 今年记录可见
        self.assertContains(resp, '¥100.00')         # 无默认时间范围，历史记录也显示

    def test_explicit_date_from_includes_old(self):
        resp = self.client.get('/statement/?unit_name=范围客户&date_from=2000-01-01')
        self.assertContains(resp, '¥100.00')
        self.assertContains(resp, '¥200.00')

    def test_explicit_date_from_excludes_old(self):
        # 反向：显式给一个较近的起始日，历史记录不再出现在明细行里
        recent = (date.today() - timedelta(days=10)).strftime('%Y-%m-%d')
        resp = self.client.get('/statement/?unit_name=范围客户&date_from=%s' % recent)
        self.assertContains(resp, '¥200.00')         # 今年记录可见
        self.assertEqual(len(resp.context['rows']), 1)          # 800 天前那笔已不出行
        # 被排除的旧记录金额计入期初（期初 = 起始日之前的期初表 + 主表发生额）
        self.assertEqual(round(resp.context['opening'], 2), 100.0)


class StatementOpeningBalanceTest(TestCase):
    """选了起始日期/期间/年份后，期初 = 期初表 + 窗口之前的主表发生额。

    只取 OpeningBalance 会导致期初停留在「最初金额」，与对账单口径不一致。
    """

    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('op', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='op', password='x')
        self.up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx',
                                              status='success')
        self.unit = '期初客户'
        OpeningBalance.objects.create(unit_name=self.unit, biz_type='发票',
                                      occur_date=date(2025, 1, 1), debit=100, credit=0)
        self._mr(date(2025, 6, 1), debit=500)
        self._mr(date(2026, 2, 1), credit=200)
        self._mr(date(2026, 3, 1), debit=50)

    def _mr(self, d, debit=0, credit=0):
        return MasterRecord.objects.create(
            unit_name=self.unit, source_type='sales_invoice', source_upload=self.up,
            handler='', occur_date=d, biz_type='发票', year=d.year, period=d.strftime('%Y%m'),
            debit=debit, credit=credit)

    def _opening(self, query):
        resp = self.client.get('/statement/?unit_name=%s&%s' % (self.unit, query))
        self.assertEqual(resp.status_code, 200)
        return round(resp.context['opening'], 2)

    def test_no_filter_opening_is_initial(self):
        # 不限窗口：明细是全量，期初即最初余额
        self.assertEqual(self._opening(''), 100.0)

    def test_date_from_includes_prior_records(self):
        # 2026-01-01 起：期初 = 100(期初表) + 500(2025-06 那笔)
        self.assertEqual(self._opening('date_from=2026-01-01'), 600.0)

    def test_year_filter_includes_prior_records(self):
        self.assertEqual(self._opening('year=2026'), 600.0)

    def test_period_filter_includes_prior_records(self):
        self.assertEqual(self._opening('period=202602'), 600.0)

    def test_ending_balance_consistent(self):
        resp = self.client.get('/statement/?unit_name=%s&date_from=2026-01-01' % self.unit)
        # 600 期初 - 200 + 50
        self.assertEqual(round(resp.context['ending'], 2), 450.0)

    def test_export_opening_matches_page(self):
        from io import BytesIO
        from openpyxl import load_workbook
        resp = self.client.get('/statement/export/?unit_name=%s&date_from=2026-01-01' % self.unit)
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(BytesIO(resp.content))
        ws = wb.active
        opening_row = [r for r in ws.iter_rows(values_only=True) if r and r[0] == '期初余额'][0]
        self.assertEqual(opening_row[5], 600)     # 期初列
        # 导出同样应用日期范围：只应有 2026 年两笔，不含 2025 那笔
        dates = [r[0] for r in ws.iter_rows(values_only=True) if isinstance(r[0], date)]
        self.assertEqual(len(dates), 2)


class DepositSearchNoDefaultDateTest(TestCase):
    """deposit / global_search 同样不应默认近一年：缺省 date_from 时历史记录仍显示。"""

    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('ds', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='ds', password='x')
        self.up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        old = date.today() - timedelta(days=800)
        new = date.today()
        # 保证金记录（deposit 页按 tag=保证金 过滤）
        MasterRecord.objects.create(unit_name='保证金单位', source_type='other', tag='保证金',
                                     source_upload=self.up, handler='', occur_date=old,
                                     biz_type='其他', year=old.year, period=old.strftime('%Y%m'),
                                     debit=100, credit=0)
        MasterRecord.objects.create(unit_name='保证金单位', source_type='other', tag='保证金',
                                     source_upload=self.up, handler='', occur_date=new,
                                     biz_type='其他', year=new.year, period=new.strftime('%Y%m'),
                                     debit=200, credit=0)
        # 一般记录（global_search 全局检索）
        MasterRecord.objects.create(unit_name='检索单位', source_type='sales_invoice', tag='',
                                     source_upload=self.up, handler='', occur_date=old,
                                     biz_type='发票', year=old.year, period=old.strftime('%Y%m'),
                                     debit=100, credit=0)
        MasterRecord.objects.create(unit_name='检索单位', source_type='sales_invoice', tag='',
                                     source_upload=self.up, handler='', occur_date=new,
                                     biz_type='发票', year=new.year, period=new.strftime('%Y%m'),
                                     debit=200, credit=0)

    def test_deposit_no_default_date_includes_old(self):
        resp = self.client.get('/deposit/?unit_name=保证金单位')
        self.assertContains(resp, '¥200.00')         # 今年记录可见
        self.assertContains(resp, '¥100.00')         # 无默认时间范围，历史保证金记录也显示

    def test_global_search_no_default_date_includes_old(self):
        resp = self.client.get('/search/?unit_name=检索单位')
        self.assertContains(resp, '¥200.00')         # 今年记录可见
        self.assertContains(resp, '¥100.00')         # 无默认时间范围，历史记录也显示

    def test_deposit_explicit_date_from_excludes_old(self):
        recent = (date.today() - timedelta(days=10)).strftime('%Y-%m-%d')
        resp = self.client.get('/deposit/?unit_name=保证金单位&date_from=%s' % recent)
        self.assertContains(resp, '¥200.00')
        self.assertNotContains(resp, '¥100.00')

    def test_global_search_explicit_date_from_excludes_old(self):
        recent = (date.today() - timedelta(days=10)).strftime('%Y-%m-%d')
        resp = self.client.get('/search/?unit_name=检索单位&date_from=%s' % recent)
        self.assertContains(resp, '¥200.00')
        self.assertNotContains(resp, '¥100.00')


class DrillDownTest(TestCase):
    def test_collection_links_to_statement(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        dd = User.objects.create_user('dd', password='x')
        dd.groups.add(g)
        CustomerSalesperson.objects.create(unit_name='穿透客户', salesperson='张三', is_current=True)
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        MasterRecord.objects.create(unit_name='穿透客户', source_type='sales_invoice',
                                     source_upload=up, handler='张三', occur_date=date.today(),
                                     biz_type='发票', year=date.today().year,
                                     period=date.today().strftime('%Y%m'), debit=100, credit=0)
        c = Client()
        c.login(username='dd', password='x')
        resp = c.get('/collection/')
        self.assertContains(resp, 'statement/?unit_name=')

    def test_deposit_links(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user('dep', password='x')
        u.groups.add(g)
        CustomerSalesperson.objects.create(unit_name='穿透客户', salesperson='张三', is_current=True)
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        rec = MasterRecord.objects.create(unit_name='穿透客户', source_type='sales_invoice',
                                         invoice_number='INV-D-1', tag='保证金',
                                         source_upload=up, handler='张三', occur_date=date.today(),
                                         biz_type='发票', year=date.today().year,
                                         period=date.today().strftime('%Y%m'), debit=100, credit=0)
        c = Client()
        c.login(username='dep', password='x')
        resp = c.get('/deposit/?unit_name=穿透客户')
        self.assertContains(resp, 'statement/?unit_name=')
        self.assertContains(resp, f'/invoice/{rec.pk}/')
        self.assertContains(resp, '查看该发票货物明细')

    def test_reconcile_links(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user('rec', password='x')
        u.groups.add(g)
        CustomerSalesperson.objects.create(unit_name='穿透客户', salesperson='张三', is_current=True)
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        rec = MasterRecord.objects.create(unit_name='穿透客户', source_type='sales_invoice',
                                         invoice_number='INV-R-1',
                                         source_upload=up, handler='张三', occur_date=date.today(),
                                         biz_type='发票', year=date.today().year,
                                         period=date.today().strftime('%Y%m'), debit=100, credit=0)
        c = Client()
        c.login(username='rec', password='x')
        resp = c.get('/reconcile/?unit_name=穿透客户')
        self.assertContains(resp, f'/invoice/{rec.pk}/')
        self.assertContains(resp, '查看该发票货物明细')

    def test_customer_list_links(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user('cs', password='x')
        u.groups.add(g)
        CustomerSalesperson.objects.create(unit_name='穿透客户', salesperson='张三', is_current=True)
        c = Client()
        c.login(username='cs', password='x')
        resp = c.get('/data/customer/')
        self.assertContains(resp, 'statement/?unit_name=')


# ---------------------------------------------------------------------------
# 类别取发票状态 / 标记重设计（排除+内联编辑）/ 发票检索
# ---------------------------------------------------------------------------
class MarkAndInvoiceTest(TestCase):
    """类别取发票状态列；红冲/作废排除出往来与统计；查询页内联改标注；发票检索穿透。"""

    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        g = Group.objects.get_or_create(name='管理员')[0]
        self.admin = User.objects.create_user('admin', password='x')
        self.admin.groups.add(g)
        self.client = Client()
        self.client.login(username='admin', password='x')

    def _mk(self, **kw):
        defaults = dict(source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
                        biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                        debit=0, credit=0, category='', tag='', handler='', remark='',
                        tax_no='', invoice_number='')
        defaults.update(kw)
        return MasterRecord.objects.create(**defaults)

    def test_invoice_status_to_category(self):
        mappings = [
            ColumnMappingConfig(source_type='sales_invoice', source_column='发票状态',
                                target_field='category', is_active=True),
            ColumnMappingConfig(source_type='sales_invoice', source_column='购买方名称',
                                target_field='unit_name', is_active=True),
        ]
        for m in mappings:
            m.save()
        rec = _build_record('sales_invoice', {'发票状态': '正常', '购买方名称': '客户A'},
                            mappings, self.up, [])
        self.assertEqual(rec.category, '正常')
        # rule_category_default 不应覆盖已填的类别
        from books.imports.rules import apply_rule
        apply_rule('category_default', rec, {}, {'raw': {}, 'source_type': 'sales_invoice'})
        self.assertEqual(rec.category, '正常')

    def test_build_record_skips_void(self):
        # 作废发票在导入环节即被过滤，不生成主表记录
        mappings = [
            ColumnMappingConfig(source_type='sales_invoice', source_column='发票状态',
                                target_field='category', is_active=True),
            ColumnMappingConfig(source_type='sales_invoice', source_column='购买方名称',
                                target_field='unit_name', is_active=True),
        ]
        for m in mappings:
            m.save()

        # 用桩对象模拟 red_detect 规则（apply_rule 内部按 key 查注册表，使用默认参数）
        class _StubRule:
            rule_type = type('T', (), {'key': 'red_detect'})()
            params = {}

        rules = [_StubRule()]
        rec_ok = _build_record('sales_invoice', {'发票状态': '正常', '购买方名称': '客户A'},
                               mappings, self.up, rules, stats={})
        self.assertIsNotNone(rec_ok)
        rec_void = _build_record('sales_invoice', {'发票状态': '作废', '购买方名称': '客户A'},
                                 mappings, self.up, rules, stats={})
        self.assertIsNone(rec_void)  # 作废 不入库

    def test_collection_excludes_tagged(self):
        self._mk(debit=100, tag='', category='销售')
        self._mk(debit=50, tag='红冲', category='红冲')
        self._mk(debit=30, tag='作废', category='作废')
        base = MasterRecord.objects.all().exclude(tag__in=EXCLUDED_TAGS)
        a = _customer_aging('客户A', base, date(2099, 1, 1), 30)
        # 红冲 计入应收（保证统计完整），仅 作废 不计入
        self.assertEqual(a['balance'], 150.0)

    def test_statement_includes_red(self):
        self._mk(debit=100, tag='')
        self._mk(debit=50, tag='红冲')
        resp = self.client.get('/statement/?unit_name=%s' % '客户A')
        self.assertContains(resp, '100')
        self.assertContains(resp, '50')  # 红冲 默认展示并计入

    def test_statement_excludes_void(self):
        # 历史遗留的作废记录不在往来明细中展示
        self._mk(debit=100, tag='')
        self._mk(debit=30, tag='作废', category='作废')
        resp = self.client.get('/statement/?unit_name=%s' % '客户A')
        self.assertContains(resp, '100')
        # 作废行不展示/不计入；只校验表格数据行，避免误命中 licensing 试用横幅里的「30 天」
        rows_text = ''.join(resp.content.decode('utf-8', 'ignore').split('<tbody>')[1:])
        self.assertNotIn('30.00', rows_text)

    def test_statement_pagination(self):
        for i in range(5):
            self._mk(debit=10, tag='', occur_date=date(2024, 1, i + 1))
        # 该测试数据为历史日期，显式放宽起始日期以覆盖（默认近 1 年会过滤掉）
        resp = self.client.get('/statement/?unit_name=%s&per_page=2&date_from=2020-01-01' % '客户A')
        # 第一页仅 2 条明细
        self.assertContains(resp, '共 5 条')
        self.assertContains(resp, '每页')

    def test_record_tag_update_admin(self):
        m = self._mk(debit=10, tag='')
        resp = self.client.post(reverse('record_tag_update'),
                                {'next': '/statement/?unit_name=客户A', 'tag_%d' % m.id: '作废'})
        self.assertEqual(resp.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.tag, '作废')

    def test_record_tag_update_forbidden_for_viewer(self):
        g = Group.objects.get_or_create(name='查询员')[0]
        viewer = User.objects.create_user('viewer', password='x')
        viewer.groups.add(g)
        c = Client()
        c.login(username='viewer', password='x')
        m = self._mk(debit=10, tag='')
        resp = c.post(reverse('record_tag_update'), {'tag_%d' % m.id: '作废'})
        self.assertEqual(resp.status_code, 403)
        m.refresh_from_db()
        self.assertEqual(m.tag, '')  # 未改动

    def test_invoice_search(self):
        self._mk(debit=10, invoice_number='INV001', category='正常')
        self._mk(debit=20, invoice_number='INV002', category='作废', tag='作废')
        resp = self.client.get('/invoice/?invoice_no=INV001')
        self.assertContains(resp, 'INV001')
        self.assertContains(resp, '往来明细')  # 穿透链接
        # 默认仅发票来源；按全部来源可搜到另一条
        resp2 = self.client.get('/invoice/?invoice_no=INV002&all_sources=1')
        self.assertContains(resp2, 'INV002')

    def test_invoice_search_by_goods_name(self):
        inv = self._mk(debit=10, invoice_number='INV-A')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-A', goods_name='不锈钢板材',
                                       spec='2mm', unit='吨', quantity=50, price=12.5)
        other = self._mk(debit=20, invoice_number='INV-B')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=other,
                                       invoice_number='INV-B', goods_name='铝合金', spec='5mm')
        resp = self.client.get('/invoice/?goods_name=不锈钢')
        self.assertContains(resp, 'INV-A')
        self.assertNotContains(resp, 'INV-B')
        # 命中行展示货物名称、规格、数量与单价(不含税)
        self.assertContains(resp, '不锈钢板材')
        self.assertContains(resp, '×50')
        self.assertContains(resp, '单价(不含税)')
        self.assertContains(resp, '¥12.50')

    def test_invoice_search_by_spec(self):
        inv = self._mk(debit=10, invoice_number='INV-C')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-C', goods_name='板材', spec='10mm')
        resp = self.client.get('/invoice/?spec=10mm')
        self.assertContains(resp, 'INV-C')

    def test_invoice_search_by_name_and_spec(self):
        inv = self._mk(debit=10, invoice_number='INV-D')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-D', goods_name='钢材', spec='20mm')
        other = self._mk(debit=20, invoice_number='INV-E')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=other,
                                       invoice_number='INV-E', goods_name='钢材', spec='30mm')
        # 同名（钢材）不同规格，按 名称+规格 应精确区分
        resp = self.client.get('/invoice/?goods_name=钢材&spec=20mm')
        self.assertContains(resp, 'INV-D')
        self.assertNotContains(resp, 'INV-E')

    def test_invoice_search_name_no_match(self):
        inv = self._mk(debit=10, invoice_number='INV-F')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-F', goods_name='塑料', spec='1mm')
        resp = self.client.get('/invoice/?goods_name=不存在的物料')
        self.assertContains(resp, '无匹配发票记录')

    def test_invoice_search_tax_included_price(self):
        inv = self._mk(debit=10, invoice_number='INV-T')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-T', goods_name='含税料',
                                       quantity=10, amount=100, tax=13)
        # 勾选含税：单价(含税) = (金额+税额)/数量 = 113/10 = 11.30
        resp = self.client.get('/invoice/?goods_name=含税料&tax_included=1')
        self.assertContains(resp, 'INV-T')
        self.assertContains(resp, '单价(含税)')
        self.assertContains(resp, '¥11.30')
        # 不勾选时不显示含税单价
        resp2 = self.client.get('/invoice/?goods_name=含税料')
        self.assertNotContains(resp2, '单价(含税)')

    def test_invoice_search_export(self):
        import io, openpyxl
        inv = self._mk(debit=10, invoice_number='INV-X')
        InvoiceLineItem.objects.create(source_upload=self.up, master_record=inv,
                                       invoice_number='INV-X', goods_name='导出料',
                                       spec='S1', unit='吨', quantity=10, price=10, amount=100, tax=13)
        # 不勾含税
        resp = self.client.get('/invoice/export/?goods_name=导出料')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        text = '\n'.join(str(c.value) for row in wb.active.iter_rows() for c in row)
        self.assertIn('导出料', text)
        self.assertIn('单价(不含税)¥10.00', text)
        self.assertNotIn('单价(含税)', text)
        # 勾选含税：导出应包含 单价(含税)¥11.30
        resp2 = self.client.get('/invoice/export/?goods_name=导出料&tax_included=1')
        wb2 = openpyxl.load_workbook(io.BytesIO(resp2.content))
        text2 = '\n'.join(str(c.value) for row in wb2.active.iter_rows() for c in row)
        self.assertIn('单价(含税)¥11.30', text2)


class CleanVoidRecordsTest(TestCase):
    """clean_void_records 命令：清理作废主表及明细，保留红冲/正常记录。"""

    def test_clean_removes_void_keeps_others(self):
        from django.core.management import call_command
        from io import StringIO
        # 作废（tag）/ 空白作废（category）/ 红冲 / 正常 各建一条，并给作废配一条明细
        void = MasterRecord.objects.create(source_type='sales_invoice', source_upload_id=None,
                                            unit_name='客户A', biz_type='发票', year=2024, period='202401',
                                            occur_date=date(2024, 1, 1), debit=30, credit=0,
                                            tag='作废', category='作废', invoice_number='V1')
        blank = MasterRecord.objects.create(source_type='sales_invoice', source_upload_id=None,
                                            unit_name='客户A', biz_type='发票', year=2024, period='202401',
                                            occur_date=date(2024, 1, 2), debit=0, credit=5,
                                            tag='', category='空白作废', invoice_number='V2')
        red = MasterRecord.objects.create(source_type='sales_invoice', source_upload_id=None,
                                          unit_name='客户A', biz_type='发票', year=2024, period='202401',
                                          occur_date=date(2024, 1, 3), debit=50, credit=0,
                                          tag='红冲', category='红冲', invoice_number='V3')
        MasterRecord.objects.create(source_type='sales_invoice', source_upload_id=None,
                                    unit_name='客户A', biz_type='发票', year=2024, period='202401',
                                    occur_date=date(2024, 1, 4), debit=100, credit=0,
                                    tag='', category='销售', invoice_number='V4')
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        InvoiceLineItem.objects.create(source_upload=up, invoice_number='V1', goods_name='g', amount=30)

        out = StringIO()
        call_command('clean_void_records', '--apply', stdout=out)

        self.assertFalse(MasterRecord.objects.filter(tag='作废').exists())
        self.assertFalse(MasterRecord.objects.filter(category='空白作废').exists())
        self.assertFalse(InvoiceLineItem.objects.filter(master_record=void).exists())
        # 红冲与正常记录保留
        self.assertEqual(MasterRecord.objects.filter(tag='红冲').count(), 1)
        self.assertEqual(MasterRecord.objects.filter(category='销售').count(), 1)


class CleanDupInvoicesTest(TestCase):
    """clean_dup_invoices：清理「发票号为空且有同键(日期/单位/金额/摘要)有票号记录」的重复行。"""

    def _mk(self, inv, d, unit='客户A', credit=100, remark=''):
        return MasterRecord.objects.create(
            source_type='purchase_invoice', unit_name=unit, biz_type='发票',
            year=d.year, period=d.strftime('%Y%m'), occur_date=d,
            debit=0, credit=credit, remark=remark, invoice_number=inv,
            category='', tag='', handler='', tax_no='', blue_invoice_no='')

    def _call(self, **kw):
        import os
        import tempfile
        from io import StringIO
        kw.setdefault('backup', os.path.join(tempfile.mkdtemp(), 'dup.csv'))
        out = StringIO()
        call_command('clean_dup_invoices', stdout=out, **kw)
        return out.getvalue()

    def test_preview_keeps_everything(self):
        d = datetime.date(2026, 7, 10)
        self._mk('INV-1', d)
        dup = self._mk('', d)
        out = self._call()
        self.assertIn('预览模式', out)
        self.assertTrue(MasterRecord.objects.filter(pk=dup.pk).exists())

    def test_deletes_only_empty_no_twin(self):
        d = datetime.date(2026, 7, 10)
        keep = self._mk('INV-1', d)
        dup = self._mk('', d)
        lone = self._mk('', d, credit=999)          # 无同键的有票号记录 -> 保留
        self._call(apply=True)
        self.assertTrue(MasterRecord.objects.filter(pk=keep.pk).exists())
        self.assertTrue(MasterRecord.objects.filter(pk=lone.pk).exists())
        self.assertFalse(MasterRecord.objects.filter(pk=dup.pk).exists())

    def test_lines_reassigned_when_survivor_has_none(self):
        d = datetime.date(2026, 7, 11)
        keep = self._mk('INV-2', d)
        dup = self._mk('', d)
        up = SourceUpload.objects.create(source_type='purchase_invoice', file_name='t.xlsx',
                                         status='success')
        li = InvoiceLineItem.objects.create(source_upload=up, master_record=dup,
                                            invoice_number='INV-2', goods_name='过滤器')
        self._call(apply=True)
        li.refresh_from_db()
        self.assertEqual(li.master_record_id, keep.pk)

    def test_lines_deleted_when_survivor_already_has_lines(self):
        d = datetime.date(2026, 7, 12)
        keep = self._mk('INV-3', d)
        dup = self._mk('', d)
        up = SourceUpload.objects.create(source_type='purchase_invoice', file_name='t.xlsx',
                                         status='success')
        InvoiceLineItem.objects.create(source_upload=up, master_record=keep,
                                       invoice_number='INV-3', goods_name='过滤器')
        dup_line = InvoiceLineItem.objects.create(source_upload=up, master_record=dup,
                                                  invoice_number='INV-3', goods_name='过滤器')
        self._call(apply=True)
        self.assertFalse(InvoiceLineItem.objects.filter(pk=dup_line.pk).exists())
        self.assertEqual(InvoiceLineItem.objects.filter(master_record=keep).count(), 1)


class RelinkInvoiceDetailsTest(TestCase):
    """relink_invoice_details：把早期导入遗留的未挂明细按发票号补挂到主表。"""

    def _call(self, **kw):
        from io import StringIO
        out = StringIO()
        call_command('relink_invoice_details', stdout=out, **kw)
        return out.getvalue()

    def test_preview_then_apply(self):
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx',
                                         status='success')
        rec = MasterRecord.objects.create(
            source_type='sales_invoice', source_upload=up, unit_name='客户A', biz_type='发票',
            year=2026, period='202607', occur_date=datetime.date(2026, 7, 1),
            debit=100, credit=0, invoice_number='INV-1', category='', tag='', handler='',
            remark='', tax_no='', blue_invoice_no='')
        line = InvoiceLineItem.objects.create(source_upload=up, invoice_number='INV-1',
                                              goods_name='过滤器')
        stranger = InvoiceLineItem.objects.create(source_upload=up, invoice_number='NO-SUCH',
                                                  goods_name='找不到主表的行')
        out = self._call(source_type='sales_invoice')
        self.assertIn('预览模式', out)
        line.refresh_from_db()
        self.assertIsNone(line.master_record_id)

        self._call(source_type='sales_invoice', apply=True)
        line.refresh_from_db()
        stranger.refresh_from_db()
        self.assertEqual(line.master_record_id, rec.pk)
        self.assertIsNone(stranger.master_record_id)   # 库里没有同号主表，保持原样


class BackupDbCommandTest(TestCase):
    """backup_db 命令：在线一致性备份 + 完整性校验（基于真实临时文件）。"""

    @skipIf(connections['default'].vendor == 'postgresql',
            '该用例直接构造 SQLite 文件验证，PostgreSQL 引擎下跳过（CI 无 PG）')
    def test_online_copy_and_check(self):
        import os, tempfile, sqlite3
        from books.management.commands.backup_db import _online_copy, _integrity_ok
        from django.core.management import call_command
        from io import StringIO

        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, 'src.sqlite3')
        dst = os.path.join(tmp, 'dst.sqlite3')
        # 造一个有效库 + 一张表 + 若干行
        c = sqlite3.connect(src)
        c.execute('CREATE TABLE t(a INTEGER)')
        c.executemany('INSERT INTO t(a) VALUES (?)', [(i,) for i in range(50)])
        c.commit(); c.close()

        # 备份（在线一致性拷贝，backup 与 restore 共用此函数）
        _online_copy(src, dst)
        self.assertTrue(os.path.exists(dst))
        self.assertTrue(_integrity_ok(dst))
        self.assertEqual(sqlite3.connect(dst).execute('SELECT COUNT(*) FROM t').fetchone()[0], 50)

        # --check 选项能判定完整性
        out = StringIO()
        call_command('backup_db', '--check', dst, stdout=out)
        self.assertIn('OK', out.getvalue())


class BackupPruneTest(TestCase):
    """备份保留策略：只清理例行备份，安全备份与月度存档必须留存。"""

    def _mkdir_with(self, names):
        import os, tempfile
        tmp = tempfile.mkdtemp()
        for n in names:
            with open(os.path.join(tmp, n), 'wb') as f:
                f.write(b'x' * 1024)
        return tmp

    def test_keeps_latest_n_and_removes_rest(self):
        import os
        from books.management.commands.backup_db import _prune_backups

        names = [f'db-202608{d:02d}-120000.sqlite3' for d in range(1, 11)]
        tmp = self._mkdir_with(names)
        removed, freed = _prune_backups(tmp, keep=3, days=0, keep_newest_per_month=False)

        self.assertEqual(len(removed), 7)
        self.assertEqual(freed, 7 * 1024)
        # 保留的是时间戳最新的 3 份
        kept = sorted(set(names) - {os.path.basename(p) for p in removed})
        self.assertEqual(kept, ['db-20260808-120000.sqlite3',
                                'db-20260809-120000.sqlite3',
                                'db-20260810-120000.sqlite3'])

    def test_never_touches_safety_backups(self):
        """pre-restore-* 与 db.pre-*（破坏性操作前的退路）不得被例行清理删除。"""
        from books.management.commands.backup_db import _prune_backups

        safety = [
            'db.pre-sync-20260730_012714.sqlite3',
            'db.pre-rerun-20260730_012714.sqlite3',
            'db.pre-clear-remark-20260730_012714.sqlite3',
            'pre-restore-20260730-012714.sqlite3',
        ]
        routine = [f'db-202608{d:02d}-120000.sqlite3' for d in range(1, 6)]
        tmp = self._mkdir_with(safety + routine)

        removed, _ = _prune_backups(tmp, keep=1, days=0, keep_newest_per_month=False)
        removed_names = {p.rsplit('/', 1)[-1] for p in removed}
        # 4 份例行被清（保留最新 1 份），安全备份一份都不能少
        self.assertEqual(len(removed), 4)
        for name in safety:
            self.assertNotIn(name, removed_names)

    def test_keeps_newest_per_month_as_archive(self):
        """连续部署不应冲掉历史月份：每月最新一份作为月度存档保留。"""
        import os
        from books.management.commands.backup_db import _prune_backups

        names = [
            'db-20260615-120000.sqlite3', 'db-20260620-120000.sqlite3',  # 6 月
            'db-20260710-120000.sqlite3', 'db-20260715-120000.sqlite3',  # 7 月
            'db-20260801-120000.sqlite3', 'db-20260802-120000.sqlite3',  # 8 月
        ]
        tmp = self._mkdir_with(names)
        removed, _ = _prune_backups(tmp, keep=2, days=0)  # 最近 2 份即 8 月那两份
        kept = sorted(set(names) - {os.path.basename(p) for p in removed})

        # 8 月两份（keep=2）+ 6/7 月各自最新一份（月度存档）
        self.assertEqual(kept, ['db-20260620-120000.sqlite3',
                                'db-20260715-120000.sqlite3',
                                'db-20260801-120000.sqlite3',
                                'db-20260802-120000.sqlite3'])

    def test_dry_run_deletes_nothing(self):
        """--dry-run 只报告不删除；去掉后同样的调用才真正删除。"""
        import os
        from io import StringIO
        from books.management.commands.backup_db import Command

        names = [f'db-202608{d:02d}-120000.sqlite3' for d in range(1, 6)]
        tmp = self._mkdir_with(names)

        cmd = Command()
        cmd.stdout = StringIO()
        cmd._prune(tmp, keep=1, keep_days=0, dry_run=True)
        self.assertIn('将清理', cmd.stdout.getvalue())
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(tmp, n)), f'dry-run 不应删除 {n}')

        # 同一目录真跑一次：保留最新 1 份 + 8 月月度存档（同一份），其余删除
        cmd.stdout = StringIO()
        cmd._prune(tmp, keep=1, keep_days=0, dry_run=False)
        self.assertIn('已清理', cmd.stdout.getvalue())
        survivors = sorted(n for n in os.listdir(tmp))
        self.assertEqual(survivors, ['db-20260805-120000.sqlite3'])

    def test_keep_must_be_positive(self):
        from django.core.management.base import CommandError
        from django.core.management import call_command

        with self.assertRaises(CommandError):
            call_command('backup_db', '--keep', '0')

    def test_daily_tier_survives_dense_deploys(self):
        """密集部署不得冲掉历史天数：这正是引入按天分级的原因。

        构造 8/10 当天 30 次部署 + 前 5 天各 1 次。若只按"最近 20 份"，
        20 份全被当天占满，历史 5 天会被清空；分级后每天至少留一份。
        """
        import os
        from books.management.commands.backup_db import _prune_backups

        dense = [f'db-20260810-{h:02d}0000.sqlite3' for h in range(30)]
        history = [f'db-202608{d:02d}-120000.sqlite3' for d in range(5, 10)]
        tmp = self._mkdir_with(dense + history)

        removed, _ = _prune_backups(tmp, keep=20, days=14)
        kept = {os.path.basename(p) for p in set(dense + history)} - \
               {os.path.basename(p) for p in removed}

        # 历史 5 天一份都不能少
        for n in history:
            self.assertIn(n, kept, f'{n} 属最近14天，必须保留')
        # 当天保留最近 20 份，其余 10 份被清理
        self.assertEqual(len(removed), 10)

    def test_daily_tier_drops_beyond_window(self):
        """超出 keep-days 窗口的日份会被清理（仅月度存档幸免）。"""
        import os
        from books.management.commands.backup_db import _prune_backups

        # 基准日 8/20；8/01 与 8/02 已在 14 天窗口外（窗口起点 8/07）
        names = [f'db-202608{d:02d}-120000.sqlite3' for d in [1, 2, 7, 15, 20]]
        tmp = self._mkdir_with(names)

        removed, _ = _prune_backups(tmp, keep=1, days=14)
        removed_names = {os.path.basename(p) for p in removed}

        self.assertIn('db-20260802-120000.sqlite3', removed_names)
        self.assertNotIn('db-20260807-120000.sqlite3', removed_names)  # 窗口内
        self.assertNotIn('db-20260820-120000.sqlite3', removed_names)  # 最近1份
        # 8/01 是 8 月最早、但也是该月唯一候选存档？不是——存档取每月"最新"，
        # 即 8/20，故 8/01 应被清理
        self.assertIn('db-20260801-120000.sqlite3', removed_names)

    def test_window_anchored_to_newest_not_today(self):
        """长期未部署时，窗口以最新备份为基准，不应把全部历史判为过期。"""
        from books.management.commands.backup_db import _prune_backups

        # 全部是很久以前的备份（相对测试运行的"今天"已远超14天）
        names = [f'db-2020010{d}-120000.sqlite3' for d in range(1, 6)]
        tmp = self._mkdir_with(names)

        removed, _ = _prune_backups(tmp, keep=1, days=14)
        # 5 天都在"最新备份往前14天"内，全部保留
        self.assertEqual(removed, [])


class SalesPerfRedInkTest(TestCase):
    """业务员开票回款明细的 红冲 逻辑：开票额=销项发票净额（红冲按负数扣减），仅排除已作废。"""

    def setUp(self):
        self.g_admin = Group.objects.create(name='管理员')
        self.admin = User.objects.create_user('admin1', password='x')
        self.admin.groups.add(self.g_admin)
        self.upload = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        self.client = Client()

    def _mk_inv(self, unit_name, debit, tag='', category='销售', **kw):
        defaults = dict(
            source_type='sales_invoice', source_upload=self.upload, unit_name=unit_name,
            biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
            debit=debit, credit=0, category=category, tag=tag, handler='', remark='',
            tax_no='', invoice_number='', blue_invoice_no='')
        defaults.update(kw)
        return MasterRecord.objects.create(**defaults)

    def _mk_collect(self, unit_name, credit, category='贷方', **kw):
        # 回款来自非发票来源（如银行），避免被计入开票额
        defaults = dict(
            source_type='bank', source_upload=self.upload, unit_name=unit_name,
            biz_type='银行', year=2024, period='202401', occur_date=date(2024, 1, 1),
            debit=0, credit=credit, category=category, tag='', handler='', remark='',
            tax_no='', invoice_number='', blue_invoice_no='')
        defaults.update(kw)
        return MasterRecord.objects.create(**defaults)

    def _perf(self, unit_name):
        return _perf_for_customer(MasterRecord.objects.all(), unit_name)

    def test_invoice_includes_red_as_face_value(self):
        # 正常 1000 + 红冲 -300(按负数扣减) + 作废 200(排除)；回款 500
        self._mk_inv('客户A', 1000, tag='', category='销售')
        self._mk_inv('客户A', -300, tag='红冲', category='红冲')
        self._mk_inv('客户A', 200, tag='作废', category='作废')
        self._mk_collect('客户A', 500, category='贷方')
        a = self._perf('客户A')
        self.assertEqual(a['invoice'], 700.0)    # 1000 - 300
        self.assertEqual(a['collect'], 500.0)
        self.assertEqual(a['unpaid'], 200.0)     # 700 - 500

    def test_void_excluded_from_invoice(self):
        self._mk_inv('客户B', 1000, tag='', category='销售')
        self._mk_inv('客户B', 500, tag='作废', category='作废')
        self.assertEqual(self._perf('客户B')['invoice'], 1000.0)

    def test_no_red_equals_normal(self):
        self._mk_inv('客户C', 1000, tag='', category='销售')
        self.assertEqual(self._perf('客户C')['invoice'], 1000.0)

    def test_red_only_counts_face_value(self):
        # 仅一张红字发票：开票额按负数扣减（净额口径）
        self._mk_inv('客户D', -300, tag='红冲', category='红冲')
        self.assertEqual(self._perf('客户D')['invoice'], -300.0)


class SalesPerfApiRedInkTest(TestCase):
    """业务员开票回款明细 HTTP 接口：覆盖红冲场景（开票额=净额，红冲按负数扣减），含查询参数与导出解析。"""

    def setUp(self):
        self.g_admin = Group.objects.create(name='管理员')
        self.admin = User.objects.create_user('admin1', password='x')
        self.admin.groups.add(self.g_admin)
        self.upload = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        self.client = Client()
        # 张三：客户E(正常1000+红冲-300,回款500) + 客户F(正常500+红冲-200,回款300)
        # 李四：客户G(正常800,回款0)
        inv = [('客户E', 1000, '', '销售'), ('客户E', -300, '红冲', '红冲'),
               ('客户F', 500, '', '销售'), ('客户F', -200, '红冲', '红冲'),
               ('客户G', 800, '', '销售')]
        for u, d, tag, cat in inv:
            MasterRecord.objects.create(source_type='sales_invoice', source_upload=self.upload,
                unit_name=u, biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=d, credit=0, category=cat, tag=tag, handler='', remark='',
                tax_no='', invoice_number='', blue_invoice_no='')
        for u, c in [('客户E', 500), ('客户F', 300), ('客户G', 0)]:
            MasterRecord.objects.create(source_type='bank', source_upload=self.upload,
                unit_name=u, biz_type='银行', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=0, credit=c, category='贷方', tag='', handler='', remark='',
                tax_no='', invoice_number='', blue_invoice_no='')
        CustomerSalesperson.objects.create(unit_name='客户E', salesperson='张三', is_current=True)
        CustomerSalesperson.objects.create(unit_name='客户F', salesperson='张三', is_current=True)
        CustomerSalesperson.objects.create(unit_name='客户G', salesperson='李四', is_current=True)
        self.client.login(username='admin1', password='x')

    def test_page_invoice_includes_red(self):
        resp = self.client.get('/sales_perf/')
        self.assertEqual(resp.status_code, 200)
        # 张三 开票额=(1000-300)+(500-200)=1000；未回款=1000-(500+300)=200（货币格式带千分位）
        self.assertContains(resp, '1,000.00')
        self.assertContains(resp, '200.00')
        # 旧口径的红冲额/净开票 列头不应再出现
        self.assertNotContains(resp, '红冲额')
        self.assertNotContains(resp, '净开票')

    def test_page_currency_format(self):
        resp = self.client.get('/sales_perf/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '¥')            # 货币符号
        self.assertContains(resp, '1,000.00')     # 千分位格式

    def test_expand_shows_per_customer_red(self):
        resp = self.client.get('/sales_perf/?expand=1')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '客户')          # 客户列头
        self.assertContains(resp, '700.00')        # 客户E 净额 1000-300
        self.assertContains(resp, '300.00')        # 客户F 净额 500-200
        self.assertContains(resp, '800.00')        # 客户G 800

    def test_salesperson_filter(self):
        resp = self.client.get('/sales_perf/?salesperson=张三')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '1,000.00')       # 张三合计开票额
        self.assertContains(resp, '200.00')       # 张三未回款
        self.assertNotContains(resp, '1,300.00')    # 客户E 仅在展开时出现，按业务员汇总后不单列

    def test_year_filter_excludes_other_years(self):
        # 2023 年无数据：按业务员仍有一行，但开票额为 0（不含 2024 的红冲数据）
        resp = self.client.get('/sales_perf/?year=2023')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '0.00')
        self.assertNotContains(resp, '2,000.00')
        resp2 = self.client.get('/sales_perf/?year=2024')
        self.assertContains(resp2, '1,000.00')

    def test_expand_customer_links_to_statement(self):
        # 展开到客户时，客户名应为穿透到「客户往来明细」的链接（携带 unit_name/year/back）
        resp = self.client.get('/sales_perf/?expand=1')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'href="/statement/?unit_name=')
        self.assertContains(resp, 'back=')
        self.assertContains(resp, '>客户E<')   # 链接文本（可见客户名）

    def test_drill_through_to_statement_filters_customer(self):
        # 点击穿透链接后，客户往来明细按该客户过滤并展示数据
        resp = self.client.get('/statement/', {'unit_name': '客户E', 'back': '/sales_perf/?expand=1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '单位：')
        self.assertContains(resp, '客户E')                 # 已定位到该客户
        self.assertContains(resp, '← 返回')               # 带 back 参数时显示返回按钮

    def test_statement_no_back_button_without_back_param(self):
        # 直接访问客户往来明细（无 back 参数）时不显示返回按钮
        resp = self.client.get('/statement/', {'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '← 返回')

    def test_customer_filter_non_expand(self):
        # 按客户筛选（不展开）：仅该客户数据，其余业务员/客户不计入合计
        resp = self.client.get('/sales_perf/', {'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')     # 客户E 净额 1000-300
        self.assertNotContains(resp, '2,000.00')   # 张三含客户F的合计不再出现
        self.assertNotContains(resp, '2,100.00')   # 李四(客户G) 未计入合计

    def test_customer_filter_expand(self):
        # 按客户筛选（展开到客户）：只剩该客户行
        resp = self.client.get('/sales_perf/', {'unit_name': '客户E', 'expand': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')        # 客户E 净额
        self.assertNotContains(resp, '300.00')     # 客户F 净额不出现
        self.assertNotContains(resp, '2,100.00')   # 客户G 未计入合计

    def test_customer_filter_no_match(self):
        # 筛选不存在的客户：无数据提示
        resp = self.client.get('/sales_perf/', {'unit_name': '无此客户'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '暂无数据')

    def test_export_respects_customer_filter(self):
        import io, openpyxl
        resp = self.client.get('/sales_perf/export/', {'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        nums = [round(c.value, 2) for c in ws[4] if isinstance(c.value, (int, float))]
        self.assertIn(700.0, nums)     # 客户E 净额 1000-300
        self.assertNotIn(2000.0, nums)  # 不含全部客户的合计

    def test_combined_year_and_customer(self):
        # 年份 + 客户：仅 2024 年的客户E
        resp = self.client.get('/sales_perf/', {'year': '2024', 'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')     # 客户E 净额 1000-300
        self.assertNotContains(resp, '2,000.00')   # 张三含客户F的合计不出现
        self.assertNotContains(resp, '2,100.00')   # 李四(客户G) 未计入

    def test_combined_salesperson_and_customer(self):
        # 业务员 + 客户：仅张三名下的客户E
        resp = self.client.get('/sales_perf/', {'salesperson': '张三', 'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')
        self.assertNotContains(resp, '300.00')     # 客户F 不出现
        self.assertNotContains(resp, '2,100.00')   # 客户G 不出现

    def test_combined_all_filters_expand(self):
        # 年份 + 业务员 + 客户 + 展开：四条件同时生效
        resp = self.client.get('/sales_perf/',
                               {'year': '2024', 'salesperson': '张三', 'unit_name': '客户E', 'expand': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')
        self.assertNotContains(resp, '300.00')     # 客户F 不出现
        self.assertNotContains(resp, '2,100.00')   # 客户G 未计入合计

    def test_combined_no_match_salesperson_customer(self):
        # 业务员与其客户不匹配：李四名下无客户E → 暂无数据
        resp = self.client.get('/sales_perf/', {'salesperson': '李四', 'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '暂无数据')

    def test_combined_year_customer_zero_result(self):
        # 年份与客户组合无数据命中：客户E 映射存在但 2023 年无记录 → 显示 0 行（非「暂无数据」）
        resp = self.client.get('/sales_perf/', {'year': '2023', 'unit_name': '客户E'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '0.00')
        self.assertNotContains(resp, '700.00')

    def test_combined_export_respects_all_filters(self):
        import io, openpyxl
        resp = self.client.get('/sales_perf/export/',
                               {'year': '2024', 'salesperson': '张三', 'unit_name': '客户E', 'expand': '1'})
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        nums = [round(c.value, 2) for c in ws[4] if isinstance(c.value, (int, float))]
        self.assertIn(700.0, nums)   # 客户E 净额 1000-300
        self.assertNotIn(300.0, nums)  # 客户F 不计入

    def test_salesperson_filter_with_year(self):
        # 业务员 + 年份
        resp = self.client.get('/sales_perf/', {'salesperson': '张三', 'year': '2024'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '1,000.00')     # 张三合计开票额
        self.assertContains(resp, '200.00')     # 张三未回款合计
        self.assertNotContains(resp, '2,800.00')   # 李四未被计入合计

    def test_salesperson_filter_with_expand(self):
        # 业务员 + 展开到客户：仅张三名下客户E/F
        resp = self.client.get('/sales_perf/', {'salesperson': '张三', 'expand': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')       # 客户E 净额
        self.assertContains(resp, '300.00')       # 客户F 净额
        self.assertContains(resp, '1,000.00')     # 张三合计
        self.assertNotContains(resp, '2,800.00')   # 李四未计入

    def test_salesperson_filter_with_year_and_expand(self):
        # 业务员 + 年份 + 展开：三条件同时生效
        resp = self.client.get('/sales_perf/', {'salesperson': '张三', 'year': '2024', 'expand': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '700.00')
        self.assertContains(resp, '300.00')
        self.assertNotContains(resp, '2,800.00')

    def test_salesperson_filter_year_no_data(self):
        # 业务员 + 无数据年份：显示 0 行
        resp = self.client.get('/sales_perf/', {'salesperson': '张三', 'year': '2023'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '0.00')
        self.assertNotContains(resp, '2,000.00')

    def test_return_button_links_back_to_sales_perf(self):
        # 返回按钮的 href 指向穿透来源页（业务员回款明细）
        resp = self.client.get('/statement/', {'unit_name': '客户E', 'back': '/sales_perf/?expand=1'})
        self.assertContains(resp, 'href="/sales_perf/?expand=1"')

    def test_export_xlsx_values(self):
        import io, openpyxl
        resp = self.client.get('/sales_perf/export/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        headers = [c.value for c in ws[3]]          # 第 3 行为表头
        self.assertIn('开票额', headers)
        self.assertIn('回款额', headers)
        self.assertIn('未回款额', headers)
        self.assertNotIn('红冲额', headers)
        self.assertNotIn('净开票', headers)
        # 数据行：张三 开票额=1000（红冲按负数扣减）
        nums = [round(v, 2) for v in (c.value for c in ws[4]) if isinstance(v, (int, float))]
        self.assertIn(1000.0, nums)


class SalesPerfExportTest(TestCase):
    """业务员开票回款明细 导出逻辑（sales_perf_export）单元测试：表头/数值/合计/展开/红冲。"""

    def setUp(self):
        self.g_admin = Group.objects.create(name='管理员')
        self.admin = User.objects.create_user('admin1', password='x')
        self.admin.groups.add(self.g_admin)
        self.factory = RequestFactory()
        self.upload = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        # 张三：客户E(正常1000+红冲-300,回款500) + 客户F(正常500+红冲-200,回款300)
        # 李四：客户G(正常800,回款0)
        inv = [('客户E', 1000, '', '销售'), ('客户E', -300, '红冲', '红冲'),
               ('客户F', 500, '', '销售'), ('客户F', -200, '红冲', '红冲'),
               ('客户G', 800, '', '销售')]
        for u, d, tag, cat in inv:
            MasterRecord.objects.create(source_type='sales_invoice', source_upload=self.upload,
                unit_name=u, biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=d, credit=0, category=cat, tag=tag, handler='', remark='',
                tax_no='', invoice_number='', blue_invoice_no='')
        for u, c in [('客户E', 500), ('客户F', 300), ('客户G', 0)]:
            MasterRecord.objects.create(source_type='bank', source_upload=self.upload,
                unit_name=u, biz_type='银行', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=0, credit=c, category='贷方', tag='', handler='', remark='',
                tax_no='', invoice_number='', blue_invoice_no='')
        CustomerSalesperson.objects.create(unit_name='客户E', salesperson='张三', is_current=True)
        CustomerSalesperson.objects.create(unit_name='客户F', salesperson='张三', is_current=True)
        CustomerSalesperson.objects.create(unit_name='客户G', salesperson='李四', is_current=True)

    def _export(self, params=''):
        import io, openpyxl
        req = self.factory.get('/sales_perf/export/' + params)
        req.user = self.admin
        resp = sales_perf_export(req)
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        return resp, wb.active

    def _row_nums(self, ws, row):
        return [round(c.value, 2) for c in ws[row] if isinstance(c.value, (int, float))]

    def test_headers_and_no_red_columns(self):
        resp, ws = self._export()
        headers = [c.value for c in ws[3]]          # 第 3 行为表头
        self.assertIn('开票额', headers)
        self.assertIn('回款额', headers)
        self.assertIn('未回款额', headers)
        self.assertNotIn('红冲额', headers)
        self.assertNotIn('净开票', headers)
        self.assertEqual(resp['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        # 文件名按 RFC2231 编码，解码后校验
        from email.header import decode_header
        decoded = ''
        for frag, enc in decode_header(resp['Content-Disposition']):
            if isinstance(frag, bytes):
                frag = frag.decode(enc or 'utf-8')
            decoded += frag
        self.assertIn('业务员开票回款明细.xlsx', decoded)

    def test_export_currency_number_format(self):
        resp, ws = self._export()
        # 非展开：第2/3/4列（开票额/回款额/未回款额）单元格为货币格式
        self.assertEqual(ws.cell(4, 2).number_format, '¥#,##0.00')
        self.assertEqual(ws.cell(4, 3).number_format, '¥#,##0.00')
        self.assertEqual(ws.cell(4, 4).number_format, '¥#,##0.00')

    def test_values_include_red(self):
        resp, ws = self._export()
        # 张三 行（第4行）：开票额=1000(红冲按负数扣减)，回款=800，未回款=200
        self.assertIn(1000.0, self._row_nums(ws, 4))
        self.assertIn(800.0, self._row_nums(ws, 4))
        self.assertIn(200.0, self._row_nums(ws, 4))
        # 李四 行（第5行）：开票额=800
        self.assertIn(800.0, self._row_nums(ws, 5))

    def test_totals_row(self):
        resp, ws = self._export()
        last = [c.value for c in ws[ws.max_row]]
        self.assertEqual(last[0], '合计')
        nums = self._row_nums(ws, ws.max_row)
        # 开票额=1000+800=1800；回款=800；未回款=1000
        self.assertIn(1800.0, nums)
        self.assertIn(800.0, nums)
        self.assertIn(1000.0, nums)

    def test_expand_includes_customer_column(self):
        resp, ws = self._export('?expand=1')
        headers = [c.value for c in ws[3]]
        self.assertIn('客户', headers)              # 展开后多一列客户
        vals = []
        for row in ws.iter_rows(min_row=4):
            vals.extend(self._row_nums(ws, row[0].row))
        # 客户级：客户E=700, 客户F=300, 客户G=800
        self.assertIn(700.0, vals)
        self.assertIn(300.0, vals)
        self.assertIn(800.0, vals)


class _RedDetectRule:
    """最小的规则桩：让 _build_record 应用 red_detect。"""
    rule_type = type('T', (), {'key': 'red_detect'})()
    params = {}


class ImportRedInkTest(TestCase):
    """导入逻辑：红冲/作废识别（rule_red_detect）+ _build_record 过滤/保留，承接开票额口径。"""

    def setUp(self):
        self.upload = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        ColumnMappingConfig.objects.create(source_type='sales_invoice', source_column='单位名称',
                                            target_field='unit_name', transform='none', is_active=True)
        ColumnMappingConfig.objects.create(source_type='sales_invoice', source_column='金额',
                                            target_field='debit', transform='none', is_active=True)
        ColumnMappingConfig.objects.create(source_type='sales_invoice', source_column='备注',
                                            target_field='remark', transform='none', is_active=True)
        self.mappings = list(ColumnMappingConfig.objects.filter(source_type='sales_invoice', is_active=True))

    def _detect(self, raw):
        rec = MasterRecord(source_type='sales_invoice', unit_name='X',
                            biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                            debit=100, credit=0, category='', tag='', handler='', remark='',
                            tax_no='', invoice_number='', blue_invoice_no='')
        rule_red_detect(rec, {}, {'raw': raw})
        return rec

    def test_remark_red_keywords(self):
        for kw in ('红冲', '红字', '负数'):
            rec = self._detect({'备注': '%s发票' % kw, '发票状态': '正常', '是否正数发票': '是'})
            self.assertEqual(rec.tag, '红冲', kw)

    def test_void_by_status(self):
        rec = self._detect({'备注': '', '发票状态': '作废', '是否正数发票': '是'})
        self.assertEqual(rec.tag, '作废')

    def test_red_by_negate_flag(self):
        # 备注重无红冲关键字，但「是否正数发票=否」识别为红冲
        rec = self._detect({'备注': '', '发票状态': '正常', '是否正数发票': '否'})
        self.assertEqual(rec.tag, '红冲')

    def test_normal_no_tag(self):
        rec = self._detect({'备注': '正常开票', '发票状态': '正常', '是否正数发票': '是'})
        self.assertEqual(rec.tag, '')

    def test_blue_invoice_no_parsed(self):
        rec = self._detect({'备注': '红冲 对应蓝字发票号码 12345678', '发票状态': '正常', '是否正数发票': '是'})
        self.assertEqual(rec.tag, '红冲')
        self.assertEqual(rec.blue_invoice_no, '12345678')

    def test_build_record_keeps_red_drops_void(self):
        red = _build_record('sales_invoice',
                            {'单位名称': '客户A', '金额': -300, '备注': '红冲', '发票状态': '正常', '是否正数发票': '是'},
                            self.mappings, self.upload, [_RedDetectRule()])
        self.assertIsNotNone(red)
        self.assertEqual(red.tag, '红冲')
        void = _build_record('sales_invoice',
                             {'单位名称': '客户A', '金额': 200, '备注': '', '发票状态': '作废', '是否正数发票': '是'},
                             self.mappings, self.upload, [_RedDetectRule()])
        self.assertIsNone(void)   # 作废在导入环节即过滤，不入库

    def test_import_feeds_invoice_calc(self):
        # 模拟导入：正常1000 + 红冲-300 + 作废200；仅前两条入库
        for raw in [
            {'单位名称': '客户A', '金额': 1000, '备注': '', '发票状态': '正常', '是否正数发票': '是'},
            {'单位名称': '客户A', '金额': -300, '备注': '红冲', '发票状态': '正常', '是否正数发票': '是'},
            {'单位名称': '客户A', '金额': 200, '备注': '', '发票状态': '作废', '是否正数发票': '是'},
        ]:
            rec = _build_record('sales_invoice', raw, self.mappings, self.upload, [_RedDetectRule()])
            if rec is not None:
                rec.save()
        a = _perf_for_customer(MasterRecord.objects.all(), '客户A')
        self.assertEqual(a['invoice'], 700.0)    # 1000 - 300（红冲按负数扣减，作废排除）
        self.assertEqual(a['unpaid'], 700.0)     # 无回款


class InvoiceGoodsDetailLinkTest(TestCase):
    """数电发票的货物明细挂接。

    数电发票导出模板里「发票号码」列常整列为空，号码只在「数电发票号码」列；
    且重复导入同一文件时主表全被判重跳过（新增 0 行）。这两种情况下明细都必须
    仍挂到主表上，否则穿透页与「按货物名称检索」都看不到货物明细。
    """

    INV = '26422000002750576611'

    def setUp(self):
        for col, target, order in (('购方名称', 'unit_name', 0), ('价税合计', 'debit', 1)):
            ColumnMappingConfig.objects.create(source_type='sales_invoice', source_column=col,
                                               target_field=target, transform='none',
                                               is_active=True, order=order)

    def _file(self):
        """发票基础信息(主表) + 信息汇总表(货物明细)，且「发票号码」列为空。"""
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        base = wb.active
        base.title = '发票基础信息'
        base.append(['购方名称', '发票号码', '数电发票号码', '价税合计'])
        base.append(['客户A', None, self.INV, 1130])
        det = wb.create_sheet('信息汇总表')
        det.append(['数电发票号码', '货物或应税劳务名称', '金额', '税额'])
        det.append([self.INV, '初效过滤器', 1000, 130])
        det.append([self.INV, '中效过滤器', 1000, 130])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 't.xlsx'
        return buf

    def _user(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user('gd', password='x')
        u.groups.add(g)
        return u

    def test_invoice_no_falls_back_to_digital_no(self):
        up = run_import('sales_invoice', self._file(), self._user())
        rec = MasterRecord.objects.get(source_upload=up)
        self.assertEqual(rec.invoice_number, self.INV)

    def test_detail_lines_linked_when_master_skipped(self):
        u = self._user()
        run_import('sales_invoice', self._file(), u)
        up2 = run_import('sales_invoice', self._file(), u)   # 同一文件再导：主表全部判重
        self.assertEqual(up2.row_count, 0)
        lines = InvoiceLineItem.objects.filter(source_upload=up2)
        self.assertEqual(lines.count(), 2)
        self.assertEqual(lines.exclude(master_record__isnull=True).count(), 2)

    def test_digital_no_with_blank_cell_stays_exact(self):
        """数电票号列含空单元格时 pandas 会按 float 读（20 位精度丢失）。

        读表时必须按文本读取，否则主表票号变成 2.6422000002354455e+19，
        与明细表的 20 位号码对不上，货物明细挂不上。
        """
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        base = wb.active
        base.title = '发票基础信息'
        base.append(['购方名称', '发票号码', '数电发票号码', '价税合计'])
        base.append(['客户A', None, self.INV, 1130])
        base.append(['客户B', None, None, 220])       # 空票号 -> 该列被 pandas 读成 float
        det = wb.create_sheet('信息汇总表')
        det.append(['数电发票号码', '货物或应税劳务名称', '金额', '税额'])
        det.append([self.INV, '初效过滤器', 1000, 130])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 't.xlsx'

        up = run_import('sales_invoice', buf, self._user())
        rec = MasterRecord.objects.get(source_upload=up, invoice_number=self.INV)
        self.assertEqual(rec.invoice_number, self.INV)
        lines = InvoiceLineItem.objects.filter(source_upload=up)
        self.assertEqual(lines.count(), 1)
        self.assertEqual(lines.exclude(master_record__isnull=True).count(), 1)

    def test_invoice_no_prefers_digital_over_short_no(self):
        """主表与明细取号顺序必须一致：有数电号就用数电号，没有才用旧式发票号码。"""
        ColumnMappingConfig.objects.create(source_type='sales_invoice', source_column='发票号码',
                                           target_field='invoice_number', transform='none',
                                           is_active=True, order=0)
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        base = wb.active
        base.title = '发票基础信息'
        base.append(['购方名称', '发票号码', '数电发票号码', '价税合计'])
        base.append(['客户A', '4461279', self.INV, 1130])
        base.append(['客户B', '4461280', None, 220])     # 无数电号 -> 用旧式发票号码
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 't.xlsx'

        up = run_import('sales_invoice', buf, self._user())
        self.assertTrue(MasterRecord.objects.filter(source_upload=up,
                                                    invoice_number=self.INV).exists())
        self.assertTrue(MasterRecord.objects.filter(source_upload=up,
                                                    invoice_number='4461280').exists())

    def test_index_accepts_both_short_and_digital_no(self):
        """历史主表存的是短号、明细用数电号时，索引两边都收录，仍能挂上。"""
        import io
        import openpyxl
        import pandas as pd
        from books.imports.engine import _import_detail_sheets
        up = SourceUpload.objects.create(source_type='sales_invoice', file_name='a.xlsx',
                                         status='success')
        raw = RawImportRow.objects.create(source_upload=up, data={'发票号码': '4461279',
                                                                  '数电发票号码': self.INV})
        rec = MasterRecord.objects.create(
            source_type='sales_invoice', source_upload=up, raw_row=raw, unit_name='客户A',
            biz_type='发票', year=2026, period='202607', occur_date=datetime.date(2026, 7, 1),
            debit=1130, credit=0, invoice_number='4461279', category='', tag='', handler='',
            remark='', tax_no='', blue_invoice_no='')
        det_up = SourceUpload.objects.create(source_type='sales_invoice', file_name='b.xlsx',
                                             status='success')
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '信息汇总表'
        ws.append(['数电发票号码', '货物或应税劳务名称', '金额', '税额'])
        ws.append([self.INV, '初效过滤器', 1000, 130])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        _import_detail_sheets(pd.ExcelFile(buf, engine='openpyxl'), det_up)
        lines = InvoiceLineItem.objects.filter(source_upload=det_up)
        self.assertEqual(lines.count(), 1)
        self.assertEqual(lines.first().master_record_id, rec.pk)

    def test_detail_page_finds_lines_from_other_upload(self):
        self._user()
        old_up = SourceUpload.objects.create(source_type='sales_invoice', file_name='a.xlsx',
                                             status='success')
        rec = MasterRecord.objects.create(
            source_type='sales_invoice', source_upload=old_up, unit_name='客户A', biz_type='发票',
            year=2026, period='202607', occur_date=datetime.date(2026, 7, 1), debit=1130, credit=0,
            invoice_number=self.INV, category='', tag='', handler='', remark='', tax_no='',
            blue_invoice_no='')
        new_up = SourceUpload.objects.create(source_type='sales_invoice', file_name='b.xlsx',
                                             status='success')
        InvoiceLineItem.objects.create(source_upload=new_up, invoice_number=self.INV,
                                       goods_name='初效过滤器', amount=1000, tax=130)
        c = Client()
        c.login(username='gd', password='x')
        resp = c.get(f'/invoice/{rec.pk}/')
        self.assertContains(resp, '初效过滤器')


from decimal import Decimal


def _xlsx(rows, name='t.xlsx'):
    """用列名->值的 dict 列表生成内存 xlsx 文件对象，供 run_import 直接读取。"""
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = list(rows[0].keys())
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = name
    return buf


class AcceptanceImportTest(TestCase):
    """承兑/迪链导入：列映射与借贷方向规则由迁移 0016 播种，此处走真实 run_import 全链路。

    口径：签收(收款)=贷方，申请/转单(付款)=借方；迪链行打 tag=迪链 以便与普通承兑区分。
    """

    def setUp(self):
        self.user = User.objects.create_user('imp', password='x')

    def test_acceptance_direction_by_operation_type(self):
        up = run_import('acceptance', _xlsx([
            {'交易对手名称': '甲公司', '交易日期': '2025-03-01', '交易金额（元）': 1000,
             '操作类型': '签收', '处理结果': '票交所处理成功', '备注': '收票'},
            {'交易对手名称': '乙公司', '交易日期': '2025-03-02', '交易金额（元）': 2000,
             '操作类型': '申请', '处理结果': '票交所处理成功', '备注': '付票'},
        ]), self.user)
        self.assertEqual(up.status, 'success')
        self.assertEqual(up.row_count, 2)

        recv = MasterRecord.objects.get(unit_name='甲公司')
        self.assertEqual(recv.category, '签收')
        self.assertEqual(recv.credit, Decimal('1000'))   # 签收(收款) 记贷方
        self.assertEqual(recv.debit, Decimal('0'))
        self.assertEqual(recv.occur_date, date(2025, 3, 1))
        self.assertEqual(recv.biz_type, '承兑')

        pay = MasterRecord.objects.get(unit_name='乙公司')
        self.assertEqual(pay.category, '申请')
        self.assertEqual(pay.debit, Decimal('2000'))     # 申请(付款) 记借方
        self.assertEqual(pay.credit, Decimal('0'))

    def test_failed_transactions_filtered(self):
        """票交所处理失败/拒绝签收/已撤回并未真实发生，不应入库虚增借方。"""
        up = run_import('acceptance', _xlsx([
            {'交易对手名称': '甲公司', '交易日期': '2025-03-01', '交易金额（元）': 1000,
             '操作类型': '申请', '处理结果': '票交所处理成功', '备注': ''},
            {'交易对手名称': '乙公司', '交易日期': '2025-03-02', '交易金额（元）': 2000,
             '操作类型': '申请', '处理结果': '票交所处理失败', '备注': ''},
            {'交易对手名称': '丙公司', '交易日期': '2025-03-03', '交易金额（元）': 3000,
             '操作类型': '申请', '处理结果': '票交所处理成功，对方拒绝签收', '备注': ''},
            {'交易对手名称': '丁公司', '交易日期': '2025-03-04', '交易金额（元）': 4000,
             '操作类型': '申请', '处理结果': '票交所处理成功，已撤回', '备注': ''},
        ]), self.user)
        self.assertEqual(up.row_count, 1)
        self.assertEqual(list(MasterRecord.objects.values_list('unit_name', flat=True)), ['甲公司'])

    def test_dilian_import_tag_and_direction(self):
        up = run_import('acceptance', _xlsx([
            {'类别': '迪链', '交易时间': '2025-04-11', '分类': '签收', '借方': 0,
             '贷方': 27397.74, '单位名称': '净森环保', '业务员': '邓雪玲'},
            {'类别': '迪链', '交易时间': '2025-04-28', '分类': '转单 ', '借方': 27397.74,
             '贷方': 0, '单位名称': '捷佳净化', '业务员': None},
        ]), self.user)
        self.assertEqual(up.status, 'success')
        self.assertEqual(up.row_count, 2)

        recv = MasterRecord.objects.get(unit_name='净森环保')
        self.assertEqual(recv.tag, '迪链')            # 用 tag 与普通承兑区分
        self.assertEqual(recv.category, '签收')
        self.assertEqual(recv.credit, Decimal('27397.74'))
        self.assertEqual(recv.debit, Decimal('0'))
        self.assertEqual(recv.handler, '邓雪玲')
        self.assertEqual(recv.occur_date, date(2025, 4, 11))

        pay = MasterRecord.objects.get(unit_name='捷佳净化')
        self.assertEqual(pay.tag, '迪链')
        self.assertEqual(pay.category, '转单')         # 尾随空格已被 trim 算子清掉
        self.assertEqual(pay.debit, Decimal('27397.74'))
        self.assertEqual(pay.credit, Decimal('0'))

    def test_plain_acceptance_not_tagged_dilian(self):
        run_import('acceptance', _xlsx([
            {'交易对手名称': '甲公司', '交易日期': '2025-03-01', '交易金额（元）': 1000,
             '操作类型': '签收', '处理结果': '票交所处理成功', '备注': ''},
        ]), self.user)
        self.assertEqual(MasterRecord.objects.get(unit_name='甲公司').tag, '')

    def test_package_no_extracted_on_import(self):
        """票据包号码列应在导入时写入主表 package_no（此前漏赋值，仅迁移回填有值）。"""
        up = run_import('acceptance', _xlsx([
            {'交易对手名称': '丙公司', '交易日期': '2025-05-01', '交易金额（元）': 500,
             '操作类型': '签收', '处理结果': '票交所处理成功', '备注': '',
             '票据包号码': 'PKGTEST001'},
        ]), self.user)
        self.assertEqual(up.status, 'success')
        rec = MasterRecord.objects.get(unit_name='丙公司')
        self.assertEqual(rec.package_no, 'PKGTEST001')

    def test_same_amount_same_day_different_package_all_imported(self):
        """同日同对手同金额的多张票，票据包号不同即属不同票据，不能判重丢票。"""
        rows = [
            {'交易对手名称': '中部硅业', '交易日期': '2024-10-11', '交易金额（元）': 50000,
             '操作类型': '签收', '处理结果': '票交所处理成功', '备注': '',
             '票据包号码': f'PKG{i}'}
            for i in range(1, 5)
        ]
        up = run_import('acceptance', _xlsx(rows), self.user)
        self.assertEqual(up.row_count, 4)
        self.assertEqual(MasterRecord.objects.filter(unit_name='中部硅业').count(), 4)
        self.assertEqual(
            sorted(MasterRecord.objects.filter(unit_name='中部硅业')
                   .values_list('package_no', flat=True)),
            ['PKG1', 'PKG2', 'PKG3', 'PKG4'])


class BankImportDedupTest(TestCase):
    """银行流水导入：重叠导入应跳过已存在的重复记录，而非再次入库。"""

    def setUp(self):
        self.user = User.objects.create_user('bk', password='x')
        # 测试库未加载 fixtures，需自行配置 bank 列映射
        for i, (col, field) in enumerate([
            ('对方户名', 'unit_name'), ('交易日期', 'occur_date'),
            ('借方金额', 'debit'), ('贷方金额', 'credit'), ('摘要', 'remark')]):
            ColumnMappingConfig.objects.create(
                source_type='bank', source_column=col, target_field=field,
                transform='none', is_active=True, order=i)
        # 模拟“旧全量导入”已存在的记录
        up_old = SourceUpload.objects.create(
            source_type='bank', file_name='old.xlsx', status='success')
        MasterRecord.objects.create(
            source_type='bank', source_upload=up_old, unit_name='甲公司',
            occur_date=date(2026, 6, 1), year=2026, period='202606',
            debit=Decimal('0'), credit=Decimal('1900'),
            remark='货款', biz_type='银行')
        MasterRecord.objects.create(
            source_type='bank', source_upload=up_old, unit_name='乙公司',
            occur_date=date(2026, 7, 10), year=2026, period='202607',
            debit=Decimal('0'), credit=Decimal('500'),
            remark='货款', biz_type='银行')

    def test_personal_bank_tag_applied(self):
        """选「个人」导入时，整批应打 tag='个人'，且 biz_type 仍为银行。"""
        up = run_import('bank', _xlsx([
            {'对方户名': '丁公司', '交易日期': '2026-08-01', '借方金额': 0,
             '贷方金额': 800, '摘要': '退款'},
        ]), self.user, tag='个人')
        self.assertEqual(up.status, 'success')
        rec = MasterRecord.objects.get(unit_name='丁公司')
        self.assertEqual(rec.tag, '个人')
        self.assertEqual(rec.biz_type, '银行')

    def test_default_import_no_tag(self):
        """不选账户性质（对公）时，tag 保持空。"""
        up = run_import('bank', _xlsx([
            {'对方户名': '己公司', '交易日期': '2026-08-02', '借方金额': 100,
             '贷方金额': 0, '摘要': '采购'},
        ]), self.user)
        self.assertEqual(up.status, 'success')
        rec = MasterRecord.objects.get(unit_name='己公司')
        self.assertEqual(rec.tag, '')

    def test_overlap_skips_duplicates_keeps_new(self):
        up = run_import('bank', _xlsx([
            # 与旧导入完全重复（同日期/单位/金额/摘要）
            {'对方户名': '甲公司', '交易日期': '2026-06-01', '借方金额': 0,
             '贷方金额': 1900, '摘要': '货款'},
            # 新记录（晚于旧导入截止日）
            {'对方户名': '丙公司', '交易日期': '2026-07-30', '借方金额': 0,
             '贷方金额': 800, '摘要': '货款'},
        ]), self.user)
        self.assertEqual(up.status, 'success')
        self.assertEqual(up.row_count, 1)            # 仅丙公司新增
        self.assertIn('跳过重复 1 条', up.note)
        self.assertEqual(MasterRecord.objects.filter(source_type='bank').count(), 3)
        self.assertTrue(MasterRecord.objects.filter(unit_name='丙公司').exists())
        self.assertFalse(MasterRecord.objects.filter(unit_name='甲公司',
                          source_upload=up).exists())

    def test_empty_counterparty_reported_not_silent(self):
        up = run_import('bank', _xlsx([
            {'对方户名': '', '交易日期': '2026-07-30', '借方金额': 0,
             '贷方金额': 800, '摘要': '结息'},
            {'对方户名': '丙公司', '交易日期': '2026-07-31', '借方金额': 0,
             '贷方金额': 100, '摘要': '货款'},
        ]), self.user)
        self.assertEqual(up.row_count, 1)
        self.assertIn('无对方户名跳过 1 行', up.note)


class AcceptanceSearchTest(TestCase):
    """票据检索页：承兑与迪链同表检索，可按类型/方向/日期过滤。"""

    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('acc', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='acc', password='x')
        up = SourceUpload.objects.create(source_type='acceptance', file_name='t.xlsx', status='success')

        def mk(unit, d, cat, debit, credit, tag='', package_no='', raw=None):
            raw_row = None
            if raw is not None or package_no:
                raw = dict(raw or {})
                if package_no:
                    raw.setdefault('票据包号码', package_no)
                raw_row = RawImportRow.objects.create(source_upload=up, data=raw)
            MasterRecord.objects.create(
                unit_name=unit, source_type='acceptance', biz_type='承兑', tag=tag,
                source_upload=up, raw_row=raw_row, occur_date=d, year=d.year,
                period=d.strftime('%Y%m'), category=cat, debit=debit, credit=credit,
                handler='', package_no=package_no)

        # 同一票据包 PKG001：先收(签收)后付(申请)，验证拆分付款与库存余额
        mk('承兑签收方', date(2025, 3, 1), '签收', 0, 1000, package_no='PKG001',
           raw={'出票人全称': '测试出票人A'})
        mk('承兑申请方', date(2025, 6, 1), '申请', 2000, 0, package_no='PKG001',
           raw={'出票人全称': '测试出票人A'})
        mk('迪链签收方', date(2025, 4, 1), '签收', 0, 3000, tag='迪链')
        mk('迪链转单方', date(2025, 5, 1), '转单', 4000, 0, tag='迪链')

    def test_lists_both_acceptance_and_dilian(self):
        resp = self.client.get('/acceptance/')
        self.assertEqual(resp.status_code, 200)
        for unit in ('承兑签收方', '承兑申请方', '迪链签收方', '迪链转单方'):
            self.assertContains(resp, unit)
        # 合计：签收 1000+3000，申请/转单 2000+4000
        self.assertEqual(resp.context['totals']['recv'], 4000.0)
        self.assertEqual(resp.context['totals']['pay'], 6000.0)
        self.assertEqual(resp.context['totals']['net'], -2000.0)

    def _units(self, resp):
        # 单位下拉(datalist)始终列出全部单位，故断言命中的明细而非整页 HTML
        return sorted(r.unit_name for r in resp.context['page_obj'])

    def test_filter_by_kind(self):
        resp = self.client.get('/acceptance/?kind=迪链')
        self.assertEqual(resp.context['count'], 2)
        self.assertEqual(self._units(resp), ['迪链签收方', '迪链转单方'])

        resp = self.client.get('/acceptance/?kind=承兑')
        self.assertEqual(resp.context['count'], 2)
        self.assertEqual(self._units(resp), ['承兑申请方', '承兑签收方'])

    def test_filter_by_direction(self):
        resp = self.client.get('/acceptance/?direction=签收')
        self.assertEqual(resp.context['count'], 2)
        self.assertEqual(resp.context['totals']['recv'], 4000.0)
        self.assertEqual(resp.context['totals']['pay'], 0.0)

    def test_filter_by_date_range(self):
        resp = self.client.get('/acceptance/?date_from=2025-04-01&date_to=2025-05-31')
        self.assertEqual(resp.context['count'], 2)
        self.assertEqual(self._units(resp), ['迪链签收方', '迪链转单方'])

    def test_no_default_date_shows_all(self):
        """与其他查询页一致：不设默认时间范围。"""
        resp = self.client.get('/acceptance/')
        self.assertEqual(resp.context['count'], 4)
        self.assertEqual(resp.context['filters']['date_from'], '')
        self.assertEqual(resp.context['filters']['date_to'], '')

    def test_export_returns_xlsx(self):
        import io
        import openpyxl
        resp = self.client.get('/acceptance/export/?kind=迪链')
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ['票据汇总', '票据明细'])
        detail = [r[1] for r in wb['票据明细'].iter_rows(min_row=2, values_only=True)]
        self.assertEqual(sorted(detail), ['迪链签收方', '迪链转单方'])

    def test_drill_through_by_unit(self):
        """点击单位名称穿透查询，仅展示该单位票据明细。"""
        resp = self.client.get('/acceptance/detail/?unit_name={}'.format(
            quote('承兑签收方')))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['unit_name'], '承兑签收方')
        self.assertEqual(resp.context['count'], 1)
        self.assertEqual(resp.context['unit_summary']['recv'], 1000.0)
        self.assertEqual(resp.context['unit_summary']['cnt'], 1)
        self.assertEqual([r.unit_name for r in resp.context['page_obj']], ['承兑签收方'])

    def test_drill_through_preserves_filters(self):
        """穿透时携带的其它筛选条件（如类型）依然生效。"""
        resp = self.client.get('/acceptance/detail/?unit_name={}&kind=迪链'.format(
            quote('迪链签收方')))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['count'], 1)
        self.assertEqual([r.unit_name for r in resp.context['page_obj']], ['迪链签收方'])

    def test_drill_through_without_unit_redirects(self):
        resp = self.client.get('/acceptance/detail/')
        self.assertEqual(resp.status_code, 302)

    def test_package_summary(self):
        """按票据包号码汇总：收款/付款/库存余额/笔数，迪链(无包号)不计入。"""
        resp = self.client.get('/acceptance/')
        self.assertEqual(resp.status_code, 200)
        pkg = {s['package_no']: s for s in resp.context['pkg_summary']}
        self.assertIn('PKG001', pkg)
        self.assertEqual(pkg['PKG001']['recv'], 1000.0)
        self.assertEqual(pkg['PKG001']['pay'], 2000.0)
        self.assertEqual(pkg['PKG001']['net'], -1000.0)
        self.assertEqual(pkg['PKG001']['cnt'], 2)
        # 迪链无包号，不进入按包号汇总
        self.assertEqual(len(resp.context['pkg_summary']), 1)
        self.assertEqual(resp.context['pkg_totals']['net'], -1000.0)

    def test_drill_through_by_package(self):
        """点击票据包号码穿透：展示该包全部原始列（23列）明细。"""
        resp = self.client.get('/acceptance/detail/?package_no={}'.format(quote('PKG001')))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['mode'], 'pkg')
        self.assertEqual(resp.context['title'], 'PKG001')
        self.assertEqual(resp.context['count'], 2)
        self.assertEqual(resp.context['unit_summary']['recv'], 1000.0)
        self.assertEqual(resp.context['unit_summary']['pay'], 2000.0)
        self.assertEqual(resp.context['unit_summary']['net'], -1000.0)
        # 原始列展示：表头含票据包号码/出票人全称，值含测试出票人A
        self.assertIn('票据包号码', resp.context['raw_columns'])
        self.assertIn('出票人全称', resp.context['raw_columns'])
        self.assertEqual(len(resp.context['raw_rows']), 2)
        flat = [c for row in resp.context['raw_rows'] for c in row]
        self.assertIn('测试出票人A', flat)
        self.assertContains(resp, 'PKG001')


class AcceptanceReportTest(TestCase):
    """承兑收付款统计报表：按期间(月/年)聚合收款/付款/期末滚动余额。"""

    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('rpt', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='rpt', password='x')
        up = SourceUpload.objects.create(source_type='acceptance', file_name='t.xlsx', status='success')
        rows = [
            ('承兑A', date(2025, 3, 1), '签收', 0, 1000),
            ('迪链B', date(2025, 4, 1), '签收', 0, 3000, '迪链'),
            ('迪链C', date(2025, 5, 1), '转单', 4000, 0, '迪链'),
            ('承兑D', date(2025, 6, 1), '申请', 2000, 0),
        ]
        for unit, d, cat, deb, cre, *rest in rows:
            tag = rest[0] if rest else ''
            MasterRecord.objects.create(
                unit_name=unit, source_type='acceptance', biz_type='承兑', tag=tag,
                source_upload=up, occur_date=d, year=d.year, period=d.strftime('%Y%m'),
                category=cat, debit=deb, credit=cre, handler='')

    def test_report_monthly(self):
        resp = self.client.get('/acceptance/report/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['rows']), 4)
        self.assertEqual(resp.context['totals']['recv'], 4000.0)
        self.assertEqual(resp.context['totals']['pay'], 6000.0)
        self.assertEqual(resp.context['totals']['net'], -2000.0)
        # 期末滚动余额（最后一期）= 累计净额
        self.assertEqual(resp.context['totals']['balance'], -2000.0)
        self.assertEqual(resp.context['rows'][-1]['balance'], -2000.0)

    def test_report_yearly(self):
        resp = self.client.get('/acceptance/report/?dim=year')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['rows']), 1)
        self.assertEqual(resp.context['rows'][0]['label'], '2025')
        self.assertEqual(resp.context['rows'][0]['balance'], -2000.0)

    def test_report_export(self):
        import io
        import openpyxl
        resp = self.client.get('/acceptance/report/export/')
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ['收付款统计报表'])

    def test_report_with_opening(self):
        """存在承兑期初余额时，期末余额 = 期初 + 累计收付款净额。"""
        AcceptanceOpeningBalance.objects.create(as_of_date=date(2025, 1, 1), amount=5000)
        resp = self.client.get('/acceptance/report/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['opening'].amount, 5000)
        self.assertEqual(resp.context['totals']['recv'], 4000.0)
        self.assertEqual(resp.context['totals']['pay'], 6000.0)
        # 5000 + (4000 − 6000) = 3000
        self.assertEqual(resp.context['totals']['balance'], 3000.0)
        self.assertEqual(resp.context['rows'][-1]['balance'], 3000.0)

    def test_set_opening_post(self):
        """POST 录入期初余额，创建单一记录并跳回报表。"""
        resp = self.client.post('/acceptance/report/opening/',
                                 {'as_of_date': '2025-01-01', 'amount': '5000', 'remark': '期初录入测试'})
        self.assertEqual(resp.status_code, 302)
        ob = AcceptanceOpeningBalance.objects.first()
        self.assertIsNotNone(ob)
        self.assertEqual(ob.amount, 5000)
        self.assertEqual(str(ob.as_of_date), '2025-01-01')


class PasswordPolicyTest(TestCase):
    """密码强度策略：自定义复杂度校验器 + 改密视图集成。"""

    @classmethod
    def setUpTestData(cls):
        cls.u = User.objects.create_user('alice', password='OldPass#12345')

    # ---- 校验器单元测试 ----
    def test_weak_passwords_rejected(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as VE
        weak = [
            'short1!',                 # 长度不足 12
            'alllowercase1!',          # 无大写
            'ALLUPPERCASE1!',          # 无小写
            'NoSpecialChar123',        # 无特殊字符
            'NoDigit!@#ABCdef',        # 无数字
            '123456789012',           # 纯数字
            'password123!',            # 常见弱密码（含变体）
        ]
        for pwd in weak:
            with self.assertRaises(VE, msg=f'应拒绝弱密码: {pwd}'):
                validate_password(pwd, self.u)

    def test_strong_password_accepted(self):
        from django.contrib.auth.password_validation import validate_password
        # 12 位 + 大小写 + 数字 + 特殊字符
        validate_password('Str0ng!Passw0rd', self.u)

    def test_similarity_to_username_rejected(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as VE
        with self.assertRaises(VE):
            validate_password('alice123', self.u)  # 与用户名 alice 高度相似

    # ---- 改密视图集成测试 ----
    def test_change_to_weak_password_blocked(self):
        self.client.force_login(self.u)
        resp = self.client.post(reverse('password_change'), {
            'old_password': 'OldPass#12345',
            'new_password1': 'weak1!',
            'new_password2': 'weak1!',
        })
        # 校验失败应停留在改密页（200），且密码未被更改
        self.assertEqual(resp.status_code, 200)
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password('OldPass#12345'))

    def test_change_to_strong_password_ok(self):
        self.client.force_login(self.u)
        resp = self.client.post(reverse('password_change'), {
            'old_password': 'OldPass#12345',
            'new_password1': 'NewStr0ng!Pass',
            'new_password2': 'NewStr0ng!Pass',
        })
        self.assertEqual(resp.status_code, 302)
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password('NewStr0ng!Pass'))


class ForcePasswordChangeTest(TestCase):
    """存量用户强制改密：标记未改密者被中间件跳改密页，改密后放行。"""

    @classmethod
    def setUpTestData(cls):
        cls.u = User.objects.create_user('bob', password='OldPass#12345')

    def _set_flag(self, must):
        p, _ = UserProfile.objects.get_or_create(user=self.u)
        p.must_change_password = must
        p.save(update_fields=['must_change_password'])
        return p

    def test_forced_user_redirected_to_change(self):
        self._set_flag(True)
        self.client.login(username='bob', password='OldPass#12345')
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('password_change'), resp.url)

    def test_change_clears_flag_and_allows_access(self):
        self._set_flag(True)
        self.client.login(username='bob', password='OldPass#12345')
        resp = self.client.post(reverse('password_change'), {
            'old_password': 'OldPass#12345',
            'new_password1': 'NewStr0ng!Pass',
            'new_password2': 'NewStr0ng!Pass',
        })
        self.assertEqual(resp.status_code, 302)
        self.u.profile.refresh_from_db()
        self.assertFalse(self.u.profile.must_change_password)
        # 改密后不再被拦截
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)

    def test_not_forced_when_flag_false(self):
        self._set_flag(False)
        self.client.login(username='bob', password='OldPass#12345')
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)

    def test_logout_allowed_while_forced(self):
        self._set_flag(True)
        self.client.login(username='bob', password='OldPass#12345')
        resp = self.client.get(reverse('logout'))
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_weak_password_blocked_while_forced(self):
        # 强制改密期间，弱密码仍被拒且仍停留在改密页（标记保持）
        self._set_flag(True)
        self.client.login(username='bob', password='OldPass#12345')
        resp = self.client.post(reverse('password_change'), {
            'old_password': 'OldPass#12345',
            'new_password1': 'weak1!',
            'new_password2': 'weak1!',
        })
        self.assertEqual(resp.status_code, 200)
        self.u.profile.refresh_from_db()
        self.assertTrue(self.u.profile.must_change_password)


class ReconcileUnmatchedTest(TestCase):
    """reconcile_unmatched：导出「银行收款但经手人为空」三份清单。"""

    def _bank(self, unit, credit, occur=None, handler='', tag=''):
        return MasterRecord.objects.create(
            source_type='bank', unit_name=unit, biz_type='银行',
            year=2025, period='202506', occur_date=occur,
            credit=credit, handler=handler, tag=tag)

    def _frh(self, unit, handler, amount, occur=None):
        return FundReceiptHandler.objects.create(
            unit_name=unit, handler=handler, amount=amount, occur_date=occur)

    def test_three_lists_generated(self):
        # 单位A：两笔空经手人银行收款 + 资金明细业务员表候选
        self._bank('单位A', 100.00, datetime.date(2025, 6, 1))
        self._bank('单位A', 200.00, datetime.date(2025, 6, 2))
        self._frh('单位A', '张三', 100.00, datetime.date(2025, 1, 1))
        self._frh('单位A', '李四', 200.00, datetime.date(2025, 2, 1))
        # 单位B：一笔空经手人银行收款，无候选
        self._bank('单位B', 50.00, datetime.date(2025, 6, 3))
        # 单位C：已建立现任业务员映射 → 不应出现在「仍需手工补登」
        self._bank('单位C', 80.00, datetime.date(2025, 6, 4))
        CustomerSalesperson.objects.create(unit_name='单位C', salesperson='王五', is_current=True)
        # 作废银行收款应被排除
        self._bank('单位D', 999.00, datetime.date(2025, 6, 5), tag='作废')

        import tempfile, csv, io
        out = tempfile.mkdtemp()
        out_io = io.StringIO()
        call_command('reconcile_unmatched', output=out, stdout=out_io)

        # 1) 待对齐清单
        with open(f'{out}/未匹配_待对齐清单.csv', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)  # A×2 + B + C（D 作废已排除）
        a_rows = [r for r in rows if r['单位名称'] == '单位A']
        self.assertEqual(len(a_rows), 2)
        self.assertEqual(a_rows[0]['业务员表登记笔数'], '2')
        self.assertIn('张三', a_rows[0]['登记候选(日期/金额/经手人)'])
        self.assertIn('李四', a_rows[0]['登记候选(日期/金额/经手人)'])

        # 2) 缺失单位清单：A(300) / C(80) / B(50)
        with open(f'{out}/未匹配_缺失单位清单.csv', encoding='utf-8-sig') as f:
            miss = {r['单位名称']: r for r in csv.DictReader(f)}
        self.assertEqual(set(miss), {'单位A', '单位B', '单位C'})
        self.assertEqual(miss['单位A']['银行空经手人收款笔数'], '2')
        self.assertEqual(miss['单位A']['合计金额'], '300.00')

        # 3) 仍需手工补登单位：C 已建现任映射 → 剔除，只剩 A、B
        with open(f'{out}/未匹配_仍需手工补登单位.csv', encoding='utf-8-sig') as f:
            need = list(csv.DictReader(f))
        self.assertEqual({r['单位名称'] for r in need}, {'单位A', '单位B'})
        self.assertTrue(all(r['状态'] == '需手工补登经手人' for r in need))


class GlobalSearchLinkTest(TestCase):
    """综合查询（全局检索）：单位名称/发票号可点击跳转。"""

    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('gl', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='gl', password='x')
        self.up = SourceUpload.objects.create(source_type='sales_invoice', file_name='t.xlsx', status='success')
        self.rec = MasterRecord.objects.create(
            unit_name='跳转单位', source_type='sales_invoice', invoice_number='INV-123',
            handler='', tag='', source_upload=self.up, occur_date=datetime.date.today(),
            biz_type='发票', year=datetime.date.today().year, period=datetime.date.today().strftime('%Y%m'),
            debit=100, credit=0)
        MasterRecord.objects.create(
            unit_name='无票单位', source_type='bank', invoice_number='',
            handler='', tag='', source_upload=self.up, occur_date=datetime.date.today(),
            biz_type='银行', year=datetime.date.today().year, period=datetime.date.today().strftime('%Y%m'),
            credit=50)

    def test_unit_name_links_to_statement(self):
        resp = self.client.get('/search/?unit_name=跳转单位')
        self.assertContains(resp, 'statement/?unit_name=')
        self.assertContains(resp, '查看该单位往来明细')

    def test_invoice_links_to_detail(self):
        resp = self.client.get('/search/?unit_name=跳转单位')
        self.assertContains(resp, f'/invoice/{self.rec.pk}/')
        self.assertContains(resp, '查看该发票货物明细')

    def test_empty_invoice_not_linked(self):
        resp = self.client.get('/search/?unit_name=无票单位')
        # 无发票号时不应出现「发票货物明细」跳转链接（base 导航的 /invoice/ 不算）
        self.assertNotContains(resp, '查看该发票货物明细')


# ---------------------------------------------------------------------------
# 催款管理 / 账龄分析的应收余额口径
#   余额 = 借贷净额 Σ(借−贷)，必须与往来明细账一致；红冲（负数）、预收、多收
#   都必须参与 FIFO 核销，否则应收虚高或被截成 0。
# ---------------------------------------------------------------------------
class CustomerAgingCaliberTest(TestCase):
    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        self.base = MasterRecord.objects.all().exclude(tag__in=EXCLUDED_TAGS)
        self.as_of = date(2024, 12, 31)

    def _mk(self, **kw):
        defaults = dict(source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
                        biz_type='发票', year=2024, period='202401',
                        occur_date=date(2024, 1, 1), debit=0, credit=0,
                        category='', tag='', handler='', remark='',
                        tax_no='', invoice_number='')
        defaults.update(kw)
        d = defaults['occur_date']
        defaults['year'] = d.year
        defaults['period'] = '%04d%02d' % (d.year, d.month)
        return MasterRecord.objects.create(**defaults)

    def _aging(self):
        return _customer_aging('客户A', self.base, self.as_of, 30)

    def test_red_invoice_reduces_receivable(self):
        # 红冲发票（借方负数）必须冲减应收，不能被 FIFO 跳过
        self._mk(occur_date=date(2024, 1, 10), debit=1000)
        self._mk(occur_date=date(2024, 2, 10), debit=-400, tag='红冲')
        a = self._aging()
        self.assertAlmostEqual(a['balance'], 600.0, places=2)
        self.assertAlmostEqual(a['b1'] + a['b2'] + a['b3'] + a['b4'], 600.0, places=2)

    def test_prepayment_is_not_dropped(self):
        # 先收款后开票：预收必须结转，不能丢弃后把后续发票算成未核销应收
        self._mk(occur_date=date(2024, 1, 5), credit=1000)
        self._mk(occur_date=date(2024, 2, 5), debit=600)
        a = self._aging()
        self.assertAlmostEqual(a['balance'], -400.0, places=2)
        self.assertAlmostEqual(a['b1'], 0.0, places=2)

    def test_overpaid_balance_is_negative(self):
        # 客户多付：余额应为负数（预收），而不是被截成 0
        self._mk(occur_date=date(2024, 1, 10), debit=1000)
        self._mk(occur_date=date(2024, 3, 10), credit=1500)
        a = self._aging()
        self.assertAlmostEqual(a['balance'], -500.0, places=2)
        self.assertAlmostEqual(a['overdue'], 0.0, places=2)

    def test_red_payment_restores_receivable(self):
        # 回款被红冲（贷方负数）：应收应恢复
        self._mk(occur_date=date(2024, 1, 10), debit=1000)
        self._mk(occur_date=date(2024, 2, 10), credit=1000)
        self._mk(occur_date=date(2024, 3, 10), credit=-1000, tag='红冲')
        a = self._aging()
        self.assertAlmostEqual(a['balance'], 1000.0, places=2)

    def test_balance_matches_statement_net(self):
        # 含红冲与红冲回款时，催款余额仍等于明细账借贷净额
        rows = [
            (date(2024, 1, 10), 1000, 0),
            (date(2024, 2, 10), 0, 600),
            (date(2024, 3, 10), -200, 0),
            (date(2024, 4, 10), 0, -100),
        ]
        for d, db, cr in rows:
            self._mk(occur_date=d, debit=db, credit=cr)
        a = self._aging()
        net = float(sum(db - cr for _, db, cr in rows))
        self.assertAlmostEqual(a['balance'], net, places=2)
        # 账龄各桶合计应与余额相等
        self.assertAlmostEqual(a['b1'] + a['b2'] + a['b3'] + a['b4'], net, places=2)


# ---------------------------------------------------------------------------
# 导航栏需显示当前登录用户名与角色
# ---------------------------------------------------------------------------
class NavbarUsernameTest(TestCase):
    def test_navbar_shows_username_and_role(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        u = User.objects.create_user('zhangsan', password='x')
        u.groups.add(g)
        c = Client()
        self.assertTrue(c.login(username='zhangsan', password='x'))
        resp = c.get('/collection/')
        self.assertContains(resp, '当前用户：zhangsan')
        self.assertContains(resp, '管理员')


# ---------------------------------------------------------------------------
# 业务员开票回款明细：保证金不计入开票额与回款额
# ---------------------------------------------------------------------------
class SalesPerfDepositTest(TestCase):
    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        self.base = MasterRecord.objects.all()

    def _mk(self, **kw):
        defaults = dict(source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
                        biz_type='发票', year=2024, period='202401',
                        occur_date=date(2024, 1, 1), debit=0, credit=0,
                        category='', tag='', handler='', remark='',
                        tax_no='', invoice_number='')
        defaults.update(kw)
        d = defaults['occur_date']
        defaults['year'] = d.year
        defaults['period'] = '%04d%02d' % (d.year, d.month)
        return MasterRecord.objects.create(**defaults)

    def test_deposit_excluded_from_invoice_and_collect(self):
        self._mk(debit=1000)                                                     # 开票
        self._mk(source_type='bank', biz_type='银行', category='收入', credit=300)   # 正常回款
        # 保证金：收款（会被回款额按 category 命中）与保证金发票
        self._mk(source_type='bank', biz_type='银行', category='收入', credit=200, tag='保证金')
        self._mk(debit=500, tag='保证金')
        a = _perf_for_customer(self.base, '客户A')
        self.assertAlmostEqual(a['invoice'], 1000.0, places=2)
        self.assertAlmostEqual(a['collect'], 300.0, places=2)
        self.assertAlmostEqual(a['unpaid'], 700.0, places=2)


# ---------------------------------------------------------------------------
# 客户往来明细账「编辑模式」需支持就地改标注（含保证金）
# ---------------------------------------------------------------------------
class StatementEditTagTest(TestCase):
    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        g = Group.objects.get_or_create(name='管理员')[0]
        self.admin = User.objects.create_user('admin', password='x')
        self.admin.groups.add(g)
        self.client = Client()
        self.client.login(username='admin', password='x')
        self.rec = MasterRecord.objects.create(
            source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
            biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
            debit=100, credit=0, category='', tag='', handler='')

    def test_edit_mode_renders_tag_select(self):
        resp = self.client.get('/statement/?unit_name=%s&edit=1' % '客户A')
        self.assertContains(resp, 'tag_%d' % self.rec.pk)
        self.assertContains(resp, '保证金')

    def test_view_mode_has_no_tag_input(self):
        resp = self.client.get('/statement/?unit_name=%s' % '客户A')
        self.assertNotContains(resp, 'tag_%d' % self.rec.pk)


# ---------------------------------------------------------------------------
# 分页控件需包含首页 / 末页
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 分页控件需包含首页 / 末页
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 分页控件需包含首页 / 末页
# ---------------------------------------------------------------------------
class PaginationFirstLastTest(TestCase):
    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        g = Group.objects.get_or_create(name='管理员')[0]
        self.admin = User.objects.create_user('admin', password='x')
        self.admin.groups.add(g)
        for i in range(25):
            MasterRecord.objects.create(
                source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
                biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=10, credit=0, category='', tag='', handler='',
                invoice_number='INV-%03d' % i)
        self.c = Client()
        self.c.login(username='admin', password='x')

    def test_statement_pagination_has_first_and_last(self):
        # 25 条 / 每页 20 条 → 共 2 页
        resp = self.c.get('/statement/?unit_name=%s&per_page=20&page=1' % '客户A')
        self.assertContains(resp, '末页')
        self.assertContains(resp, 'page=2')
        body = resp.content.decode('utf-8')
        self.assertIn('pagination', body)
        # 第 1 页时「首页 / 上一页」应为禁用态
        self.assertGreater(body.count('page-item disabled'), 0)


# ---------------------------------------------------------------------------
# 编辑模式保存标注：操作日志 target_id 不能超出 varchar(100)
#   PostgreSQL 强制校验长度，超长会 DataError → 500；SQLite 不校验，本地看不出来
# ---------------------------------------------------------------------------
class RecordTagUpdateLogTest(TestCase):
    def setUp(self):
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        g = Group.objects.get_or_create(name='管理员')[0]
        self.admin = User.objects.create_user('admin', password='x')
        self.admin.groups.add(g)
        self.client = Client()
        self.client.login(username='admin', password='x')
        self.recs = []
        for i in range(60):   # 模拟编辑模式整页提交（每页 50~200 行）
            self.recs.append(MasterRecord.objects.create(
                source_type='sales_invoice', source_upload=self.up, unit_name='客户A',
                biz_type='发票', year=2024, period='202401', occur_date=date(2024, 1, 1),
                debit=10, credit=0, category='', tag='', handler=''))

    def test_target_id_within_field_limit(self):
        post = {'next': '/statement/?unit_name=%s&edit=1' % '客户A'}
        # 每一行的备注都提交（编辑模式的真实行为）
        for r in self.recs:
            post['remark_%d' % r.pk] = r.remark
        post['tag_%d' % self.recs[0].pk] = '保证金'
        resp = self.client.post('/data/record_tags/', post)
        self.assertEqual(resp.status_code, 302)
        self.recs[0].refresh_from_db()
        self.assertEqual(self.recs[0].tag, '保证金')
        log = OperationLog.objects.latest('id')
        max_len = OperationLog._meta.get_field('target_id').max_length
        self.assertLessEqual(len(log.target_id), max_len)
        # 只记录真正变更的记录
        self.assertEqual(log.target_id, str(self.recs[0].pk))


# ---------------------------------------------------------------------------
# 表头点击排序：白名单之外的 sort 一律回退默认顺序，排序参数须能跨分页保留
# ---------------------------------------------------------------------------
class TableSortTest(TestCase):
    def setUp(self):
        g = Group.objects.get_or_create(name='管理员')[0]
        self.user = User.objects.create_user('sortadmin', password='x')
        self.user.groups.add(g)
        self.client = Client()
        self.client.login(username='sortadmin', password='x')
        self.up = SourceUpload.objects.create(
            source_type='sales_invoice', file_name='t.xlsx', status='success')
        # 3 条：日期与金额都不同，便于断言顺序
        for i, (d, amt, unit) in enumerate([
                (date(2025, 1, 10), 300, '丙公司'),
                (date(2025, 2, 10), 100, '甲公司'),
                (date(2025, 3, 10), 200, '乙公司')]):
            MasterRecord.objects.create(
                source_type='sales_invoice', source_upload=self.up, unit_name=unit,
                biz_type='发票', year=2025, period=d.strftime('%Y%m'), occur_date=d,
                debit=amt, credit=0, category='', tag='', handler='',
                invoice_number='INV-%03d' % i)

    def _units(self, resp):
        return [r.unit_name for r in resp.context['page_obj']]

    def test_invoice_search_sort_asc_and_desc(self):
        r1 = self.client.get('/invoice/?sort=debit&sort_dir=asc')
        self.assertEqual(self._units(r1), ['甲公司', '乙公司', '丙公司'])
        r2 = self.client.get('/invoice/?sort=debit&sort_dir=desc')
        self.assertEqual(self._units(r2), ['丙公司', '乙公司', '甲公司'])

    def test_unknown_or_malicious_sort_falls_back_to_default(self):
        default = self._units(self.client.get('/invoice/?all_sources=1'))
        for bad in ('', 'nope', '-id', 'debit;DROP TABLE books_masterrecord',
                    'raw_row__data', 'password', 'unit_name__contains'):
            resp = self.client.get('/invoice/?all_sources=1&sort=%s' % quote(bad))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self._units(resp), default)

    def test_invalid_dir_falls_back(self):
        resp = self.client.get('/invoice/?sort=debit&sort_dir=1%27')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._units(resp), self._units(
            self.client.get('/invoice/?sort=debit')))

    def test_sort_survives_pagination(self):
        # 每页 2 条，翻到第 2 页时排序参数仍在，且 page 只出现一次
        resp = self.client.get('/invoice/?sort=debit&sort_dir=asc&per_page=2&page=1')
        body = resp.content.decode('utf-8')
        self.assertIn('sort=debit', body)
        for href in [l for l in body.split('href="') if 'page=2' in l]:
            self.assertEqual(href.split('"')[0].count('page='), 1)

    def test_acceptance_tables_sort_independently(self):
        """三张表各有自己的排序参数：明细排序不应影响上面两张汇总表。"""
        for unit, amt in (('甲公司', 500), ('乙公司', 100)):
            MasterRecord.objects.create(
                source_type='acceptance', source_upload=self.up, unit_name=unit,
                biz_type='承兑', year=2025, period='202501', occur_date=date(2025, 1, 1),
                debit=amt, credit=0, category='申请', tag='', handler='',
                package_no='PKG-%s' % unit)
        det = self.client.get('/acceptance/?det_sort=debit&det_sort_dir=desc')
        self.assertEqual(det.status_code, 200)
        self.assertEqual([r.unit_name for r in det.context['page_obj']], ['甲公司', '乙公司'])
        # 汇总表未带自己的排序参数 → 保持默认（按单位名/包号升序）
        self.assertEqual([s['unit_name'] for s in det.context['summary']],
                         list(MasterRecord.objects.filter(source_type='acceptance')
                              .order_by('unit_name').values_list('unit_name', flat=True).distinct()))
        self.assertEqual([s['package_no'] for s in det.context['pkg_summary']],
                         list(MasterRecord.objects.filter(source_type='acceptance')
                              .exclude(package_no='').order_by('package_no')
                              .values_list('package_no', flat=True).distinct()))

    def test_running_balance_column_not_sortable(self):
        """余额是按发生日累计的滚动值：?sort=balance 不应改变行序。"""
        base = self.client.get('/statement/?unit_name=甲公司')
        resp = self.client.get('/statement/?unit_name=甲公司&sort=balance&sort_dir=desc')
        self.assertEqual([r['id'] for r in resp.context['rows']],
                         [r['id'] for r in base.context['rows']])

    def test_aging_list_sort(self):
        """中文按 Unicode 排序，断言用 sorted() 的结果而不是「拼音序」。"""
        names = ['甲公司', '乙公司', '丙公司']
        asc = self.client.get('/aging/?sort=unit&sort_dir=asc')
        self.assertEqual([r['unit'] for r in asc.context['rows']], sorted(names))
        desc = self.client.get('/aging/?sort=unit&sort_dir=desc')
        self.assertEqual([r['unit'] for r in desc.context['rows']], sorted(names, reverse=True))

    def test_export_ignores_sort(self):
        # 比较单元格内容而非 xlsx 字节：openpyxl 写入的修改时间随秒变化，字节比较会偶发失败。
        a = self.client.get('/aging/export/?sort=balance&sort_dir=desc')
        b = self.client.get('/aging/export/')
        wa = _openpyxl.load_workbook(io.BytesIO(a.content))
        wb = _openpyxl.load_workbook(io.BytesIO(b.content))
        self.assertEqual(
            [list(r) for r in wa.active.iter_rows(values_only=True)],
            [list(r) for r in wb.active.iter_rows(values_only=True)])


# ===========================================================================
# B2B 改造测试：认证授权 / 导入 / 户名匹配 / 分层可见
# ===========================================================================
import io
import openpyxl as _openpyxl
from decimal import Decimal as _Decimal

from books.models import (
    Department as _Department, SalespersonProfile as _SalespersonProfile,
    Merchant as _Merchant, ReceivingAccount as _ReceivingAccount,
    Counterparty as _Counterparty, Order as _Order, OrderPayment as _OrderPayment,
    AccountNameMapping as _AccountNameMapping, AccountNameAlias as _AccountNameAlias,
    InvitedPhone as _InvitedPhone, SmsCode as _SmsCode, MATCH_STATUS_CHOICES as _MATCH_STATUS_CHOICES,
)
from books.imports.utils import normalize_account_name as _normalize_account_name
from books.matching.engine import resolve_account_name as _resolve
from books.imports.lakala import run_import_lakala as _run_import_lakala
from books.decorators import visible_order_queryset as _visible_orders


class _NamedBytes(io.BytesIO):
    """给 BytesIO 加一个 name 属性（导入函数读取 file.name）。"""
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


class B2bAuthTest(TestCase):
    """手机验证码注册 + 管理员授权 + 待授权零权限。"""

    def setUp(self):
        Group.objects.get_or_create(name='管理员')
        Group.objects.get_or_create(name='商户')
        self.admin = User.objects.create_user('b2b_admin', password='pw')
        self.admin.groups.add(Group.objects.get(name='管理员'))
        self.admin.profile.account_status = '已授权'
        self.admin.profile.save()
        _InvitedPhone.objects.create(phone='13800000001')

    def _register(self, phone):
        c = Client()
        c.post('/register/', {'phone': phone, 'action': 'send'})
        code = _SmsCode.objects.filter(phone=phone, purpose='注册', is_used=False).first().code
        r = c.post('/register/', {'phone': phone, 'action': 'confirm', 'code': code})
        return c, r

    def test_register_creates_pending_user(self):
        c, r = self._register('13800000001')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/auth/pending/')
        u = User.objects.get(username='13800000001')
        self.assertEqual(u.profile.account_status, '待授权')

    def test_pending_user_blocked_then_authorized(self):
        c, r = self._register('13800000001')
        # 待授权访问订单被拦截
        self.assertEqual(c.get('/orders/').status_code, 302)
        # 管理员授权
        ac = Client(); ac.force_login(self.admin)
        u = User.objects.get(username='13800000001')
        r = ac.post('/auth/authorize/', {'user_id': u.pk, 'action': 'approve', 'role': '商户'})
        self.assertEqual(r.status_code, 302)
        u.refresh_from_db()
        self.assertEqual(u.profile.account_status, '已授权')
        self.assertTrue(u.groups.filter(name='商户').exists())
        # 授权后可访问
        self.assertEqual(c.get('/orders/').status_code, 200)

    def test_code_single_use_and_expiry(self):
        c = Client()
        c.post('/register/', {'phone': '13800000001', 'action': 'send'})
        code = _SmsCode.objects.filter(phone='13800000001', purpose='注册', is_used=False).first().code
        c.post('/register/', {'phone': '13800000001', 'action': 'confirm', 'code': code})
        # 已用验证码再次注册另一个号应失败（验证码已作废）
        _InvitedPhone.objects.create(phone='13800000002')
        c2 = Client()
        c2.post('/register/', {'phone': '13800000002', 'action': 'send'})
        c2.post('/register/', {'phone': '13800000002', 'action': 'confirm', 'code': code})
        self.assertFalse(User.objects.filter(username='13800000002').exists())


class B2bAccountNameNormalizeTest(TestCase):
    def test_normalize(self):
        self.assertEqual(_normalize_account_name('  武汉（测试）超市。 '), '武汉测试超市')
        self.assertEqual(_normalize_account_name('ABC-def'), 'ABCDEF')


class B2bAccountMatchingTest(TestCase):
    def setUp(self):
        self.cp = _Counterparty.objects.create(name='测试超市', party_type='客户')
        self.merchant = _Merchant.objects.create(name='商户甲', code='M1')
        self.salesperson = _SalespersonProfile.objects.create(name='王五')

    def test_manual_exact(self):
        _AccountNameMapping.objects.create(raw_name='测试超市', normalized_name='测试超市',
            counterparty=self.cp, is_manual=True, is_active=True, match_source='人工')
        r = _resolve('测试超市')
        self.assertEqual(r.counterparty, self.cp)
        self.assertEqual(r.source, '人工')
        self.assertTrue(r.status == '自动已匹配')

    def test_alias(self):
        _AccountNameMapping.objects.create(raw_name='测试超市', normalized_name='测试超市',
            counterparty=self.cp, is_active=True, match_source='精确')
        _AccountNameAlias.objects.create(alias='测试超市_NO1', canonical_raw_name='测试超市')
        r = _resolve('测试超市_NO1')
        self.assertEqual(r.source, '别名')
        self.assertEqual(r.counterparty, self.cp)

    def test_unmatched(self):
        r = _resolve('完全陌生的户名XYZ不存在的店')
        self.assertEqual(r.status, '未匹配')

    def test_manual_lock_wins_over_alias(self):
        other = _Counterparty.objects.create(name='其他主体', party_type='客户')
        _AccountNameMapping.objects.create(raw_name='测试超市', normalized_name='测试超市',
            counterparty=self.cp, is_manual=True, is_active=True)
        _AccountNameAlias.objects.create(alias='测试超市', canonical_raw_name='其他主体')
        r = _resolve('测试超市')
        self.assertEqual(r.counterparty, self.cp)  # 人工锁定不被别名覆盖


class B2bImportTest(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name='管理员')
        Group.objects.get_or_create(name='业务员')
        ColumnMappingConfig.objects.get_or_create(source_type='lakala_order',
            source_column='订单号', target_field='order_no', defaults={'order': 0})
        ColumnMappingConfig.objects.get_or_create(source_type='lakala_order',
            source_column='店铺名称', target_field='counterparty_raw_name', defaults={'order': 0})
        ColumnMappingConfig.objects.get_or_create(source_type='lakala_order',
            source_column='订单金额', target_field='total_amount', defaults={'order': 0})
        ColumnMappingConfig.objects.get_or_create(source_type='lakala_order',
            source_column='已收金额', target_field='paid_amount', defaults={'order': 0})
        ColumnMappingConfig.objects.get_or_create(source_type='lakala_order',
            source_column='交易时间', target_field='order_date', defaults={'order': 0})
        self.user = User.objects.create_user('b2b_importer', password='pw')

    def _xlsx(self):
        wb = _openpyxl.Workbook()
        ws = wb.active
        ws.append(['订单号', '店铺名称', '订单金额', '已收金额', '交易时间'])
        ws.append(['DD001', '测试店铺', '100.50', '40.00', '2026-01-01'])
        ws.append(['DD002', '测试店铺2', '200.00', '0', '2026-01-02'])
        buf = io.BytesIO()
        wb.save(buf)
        return _NamedBytes(buf.getvalue(), 'orders.xlsx')

    def test_import_creates_orders_and_payments(self):
        up = _run_import_lakala('lakala_order', self._xlsx(), self.user)
        self.assertEqual(up.status, 'success')
        self.assertEqual(_Order.objects.count(), 2)
        o = _Order.objects.get(external_order_no='DD001')
        self.assertEqual(o.total_amount, _Decimal('100.50'))
        self.assertEqual(o.paid_amount, _Decimal('40.00'))
        self.assertEqual(o.payments.count(), 1)
        self.assertEqual(o.direction, '销售')

    def test_import_idempotent(self):
        _run_import_lakala('lakala_order', self._xlsx(), self.user)
        up = _run_import_lakala('lakala_order', self._xlsx(), self.user)
        self.assertEqual(up.status, 'empty')
        self.assertEqual(_Order.objects.count(), 2)


class B2bHierarchyTest(TestCase):
    def setUp(self):
        for g in ['管理员', '高层', '部门', '业务员', '商户']:
            Group.objects.get_or_create(name=g)
        self.root = _Department.objects.create(name='总部', code='B2B_ROOT')
        self.dept = _Department.objects.create(name='一部', code='B2B_D1', parent=self.root)
        self.sp = _SalespersonProfile.objects.create(name='赵六', department=self.dept)
        self.m1 = _Merchant.objects.create(name='商户A', code='B2B_M1', salesperson=self.sp, department=self.dept)
        self.m2 = _Merchant.objects.create(name='商户B', code='B2B_M2', department=self.dept)
        _Order.objects.create(source_type='lakala_order', external_order_no='H1', direction='销售',
            counterparty_raw_name='X', merchant=self.m1, salesperson=self.sp, department=self.dept,
            total_amount=100)
        _Order.objects.create(source_type='lakala_order', external_order_no='H2', direction='销售',
            counterparty_raw_name='Y', merchant=self.m2, department=self.dept, total_amount=200)

    def _user(self, name, role, **kw):
        u = User.objects.create_user(name, password='pw')
        u.groups.add(Group.objects.get(name=role))
        p = u.profile
        p.account_status = '已授权'
        for k, v in kw.items():
            setattr(p, k, v)
        p.save()
        return u

    def test_admin_sees_all(self):
        u = self._user('h_admin', '管理员')
        self.assertEqual(set(_visible_orders(u, _Order.objects.all()).values_list('external_order_no', flat=True)),
                         {'H1', 'H2'})

    def test_department_sees_subtree(self):
        u = self._user('h_dept', '部门', department=self.root)
        self.assertEqual(set(_visible_orders(u, _Order.objects.all()).values_list('external_order_no', flat=True)),
                         {'H1', 'H2'})

    def test_salesperson_sees_own(self):
        u = self._user('h_sp', '业务员', salesperson=self.sp)
        self.assertEqual(list(_visible_orders(u, _Order.objects.all()).values_list('external_order_no', flat=True)), ['H1'])

    def test_merchant_sees_own(self):
        u = self._user('h_mer', '商户', merchant=self.m1)
        self.assertEqual(list(_visible_orders(u, _Order.objects.all()).values_list('external_order_no', flat=True)), ['H1'])

    def test_unauthorized_sees_none(self):
        u = self._user('h_pending', '商户', merchant=self.m1)
        u.profile.account_status = '待授权'
        u.profile.save()
        self.assertEqual(list(_visible_orders(u, _Order.objects.all())), [])


# ===========================================================================
# 腾讯云短信后端测试（mock SDK，不真实发送）
# ===========================================================================
import os as _os
from unittest import mock as _mock

from books import sms as _sms_mod


class B2bTencentSmsTest(TestCase):
    _ENV = {
        'TENCENTCLOUD_SECRET_ID': 'fake-id',
        'TENCENTCLOUD_SECRET_KEY': 'fake-key',
        'TENCENT_SMS_SDK_APP_ID': '1400006666',
        'TENCENT_SMS_SIGN_NAME': '测试签名',
        'TENCENT_SMS_TEMPLATE_ID': '449739',
    }

    def test_to_e164(self):
        self.assertEqual(_sms_mod._to_e164('13900001234'), '+8613900001234')
        self.assertEqual(_sms_mod._to_e164('+8613900001234'), '+8613900001234')
        self.assertEqual(_sms_mod._to_e164('8613900001234'), '+8613900001234')

    def test_missing_config_returns_false(self):
        with _mock.patch.dict(_os.environ, {}, clear=True):
            self.assertFalse(_sms_mod._send_tencent('13900001234', '123456', '注册'))

    def _fake_response(self, code, serial='SER1'):
        status = _mock.Mock()
        status.Code = code
        status.Message = 'OK' if code == 'Ok' else 'failed'
        status.SerialNo = serial
        resp = _mock.Mock()
        resp.SendStatusSet = [status]
        resp.to_json_string.return_value = '{}'
        return resp

    def test_send_success(self):
        with _mock.patch.dict(_os.environ, self._ENV, clear=True):
            with _mock.patch('tencentcloud.sms.v20210111.sms_client.SmsClient') as MockClient:
                MockClient.return_value.SendSms.return_value = self._fake_response('Ok')
                self.assertTrue(_sms_mod._send_tencent('13900001234', '654321', '注册'))
                req = MockClient.return_value.SendSms.call_args[0][0]
                self.assertEqual(req.PhoneNumberSet, ['+8613900001234'])
                self.assertEqual(req.TemplateParamSet, ['654321'])
                self.assertEqual(req.TemplateId, '449739')

    def test_send_api_failure(self):
        with _mock.patch.dict(_os.environ, self._ENV, clear=True):
            with _mock.patch('tencentcloud.sms.v20210111.sms_client.SmsClient') as MockClient:
                MockClient.return_value.SendSms.return_value = self._fake_response(
                    'FailedOperation.SignatureIncorrectOrUnapproved')
                self.assertFalse(_sms_mod._send_tencent('13900001234', '123456', '登录'))

    def test_empty_status(self):
        with _mock.patch.dict(_os.environ, self._ENV, clear=True):
            with _mock.patch('tencentcloud.sms.v20210111.sms_client.SmsClient') as MockClient:
                resp = _mock.Mock()
                resp.SendStatusSet = []
                resp.to_json_string.return_value = '{}'
                MockClient.return_value.SendSms.return_value = resp
                self.assertFalse(_sms_mod._send_tencent('13900001234', '123456', '注册'))

    def test_sdk_exception_is_caught(self):
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        with _mock.patch.dict(_os.environ, self._ENV, clear=True):
            with _mock.patch('tencentcloud.sms.v20210111.sms_client.SmsClient') as MockClient:
                MockClient.return_value.SendSms.side_effect = TencentCloudSDKException('boom')
                self.assertFalse(_sms_mod._send_tencent('13900001234', '123456', '注册'))

    def test_backend_selection(self):
        # default console
        with _mock.patch.object(_sms_mod.settings, 'DJANGO_SMS_BACKEND', 'console', create=True):
            self.assertTrue(_sms_mod.send_sms('13900001234', '123456', '注册'))


# ===========================================================================
# 往来单位归一模型测试（0028）
# ===========================================================================
from books.models import (
    CustomerEntity as _CustomerEntity, CustomerAlias as _CustomerAlias,
    Owner as _Owner, OwnerAccountMap as _OwnerAccountMap,
    PaymentReceipt as _PaymentReceipt, ReceivingAccount as _ReceivingAccount,
    CHANNEL_TYPE_CHOICES as _CHANNEL_TYPE_CHOICES,
    PAY_METHOD_CHOICES as _PAY_METHOD_CHOICES,
    CUSTOMER_ALIAS_TYPE_CHOICES as _CUSTOMER_ALIAS_TYPE_CHOICES,
)
from django.db import IntegrityError as _IntegrityError, transaction as _transaction


class B2bNormalizationModelTest(TestCase):
    def setUp(self):
        self.entity = _CustomerEntity.objects.create(name='某某集团有限公司', tax_no='91420000XXXXXXXXXX')
        self.owner = _Owner.objects.create(name='供货方甲', platform_owner_id='OWNER001')

    def test_multi_shop_one_entity(self):
        """一个主体的多个店铺/付款人归一到同一主体。"""
        for t, v in [('店铺名', '好又多超市'), ('店铺名', '好又多二分店'),
                     ('付款人名称', '张三'), ('平台店铺ID', 'SHOP001')]:
            _CustomerAlias.objects.create(alias_type=t, alias_value=v,
                                          normalized_value=v, entity=self.entity)
        self.assertEqual(self.entity.aliases.count(), 4)

    def test_alias_unique_per_type_value(self):
        _CustomerAlias.objects.create(alias_type='店铺名', alias_value='X店',
                                      normalized_value='X店', entity=self.entity)
        with self.assertRaises(_IntegrityError), _transaction.atomic():
            _CustomerAlias.objects.create(alias_type='店铺名', alias_value='X店',
                                          normalized_value='X店', entity=self.entity)

    def test_entity_tax_no_unique_when_present(self):
        with self.assertRaises(_IntegrityError), _transaction.atomic():
            _CustomerEntity.objects.create(name='另一个主体', tax_no=self.entity.tax_no)
        # 空税号可多个
        _CustomerEntity.objects.create(name='无税号主体A', tax_no='')
        _CustomerEntity.objects.create(name='无税号主体B', tax_no='')

    def test_owner_platform_id_unique(self):
        with self.assertRaises(_IntegrityError), _transaction.atomic():
            _Owner.objects.create(name='重复货主', platform_owner_id='OWNER001')

    def test_owner_account_map_triplet(self):
        acc_public = _ReceivingAccount.objects.create(account_name='对公账户', account_no='111',
                                                      account_type='对公')
        acc_wechat = _ReceivingAccount.objects.create(account_name='微信账户', account_no='222',
                                                      account_type='对私')
        _OwnerAccountMap.objects.create(owner=self.owner, account=acc_public,
                                        channel_type='线下', pay_method='公对公')
        _OwnerAccountMap.objects.create(owner=self.owner, account=acc_wechat,
                                        channel_type='线上', pay_method='微信')
        self.assertEqual(self.owner.account_maps.count(), 2)
        # 同一三元组+同账户不可重复
        with self.assertRaises(_IntegrityError), _transaction.atomic():
            _OwnerAccountMap.objects.create(owner=self.owner, account=acc_public,
                                            channel_type='线下', pay_method='公对公')

    def test_payment_receipt_normalization_fks(self):
        acc = _ReceivingAccount.objects.create(account_name='收款账户')
        r = _PaymentReceipt.objects.create(external_ref='PAY001', channel_type='线下',
            pay_method='公对公', payer_name='张三', payer_account='6222', amount='500.00',
            receiving_account_name='收款账户', receiving_account=acc, owner=self.owner,
            entity=self.entity)
        self.assertEqual(r.owner, self.owner)
        self.assertEqual(r.entity, self.entity)
        self.assertEqual(r.receiving_account, acc)
        with self.assertRaises(_IntegrityError), _transaction.atomic():
            _PaymentReceipt.objects.create(external_ref='PAY001', channel_type='线上',
                                           pay_method='微信', amount=1)


# ===========================================================================
# 归一引擎与收款明细导入测试（0028 逻辑）
# ===========================================================================
import io as _io
import openpyxl as _openpyxl2
from books.matching.normalization import (
    resolve_customer as _resolve_customer, resolve_owner as _resolve_owner,
    resolve_owner_account as _resolve_owner_account,
    resolve_receiving_account as _resolve_receiving_account,
)
from books.imports.payment_receipt import run_import_payment_receipt as _run_payment_import


class _NamedBytes2(_io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


class B2bNormalizationLogicTest(TestCase):
    def setUp(self):
        # 播种收款明细列映射（测试库全新，无 seed 命令的落库数据）
        from books.management.commands.init_books import PAYMENT_MAPPINGS
        from books.models import ColumnMappingConfig as _CMC, Operator as _Op, MappingStep as _MS
        for src, tgt, steps, order in PAYMENT_MAPPINGS['payment_detail']:
            m, _ = _CMC.objects.get_or_create(source_type='payment_detail',
                source_column=src, target_field=tgt, defaults={'is_active': True, 'order': order})
            for i, (op_key, params) in enumerate(steps):
                op, _ = _Op.objects.get_or_create(key=op_key, defaults={'name': op_key, 'params_schema': []})
                _MS.objects.get_or_create(mapping=m, op=op, order=i, defaults={'params': params})
        self.entity = _CustomerEntity.objects.create(name='主体甲', tax_no='TAX001')
        _CustomerAlias.objects.create(alias_type='店铺名', alias_value='甲店',
                                      normalized_value='甲店', entity=self.entity)
        _CustomerAlias.objects.create(alias_type='付款人名称', alias_value='张三',
                                      normalized_value='张三', entity=self.entity)
        self.owner = _Owner.objects.create(name='货主甲', platform_owner_id='OWNERX')

    def test_resolve_customer_by_shop_and_payer(self):
        self.assertEqual(_resolve_customer('甲店', '店铺名').obj, self.entity)
        self.assertEqual(_resolve_customer('张三', '付款人名称').obj, self.entity)
        self.assertIsNone(_resolve_customer('不存在', '店铺名').obj)

    def test_manual_alias_wins(self):
        # 唯一约束保证同 (type,value) 仅一条；验证人工锁定的映射被采用且不被覆盖
        other = _CustomerEntity.objects.create(name='主体乙', tax_no='TAX002')
        a = _CustomerAlias.objects.get(alias_type='店铺名', normalized_value='甲店')
        a.entity = other
        a.is_manual = True
        a.save()
        self.assertEqual(_resolve_customer('甲店', '店铺名').obj, other)

    def test_resolve_owner_and_account(self):
        self.assertEqual(_resolve_owner(platform_owner_id='OWNERX').obj, self.owner)
        acc = _ReceivingAccount.objects.create(account_name='对公户', account_no='111',
                                               channel_type='线下')
        _OwnerAccountMap.objects.create(owner=self.owner, account=acc,
                                        channel_type='线下', pay_method='公对公', is_default=True)
        r = _resolve_owner_account(self.owner, '线下', '公对公')
        self.assertEqual(r.obj, acc)
        self.assertEqual(_resolve_receiving_account(account_name='对公户').obj, acc)

    def _xlsx(self):
        wb = _openpyxl2.Workbook()
        ws = wb.active
        ws.append(['收款流水号', '订单号', '收款方式', '线上/线下', '收款日期',
                   '付款人名称', '付款人账号', '金额', '收款户名', '货主ID'])
        ws.append(['PAY001', 'ORD001', '公对公', '线下', '2026-06-01',
                   '张三', '6222', '888.00', '对公户', 'OWNERX'])
        buf = _io.BytesIO()
        wb.save(buf)
        return _NamedBytes2(buf.getvalue(), 'detail.xlsx')

    def test_payment_import_normalizes(self):
        # 建订单 + 收款账户 + 货主账户字典
        _ReceivingAccount.objects.create(account_name='对公户', account_no='111',
                                         channel_type='线下')
        _OwnerAccountMap.objects.create(owner=self.owner,
            account=_ReceivingAccount.objects.get(account_name='对公户'),
            channel_type='线下', pay_method='公对公', is_default=True)
        _Order.objects.create(source_type='lakala_order', external_order_no='ORD001',
                             direction='销售', counterparty_raw_name='甲店', total_amount=888)
        user = User.objects.create_user('norm_user', password='x')
        up = _run_payment_import(self._xlsx(), user)
        self.assertEqual(up.status, 'success')
        r = _PaymentReceipt.objects.get(external_ref='PAY001')
        self.assertEqual(r.entity, self.entity)       # 付款人张三 → 主体甲
        self.assertEqual(r.owner, self.owner)         # 货主ID → 货主甲
        self.assertEqual(r.receiving_account.account_name, '对公户')
        self.assertEqual(r.order.external_order_no, 'ORD001')
        self.assertEqual(r.match_status, '自动已匹配')

    def test_payment_import_idempotent(self):
        user = User.objects.create_user('norm_user2', password='x')
        _run_payment_import(self._xlsx(), user)
        up = _run_payment_import(self._xlsx(), user)
        self.assertEqual(up.status, 'empty')
        self.assertEqual(_PaymentReceipt.objects.count(), 1)


# ===========================================================================
# 真实拉卡拉 CSV 表头校准回归测试
# ===========================================================================
class B2bRealCsvMappingTest(TestCase):
    """用真实导出表头（订单_11-24_页1.csv 的 67 列）验证合并与字段落地。"""

    CSV_HEADER = ('订单Id,运单ID,店铺Id,客户Id,客户名称,支付Id,片区Id,状态,商品总额,总折扣,'
                  '订单总额,订单成本,线上支付-退款额,到货付款-退货额,订单实收款,下单时间,'
                  '编辑时间,支付时间,完成时间,支付方式,支付通道,收货人,商品名,货主Id')
    CSV_ROW1 = ('ORD9001,WAY1,SHOP1,CUST1,测试店铺,PAY1,AREA1,5-已完成,327.40,16.40,'
                '327.40,295.06,0,0,311.00,2026-11-19 23:17:49,,2026-11-19 23:18:02,,'
                '先款后货,微信B2b支付,测试店铺,商品A,OWNER1')
    CSV_ROW2 = ('ORD9001,WAY1,SHOP1,CUST1,测试店铺,PAY1,AREA1,5-已完成,327.40,16.40,'
                '327.40,295.06,0,0,311.00,2026-11-19 23:17:49,,2026-11-19 23:18:02,,'
                '先款后货,微信B2b支付,测试店铺,商品B,OWNER1')

    def setUp(self):
        from books.management.commands.init_books import LAKALA_MAPPINGS
        from books.models import ColumnMappingConfig as _CMC, Operator as _Op, MappingStep as _MS
        # 清理占位映射，避免同 target 多别名干扰（order 小的优先，真实列名应最小）
        _CMC.objects.filter(source_type='lakala_order').delete()
        for src, tgt, steps, order in LAKALA_MAPPINGS['lakala_order']:
            m = _CMC.objects.create(source_type='lakala_order', source_column=src,
                                    target_field=tgt, is_active=True, order=order)
            for i, (op_key, params) in enumerate(steps):
                op, _ = _Op.objects.get_or_create(key=op_key, defaults={'name': op_key, 'params_schema': []})
                _MS.objects.create(mapping=m, op=op, order=i, params=params)

    def test_real_csv_merge_and_fields(self):
        from books.imports.lakala import run_import_lakala as _run
        content = (self.CSV_HEADER + '\n' + self.CSV_ROW1 + '\n' + self.CSV_ROW2 + '\n').encode('utf-8-sig')
        f = _NamedBytes2(content, 'orders.csv')
        user = User.objects.create_user('csv_user', password='x')
        up = _run('lakala_order', f, user)
        self.assertEqual(up.status, 'success')
        self.assertEqual(_Order.objects.count(), 1, '同订单号的多行应合并为 1 单')
        o = _Order.objects.first()
        self.assertEqual(o.external_order_no, 'ORD9001')
        self.assertEqual(o.counterparty_raw_name, '测试店铺')
        self.assertEqual(o.total_amount, _Decimal('327.40'))
        self.assertEqual(o.paid_amount, _Decimal('311.00'))
        self.assertEqual(o.platform_owner_id, 'OWNER1')
        self.assertEqual(o.platform_customer_id, 'CUST1')
        self.assertIn('商品A', o.goods_summary)
        self.assertIn('商品B', o.goods_summary)


# ===========================================================================
# 真实收款明细表头识别测试（招行/富掌柜）
# ===========================================================================
class B2bPaymentTemplateTest(TestCase):
    def test_detect_cmb_header(self):
        from books.imports.payment_receipt import detect_template
        name, tmpl = detect_template(['账号', '交易日', '交易时间', '交易金额',
                                       '借方金额', '贷方金额', '流水号', '收(付)方名称', '收(付)方账号'])
        self.assertEqual(name, '招行交易查询')
        self.assertIsNotNone(tmpl)

    def test_detect_fzg_header(self):
        from books.imports.payment_receipt import detect_template
        name, tmpl = detect_template(['商户号', '商户名称', '交易时间', '交易金额',
                                       '支付方式', '业务类型', '交易单号', '交易备注3(用户账号)'])
        self.assertEqual(name, '富掌柜交易明细')
        self.assertIsNotNone(tmpl)

    def test_detect_unknown(self):
        from books.imports.payment_receipt import detect_template
        self.assertEqual(detect_template(['foo', 'bar'])[0], None)

    def test_cmb_amount_signed_and_owner(self):
        from books.imports.payment_receipt import run_import_payment_receipt as _run
        import openpyxl
        owner = _Owner.objects.create(name='测试货主', platform_owner_id='OWN_T')
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = '0701-1109'
        ws.append(['交易日', '交易时间', '交易类型', '交易金额', '流水号', '收(付)方名称', '收(付)方账号', '账号名称'])
        ws.append(['2026-07-01', '17:12:46', '提回对公户收款', '14000', 'FLOW1', '付款方A', '6222', '收款户'])
        ws.append(['2026-07-02', '17:26:31', '对公转账正常提出', '-12450', 'FLOW2', '付款方B', '6223', '收款户'])
        buf = _io.BytesIO(); wb.save(buf)
        f = _NamedBytes2(buf.getvalue(), 'cmb.xlsx')
        up = _run(f, User.objects.create_user('cmb_u', password='x'), default_owner=owner)
        self.assertEqual(up.status, 'success')
        rows = list(_PaymentReceipt.objects.order_by('external_ref'))
        self.assertEqual(len(rows), 2)
        by_ref = {r.external_ref: r for r in rows}
        self.assertEqual(by_ref['cmb:FLOW1'].amount, _Decimal('14000'))
        self.assertEqual(by_ref['cmb:FLOW2'].amount, _Decimal('-12450'))
        for r in rows:
            self.assertEqual(r.owner, owner)


# ===========================================================================
# 付款人批量建档 + 别名指派回填测试
# ===========================================================================
class B2bPayerAliasBuildTest(TestCase):
    def test_build_from_db_creates_unassigned_aliases(self):
        from django.core.management import call_command
        from io import StringIO
        owner = _Owner.objects.create(name='货主', platform_owner_id='O1')
        # 造两笔同付款人的收款明细
        for i in range(2):
            _PaymentReceipt.objects.create(external_ref=f'R{i}', channel_type='线上',
                pay_method='微信', payer_name='测试付款公司', amount=100, owner=owner)
        out = StringIO()
        call_command('build_payer_aliases', '--from-db', stdout=out)
        a = _CustomerAlias.objects.filter(alias_type='付款人名称',
                                          alias_value='测试付款公司').first()
        self.assertIsNotNone(a)
        self.assertIsNone(a.entity, '批量建档的锚点应留空待指派')
        self.assertIn('笔数=2', a.note)

    def test_build_is_idempotent(self):
        from django.core.management import call_command
        from io import StringIO
        owner = _Owner.objects.create(name='货主', platform_owner_id='O1')
        _PaymentReceipt.objects.create(external_ref='R1', channel_type='线上',
            pay_method='微信', payer_name='幂等测试公司', amount=100, owner=owner)
        call_command('build_payer_aliases', '--from-db', stdout=StringIO())
        n1 = _CustomerAlias.objects.count()
        call_command('build_payer_aliases', '--from-db', stdout=StringIO())
        self.assertEqual(_CustomerAlias.objects.count(), n1, '重复运行不应新增锚点')

    def test_alias_assign_backfills_receipts(self):
        from django.contrib.auth.models import User as _User
        from django.test import Client as _Client
        owner = _Owner.objects.create(name='货主', platform_owner_id='O1')
        ent = _CustomerEntity.objects.create(name='主体', tax_no='T1')
        a = _CustomerAlias.objects.create(alias_type='付款人名称', alias_value='张三公司',
                                          normalized_value='张三公司')
        for i in range(3):
            _PaymentReceipt.objects.create(external_ref=f'B{i}', channel_type='线下',
                pay_method='公对公', payer_name='张三公司', amount=100, owner=owner)
        from django.contrib.auth.models import Group as _Group
        u = _User.objects.create_superuser('assign_u', '', 'x')
        u.groups.add(_Group.objects.get_or_create(name='管理员')[0])
        c = _Client(); c.force_login(u)
        r = c.post('/normalize/', {'kind': 'alias', 'pk': a.pk, 'entity': ent.pk})
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.entity, ent)
        self.assertTrue(a.is_manual)
        self.assertEqual(_PaymentReceipt.objects.filter(entity=ent).count(), 3)


# ===========================================================================
# 客户主体清单 / 付款人对照表批量导入测试
# ===========================================================================
class B2bCustomerMapImportTest(TestCase):
    def _xlsx(self, headers, rows, sheet='Sheet1'):
        wb = _openpyxl2.Workbook(); ws = wb.active; ws.title = sheet
        ws.append(headers)
        for r in rows: ws.append(r)
        buf = _io.BytesIO(); wb.save(buf)
        return buf.getvalue()

    def test_import_entities_creates_and_updates(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        # 通过临时文件调用
        import tempfile, os
        data = self._xlsx(['主体名称', '税号', '平台客户ID'],
                          [['甲公司', 'TAX1', 'PID1'], ['乙公司', 'TAX2', '']])
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(data); path = f.name
        try:
            call_command('import_customer_map', '--entities', path, stdout=_io.StringIO())
            self.assertEqual(_CustomerEntity.objects.count(), 2)
            self.assertTrue(_CustomerEntity.objects.filter(name='甲公司', tax_no='TAX1').exists())
            # 重跑幂等
            call_command('import_customer_map', '--entities', path, stdout=_io.StringIO())
            self.assertEqual(_CustomerEntity.objects.count(), 2)
        finally:
            os.remove(path)

    def test_import_map_assigns_and_backfills(self):
        from django.core.management import call_command
        import tempfile, os
        owner = _Owner.objects.create(name='货主', platform_owner_id='O1')
        ent = _CustomerEntity.objects.create(name='目标主体', tax_no='TAXT')
        a = _CustomerAlias.objects.create(alias_type='付款人名称', alias_value='付款公司A',
                                          normalized_value='付款公司A')
        for i in range(2):
            _PaymentReceipt.objects.create(external_ref=f'M{i}', channel_type='线下',
                pay_method='公对公', payer_name='付款公司A', amount=100, owner=owner)
        data = self._xlsx(['付款人', '锚点类型', '目标主体'],
                          [['付款公司A', '付款人名称', '目标主体']])
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(data); path = f.name
        try:
            call_command('import_customer_map', '--map', path, stdout=_io.StringIO())
            a.refresh_from_db()
            self.assertEqual(a.entity, ent)
            self.assertEqual(_PaymentReceipt.objects.filter(entity=ent).count(), 2)
        finally:
            os.remove(path)

    def test_import_map_auto_creates_entity(self):
        from django.core.management import call_command
        import tempfile, os
        a = _CustomerAlias.objects.create(alias_type='付款人名称', alias_value='新付款人',
                                          normalized_value='新付款人')
        data = self._xlsx(['付款人', '目标主体'], [['新付款人', '自动建的主体']])
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(data); path = f.name
        try:
            call_command('import_customer_map', '--map', path, stdout=_io.StringIO())
            a.refresh_from_db()
            self.assertIsNotNone(a.entity)
            self.assertEqual(a.entity.name, '自动建的主体')
        finally:
            os.remove(path)


# ===========================================================================
# 店铺付款字典导入测试
# ===========================================================================
class B2bShopPaymentDictTest(TestCase):
    def test_import_shop_dict_assigns_and_leaves_blank(self):
        from django.core.management import call_command
        import openpyxl, tempfile, os
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Sheet1'
        ws.append(['店铺', '户名', '账号/用户号(微信/支付宝)'])
        ws.append(['店铺甲', '武汉甲公司', 'oQhzTEST0000000000000001'])
        ws.append(['', '', 'oQhzTEST0000000000000002'])  # 店铺空 → 留空待指派
        buf = _io.BytesIO(); wb.save(buf)
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(buf.getvalue()); path = f.name
        try:
            call_command('import_shop_payment_dict', '--file', path, stdout=_io.StringIO())
            e = _CustomerEntity.objects.get(name='店铺甲')
            a1 = _CustomerAlias.objects.get(normalized_value=_normalize_account_name('武汉甲公司'))
            a2 = _CustomerAlias.objects.get(normalized_value=_normalize_account_name('oQhzTEST0000000000000002'))
            self.assertEqual(a1.entity, e)
            self.assertIsNone(a2.entity, '店铺为空的锚点应留空待指派')
        finally:
            os.remove(path)

    def test_import_shop_dict_backfills_receipts(self):
        from django.core.management import call_command
        import openpyxl, tempfile, os
        owner = _Owner.objects.create(name='货主', platform_owner_id='O1')
        openid = 'oQhzTEST0000000000000003'
        _PaymentReceipt.objects.create(external_ref='S1', channel_type='线上',
            pay_method='微信', payer_account=openid, amount=100, owner=owner)
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Sheet1'
        ws.append(['店铺', '户名', '账号/用户号(微信/支付宝)'])
        ws.append(['店铺乙', '', openid])
        buf = _io.BytesIO(); wb.save(buf)
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(buf.getvalue()); path = f.name
        try:
            call_command('import_shop_payment_dict', '--file', path, stdout=_io.StringIO())
            e = _CustomerEntity.objects.get(name='店铺乙')
            self.assertEqual(_PaymentReceipt.objects.filter(entity=e).count(), 1)
        finally:
            os.remove(path)


class B2bShopLedgerTest(TestCase):
    """店铺往来账：字段口径、导入明细、穿透、抹零、编辑留痕、权限。"""

    def setUp(self):
        admin_grp, _ = Group.objects.get_or_create(name='管理员')
        self.admin = User.objects.create_user('shop_admin', password='pw')
        self.admin.groups.add(admin_grp)
        self.admin.profile.account_status = '已授权'
        self.admin.profile.save()
        self.client = Client()
        self.client.force_login(self.admin)

    def _order(self, **kw):
        defaults = dict(source_type='lakala_order', external_order_no='OD-1',
                        shop_name='测试店铺', total_amount=1000, refund_amount=50,
                        round_off=10, cost_amount=700, goods_amount=1080,
                        discount_amount=30, order_status='已完成', region='测试片区')
        defaults.update(kw)
        return Order.objects.create(**defaults)

    def test_order_shop_fields_and_margin(self):
        o = self._order()
        self.assertEqual(o.net_amount, 940)
        self.assertEqual(o.gross_profit, 240)
        self.assertAlmostEqual(o.gross_margin, 240 / 940, places=4)
        self.assertEqual(o.gross_margin_pct, 25.53)
        self.assertEqual(o.shop_name, '测试店铺')

    def test_refund_sum_columns_operator(self):
        raw = {'线上支付-退款额': '30', '到货付款-退货额': '20'}
        total = op_sum_columns('30', {'columns': ['线上支付-退款额', '到货付款-退货额']},
                               {'raw': raw})
        self.assertEqual(total, 50)

    def test_xlsx_composite_detail_split(self):
        from books.imports.lakala import split_detail_values
        d = {
            'detail_no': 'D1，D2', 'goods_name': '商品A，商品B',
            'sku': 'S1，S2', 'barcode': 'b1,11，b2,22',
            'sales_title': '甲 ￥300.00/件 ×1件 ￥300.00  乙 ￥200.00/件 ×1件 ￥200.00',
        }
        parts = split_detail_values(d)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]['barcode'], 'b1,11')  # ASCII 逗号属于同一商品
        self.assertEqual(parts[1]['goods_name'], '商品B')
        self.assertEqual(parts[1]['unit_price'], 200)
        self.assertEqual(parts[1]['subtotal'], 200)
        # 合并行无逐项金额时，后续拆分行不能沿用第一行金额
        merged = split_detail_values({
            'goods_name': '商品A，商品B', 'detail_no': 'D1',
            'qty': 2, 'unit_price': 10, 'subtotal': 20,
        })
        self.assertEqual(merged[0]['subtotal'], 20)
        self.assertEqual(merged[1]['subtotal'], 0)
        self.assertEqual(merged[1]['qty'], 0)

    def test_backfill_order_fields_and_details_from_raw(self):
        up = SourceUpload.objects.create(source_type='lakala_order', file_name='backfill-test')
        raw = {
            'id': 'BACKFILL-TEST', '客户Id': '回填店铺', '订单总额': '100',
            '商品总额': '105', '订单实收款': '100', '总成本': '90', '总折扣': '5', '片区': '测试片区',
            '明细id': 'X1，X2', '商品名': '商品甲，商品乙', '规格编码': 'K1，K2',
            '条码': 'B1，B2', '销售标题': '甲 ￥60.00/件 ×1件 ￥60.00  乙 ￥40.00/件 ×1件 ￥40.00',
        }
        rr = RawImportRow.objects.create(source_upload=up, data=raw)
        o = Order.objects.create(source_type='lakala_order',
                                 external_order_no='BACKFILL-TEST',
                                 raw_row=rr, total_amount=100, paid_amount=100)
        call_command('backfill_order_raw_fields', verbosity=0)
        o.refresh_from_db()
        self.assertEqual(o.goods_amount, 105)
        self.assertEqual(o.cost_amount, 90)
        self.assertEqual(o.discount_amount, 5)
        self.assertEqual(o.shop_name, '回填店铺')
        self.assertEqual(o.details.count(), 2)
        self.assertEqual(o.details.first().subtotal, 60)

    def test_rebuild_details_from_source_csv(self):
        import tempfile
        up = SourceUpload.objects.create(source_type='lakala_order', file_name='rebuild-test')
        raw = {'订单Id': 'REBUILD-TEST', '订单成本': '100', '商品名': '商品A，商品B'}
        rr = RawImportRow.objects.create(source_upload=up, data=raw)
        o = Order.objects.create(source_type='lakala_order',
                                 external_order_no='REBUILD-TEST',
                                 raw_row=rr, total_amount=200, paid_amount=200)
        OrderDetail.objects.create(order=o, goods_name='商品A', qty=1, subtotal=1)
        fd, path = tempfile.mkstemp(suffix='.csv')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=['订单Id', '明细Id', '商品名', '数量',
                                                   '单价', '小计', '折扣', '实付额'])
                w.writeheader()
                w.writerow({'订单Id': 'REBUILD-TEST', '明细Id': 'D1', '商品名': '商品A',
                            '数量': '2', '单价': '50', '小计': '100', '折扣': '0', '实付额': '0'})
                w.writerow({'订单Id': 'REBUILD-TEST', '明细Id': 'D2', '商品名': '商品B',
                            '数量': '3', '单价': '33.33', '小计': '99.99', '折扣': '0', '实付额': '0'})
            call_command('rebuild_order_details_from_csv', '--file', path, verbosity=0)
        finally:
            os.remove(path)
        ds = list(o.details.order_by('row_index'))
        self.assertEqual(len(ds), 2)
        self.assertEqual(ds[0].detail_no, 'D1')
        self.assertEqual(ds[1].qty, 3)
        from decimal import Decimal
        self.assertEqual(ds[1].subtotal, Decimal('99.99'))

    def test_zero_amount_with_cost_order_imported(self):
        """零单价赠品单有真实成本，应入库；金额成本均为 0 才跳过。"""
        mappings = [
            SimpleMapping('订单Id', 'order_no', [('none', {})]),
            SimpleMapping('客户名称', 'counterparty_raw_name', [('trim', {})]),
            SimpleMapping('订单成本', 'cost_amount', [('to_decimal', {})]),
            SimpleMapping('下单时间', 'order_date', [('to_date', {})]),
        ]
        raw = {'订单Id': 'GIFT-1', '客户名称': '赠品店铺',
               '订单成本': '6260', '下单时间': '2026-11-27 10:00:00'}
        o = _build_order('lakala_order', raw, mappings, None, {})
        self.assertIsNotNone(o)
        self.assertEqual(o.total_amount, 0)
        self.assertEqual(o.cost_amount, 6260)
        self.assertEqual(o.gross_profit, -6260)
        raw['订单成本'] = '0'
        self.assertIsNone(_build_order('lakala_order', raw, mappings, None, {}))

    def test_page_headers_and_export(self):
        import openpyxl
        from io import BytesIO
        self._order()
        r = self.client.get('/shop/ledger/')
        self.assertEqual(r.status_code, 200)
        for h in ['单据类型', '店铺名称', '订单id', '金额', '毛利', '毛利率', '业务主管', '区域']:
            self.assertContains(r, h)
        resp = self.client.get('/shop/ledger/export/')
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(BytesIO(resp.content))
        headers = [c.value for c in wb.active[1]]
        self.assertEqual(len(headers), 15)
        self.assertEqual(headers[0], '单据类型')

    def test_order_detail_penetration_and_roundoff(self):
        o = self._order()
        d = OrderDetail.objects.create(order=o, detail_no='D-1', goods_name='测试商品',
                                       qty=2, unit_price=30, subtotal=60, row_index=0)
        r = self.client.get('/shop/order/%s/' % o.pk)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, d.goods_name)
        r = self.client.post('/shop/order/%s/roundoff/' % o.pk, {'round_off': '20'})
        self.assertEqual(r.status_code, 302)
        o.refresh_from_db()
        self.assertEqual(o.round_off, 20)
        self.assertEqual(o.net_amount, 930)
        self.assertTrue(OperationLog.objects.filter(action='订单抹零').exists())
        # 非法值不落库
        self.client.post('/shop/order/%s/roundoff/' % o.pk, {'round_off': '-5'})
        o.refresh_from_db()
        self.assertEqual(o.round_off, 20)

    def test_receipt_shop_name_edit_audit(self):
        r = PaymentReceipt.objects.create(source_type='payment_detail', external_ref='R-1',
                                          amount=100, payer_name='付款人')
        resp = self.client.post('/shop/receipt/%s/edit/' % r.pk,
                                {'shop_name': '归一店铺', 'next': '/shop/receipts/'})
        self.assertEqual(resp.status_code, 302)
        r.refresh_from_db()
        self.assertEqual(r.shop_name, '归一店铺')
        self.assertEqual(r.edited_by, self.admin)
        self.assertIsNotNone(r.edited_at)
        self.assertTrue(OperationLog.objects.filter(action='编辑回款店铺名称').exists())

    def test_backfill_head_fields_from_source_csv(self):
        o = Order.objects.create(source_type='lakala_order',
                                 external_order_no='CSVHEAD-TEST',
                                 total_amount=100, paid_amount=100)
        fd, path = tempfile.mkstemp(suffix='.csv')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=['订单Id', '商品总额', '订单成本',
                                                   '总折扣', '客户名称', '片区'])
                w.writeheader()
                w.writerow({'订单Id': 'CSVHEAD-TEST', '商品总额': '105', '订单成本': '90',
                            '总折扣': '5', '客户名称': '源文件店铺', '片区': '源文件片区'})
            call_command('backfill_order_raw_fields', '--csv', path, verbosity=0)
        finally:
            os.remove(path)
        o.refresh_from_db()
        self.assertEqual(o.goods_amount, 105)
        self.assertEqual(o.cost_amount, 90)
        self.assertEqual(o.shop_name, '源文件店铺')

    def test_import_list_empty_status_label(self):
        SourceUpload.objects.create(source_type='lakala_order', file_name='dup.xlsx',
                                    uploaded_by=self.admin, status='empty', row_count=0)
        r = self.client.get('/import/list/')
        self.assertContains(r, '无新增')
        self.assertNotContains(r, 'bg-danger">失败')

    def test_registry_upsert_and_pages(self):
        reg = ShopBusinessRegistry.objects.create(shop_name='测试店铺', credit_code='91420100XX')
        reg2 = ShopBusinessRegistry.objects.filter(credit_code='91420100XX').first()
        self.assertEqual(reg, reg2)
        for url in ['/shop/receipts/', '/shop/collection/', '/shop/aging/',
                    '/shop/statements/', '/shop/registry/', '/shop/invoices/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_salesperson_row_permission(self):
        sp = SalespersonProfile.objects.create(name='业务员甲')
        u = User.objects.create_user('sp_user', password='pw')
        sp_grp, _ = Group.objects.get_or_create(name='业务员')
        u.groups.add(sp_grp)
        u.profile.account_status = '已授权'
        u.profile.salesperson = sp
        u.profile.save()
        sp.user = u
        sp.save()
        other = self._order(external_order_no='OD-OTHER', shop_name='他人店铺')
        own = self._order(external_order_no='OD-OWN', shop_name='本人店铺')
        own.salesperson = sp
        own.save(update_fields=['salesperson'])
        c = Client(); c.force_login(u)
        self.assertEqual(c.get('/shop/order/%s/' % own.pk).status_code, 200)
        self.assertEqual(c.get('/shop/order/%s/' % other.pk).status_code, 404)
