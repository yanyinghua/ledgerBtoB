"""校准主表经手人 / 业务员并刷新「客户-业务员映射」的现任。

做什么：
  1. 按历史记录推导每客户的「最新经手人」(occur_date 最新、非空的一行)，
     写入/刷新 CustomerSalesperson.is_current（管理员主动校正，auto_current=True）。
  2. 把被本次校准采纳的「业务员变更提醒」标记为「已采纳」。
  3. 输出「派生最新 ≠ 后台现任」的冲突清单（锁定等未自动采纳情形），供管理员核对。

说明：开票员也能当业务员，不再硬性排除内部人员；导入时与现任不一致仅写提醒，
      本命令是管理员显式校正入口，会直接翻现任。
参数：--dry-run 仅预览，不写库。
"""
from django.core.management.base import BaseCommand

from books.imports.utils import upsert_customer_salesperson
from books.salesperson import build_derived_latest_map, source_label_for
from books.models import CustomerSalesperson, SalespersonChangeReminder, log_operation


class Command(BaseCommand):
    help = '校准主表经手人并刷新客户-业务员映射现任，输出冲突清单'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')

    def handle(self, *args, **options):
        dry = options['dry_run']

        # 1) 派生最新经手人并刷新现任（管理员主动校正）
        dmap = build_derived_latest_map()
        if dry:
            self.stdout.write(f'[预览] 待刷新现任的客户数：{len(dmap)}')
        else:
            for unit, (date, handler, st) in dmap.items():
                upsert_customer_salesperson(unit, handler, source_label_for(st), date,
                                           auto_current=True)
            # 校准已把派生最新设为现任：把对应「待处理」提醒标记为已采纳
            accepted = 0
            for unit, (_, handler, _) in dmap.items():
                if not handler:
                    continue
                accepted += SalespersonChangeReminder.objects.filter(
                    unit_name=unit, suggested_salesperson=handler, status='待处理'
                ).update(status='已采纳')
            self.stdout.write(
                f'已处理客户数：{len(dmap)}（现任按历史最新刷新）；采纳相关提醒 {accepted} 条')

        # 2) 冲突清单：派生最新 ≠ 后台现任（锁定等未自动采纳情形）
        conflicts = []
        for unit, (_, handler, _) in dmap.items():
            cur = CustomerSalesperson.objects.filter(unit_name=unit, is_current=True).first()
            if cur and cur.salesperson != handler:
                conflicts.append((unit, handler, cur.salesperson, cur.manual_override))
        self.stdout.write(self.style.WARNING(
            f'\n冲突（派生最新 ≠ 后台现任）客户数：{len(conflicts)}'))
        for unit, derived, current, locked in conflicts[:50]:
            tag = ' [手动锁定]' if locked else ''
            self.stdout.write(f'  {unit}: 派生={derived} | 现任={current}{tag}')
        if len(conflicts) > 50:
            self.stdout.write(f'  …另有 {len(conflicts) - 50} 个冲突（用后台「客户-业务员映射」页查看 ⚠ 标记）')

        if dry:
            self.stdout.write(self.style.WARNING('（dry-run）未做任何写入。'))
        else:
            log_operation(None, '校准现任', target_type='系统', target_id='',
                          detail=f'刷新现任客户数 {len(dmap)}；采纳提醒 {accepted} 条；冲突 {len(conflicts)} 个',
                          username='系统(calibrate_handlers)')
            self.stdout.write(self.style.SUCCESS('校准完成。可在后台「客户-业务员映射」查看 ⚠ 冲突标记并批量处理。'))
