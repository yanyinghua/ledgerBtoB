"""按「客户归一字典」回填收款明细的店铺名称（未填的才补）。

付款人名称/付款人账号 → CustomerAlias(entity) → CustomerEntity.name → shop_name。
同时回填 entity，并记录 edited_by/edited_at（系统补录，记为执行账号）。

用法：
  python3 manage.py backfill_receipt_shop --username admin
  python3 manage.py backfill_receipt_shop --dry-run
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from books.models import PaymentReceipt, CustomerAlias, log_operation
from books.imports.utils import normalize_account_name


class Command(BaseCommand):
    help = '按客户归一字典回填回款店铺名称'

    def add_arguments(self, parser):
        parser.add_argument('--username', default='', help='记为编辑人的用户名（默认首个超管）')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不写库')

    @transaction.atomic
    def handle(self, *args, **options):
        user = None
        if options['username']:
            user = User.objects.filter(username=options['username']).first()
            if not user:
                raise CommandError(f'用户不存在：{options["username"]}')
        if not user:
            user = User.objects.filter(is_superuser=True).first()

        cache = {}

        def resolve(alias_type, value):
            if not value:
                return None
            norm = normalize_account_name(value)
            key = (alias_type, norm)
            if key not in cache:
                a = CustomerAlias.objects.filter(alias_type=alias_type,
                                                 normalized_value=norm,
                                                 entity__isnull=False).first()
                cache[key] = a.entity if a else None
            return cache[key]

        updated = 0
        for r in PaymentReceipt.objects.filter(shop_name='').select_related('entity'):
            ent = resolve('付款人名称', r.payer_name) or resolve('付款人账号', r.payer_account)
            if not ent:
                continue
            if not options['dry_run']:
                r.shop_name = ent.name
                r.entity = ent
                r.edited_by = user
                r.edited_at = timezone.now()
                r.save(update_fields=['shop_name', 'entity', 'edited_by', 'edited_at', 'updated_at'])
            updated += 1
        if not options['dry_run'] and updated:
            log_operation(user, '补录回款店铺名称', target_type='PaymentReceipt',
                          target_id='', detail=f'按归一字典回填 {updated} 笔')
        self.stdout.write(self.style.SUCCESS(
            f'回填回款店铺名：{updated} 笔' + ('（dry-run）' if options['dry_run'] else '')))
