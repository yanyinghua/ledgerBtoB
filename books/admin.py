"""后台管理：维护主表、期初余额、名称映射库、客户-业务员映射、列映射配置等。"""
import csv
from django.contrib import admin
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import format_html
from books.models import (
    UserProfile, SourceUpload, RawImportRow, ColumnMappingConfig, MasterRecord,
    OpeningBalance, NameMapping, CustomerSalesperson, SalespersonChangeReminder,
    Operator, MappingStep, LogicRuleType, LogicRule, LoginLog, OperationLog,
    Department, SalespersonProfile, Merchant, ReceivingAccount, Counterparty,
    Order, OrderPayment, AccountNameMapping, AccountNameAlias, InvitedPhone, SmsCode,
    CustomerEntity, CustomerAlias, Owner, OwnerAccountMap, PaymentReceipt,
    OrderDetail, ShopBusinessRegistry, CollectionReminder, ShopStatement,
)
from books.forms import MappingStepForm, LogicRuleForm

# target_type -> (URL name, 是否带主键 pk)。批量接口会把多个 pk 用 、 拼接。
_TARGET_LINKS = {
    'MasterRecord': ('invoice_detail', True),
    'CustomerSalesperson': ('customer_edit', True),
    'NameMapping': ('namemap_edit', True),
    'SourceUpload': ('import_list', False),
    'AcceptanceOpeningBalance': ('acceptance_report', False),
    'AccountNameMapping': ('accountname_edit', True),
    'Order': ('shop_order_detail', True),
}


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'salesperson_name')
    search_fields = ('user__username', 'salesperson_name')


@admin.register(SourceUpload)
class SourceUploadAdmin(admin.ModelAdmin):
    list_display = ('id', 'source_type', 'file_name', 'uploaded_by', 'uploaded_at', 'status', 'row_count')
    list_filter = ('source_type', 'status')


@admin.register(RawImportRow)
class RawImportRowAdmin(admin.ModelAdmin):
    list_display = ('id', 'source_upload')


@admin.register(Operator)
class OperatorAdmin(admin.ModelAdmin):
    list_display = ('key', 'name', 'description')
    search_fields = ('key', 'name')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LoginLog)
class LoginLogAdmin(admin.ModelAdmin):
    """登录/登出/失败日志：仅查看，禁止增改删（防止审计记录被篡改）。"""
    list_display = ('username', 'user', 'action', 'ip', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('username', 'ip')
    date_hierarchy = 'created_at'
    readonly_fields = ('user', 'username', 'action', 'ip', 'user_agent', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OperationLog)
class OperationLogAdmin(admin.ModelAdmin):
    """业务操作日志：仅查看，禁止增改删（防止审计记录被篡改）。"""
    list_display = ('username', 'action', 'target_type', 'target_link', 'created_at')
    list_filter = ('action', 'target_type', 'created_at')
    search_fields = ('username', 'detail', 'target_id')
    date_hierarchy = 'created_at'
    readonly_fields = ('user', 'username', 'action', 'target_type',
                       'target_link', 'target_id', 'detail', 'ip', 'created_at')
    actions = ['export_selected_csv']

    def target_link(self, obj):
        """把对象标识渲染为可点击链接，跳转到对应业务记录页面。"""
        cfg = _TARGET_LINKS.get(obj.target_type)
        if not cfg:
            return obj.target_id or '—'
        viewname, uses_pk = cfg
        pks = [p for p in (obj.target_id or '').split('、') if p.strip()]
        if not pks:
            return '—'
        parts = []
        for pk in pks:
            try:
                url = reverse(viewname, args=[pk]) if uses_pk else reverse(viewname)
            except Exception:
                parts.append(pk)
                continue
            parts.append(format_html('<a href="{}" target="_blank">{}</a>', url, pk))
        return format_html('、'.join(parts))
    target_link.short_description = '对象标识（点击定位）'

    def export_selected_csv(self, request, queryset):
        """将选中的操作日志导出为 CSV（带 UTF-8 BOM，Excel 直接可打开）。"""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="operation_log.csv"'
        response.write('\ufeff')  # BOM，避免 Excel 中文乱码
        writer = csv.writer(response)
        writer.writerow(['id', '时间', '操作人', '动作', '对象类型', '对象标识', '详情', 'IP'])
        for log in queryset.order_by('created_at'):
            writer.writerow([log.id, log.created_at, log.username, log.action,
                            log.target_type, log.target_id, log.detail, log.ip])
        return response
    export_selected_csv.short_description = '导出选中记录为 CSV'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LogicRuleType)
class LogicRuleTypeAdmin(admin.ModelAdmin):
    list_display = ('key', 'name', 'description')
    search_fields = ('key', 'name')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class MappingStepInline(admin.StackedInline):
    model = MappingStep
    form = MappingStepForm
    extra = 0
    ordering = ('order',)


@admin.register(ColumnMappingConfig)
class ColumnMappingConfigAdmin(admin.ModelAdmin):
    list_display = ('source_type', 'source_column', 'target_field', 'transform', 'is_active', 'order')
    list_filter = ('source_type', 'target_field', 'is_active')
    search_fields = ('source_column',)
    inlines = [MappingStepInline]


@admin.register(LogicRule)
class LogicRuleAdmin(admin.ModelAdmin):
    form = LogicRuleForm
    list_display = ('source_type', 'rule_type', 'order', 'is_active')
    list_filter = ('source_type', 'rule_type', 'is_active')
    ordering = ('source_type', 'order')


@admin.register(MasterRecord)
class MasterRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'unit_name', 'biz_type', 'occur_date', 'category', 'debit', 'credit', 'tag', 'handler')
    list_filter = ('biz_type', 'category', 'tag', 'source_type', 'year')
    search_fields = ('unit_name', 'handler', 'invoice_number', 'remark')
    date_hierarchy = 'occur_date'
    actions = ['clear_handler', 'apply_customer_current']

    @admin.action(description='清空选中行经手人')
    def clear_handler(self, request, queryset):
        n = queryset.update(handler='')
        self.message_user(request, f'已清空 {n} 行经手人')

    @admin.action(description='应用各自客户现任业务员')
    def apply_customer_current(self, request, queryset):
        from books.models import CustomerSalesperson
        units = list(queryset.values_list('unit_name', flat=True))
        current_map = dict(CustomerSalesperson.objects.filter(
            unit_name__in=units, is_current=True).values_list('unit_name', 'salesperson'))
        done = 0
        for obj in queryset:
            cur = current_map.get(obj.unit_name)
            if cur and cur != obj.handler:
                obj.handler = cur
                obj.save(update_fields=['handler'])
                done += 1
        self.message_user(request, f'已为 {done} 行应用客户现任业务员')


@admin.register(OpeningBalance)
class OpeningBalanceAdmin(admin.ModelAdmin):
    list_display = ('unit_name', 'biz_type', 'occur_date', 'category', 'debit', 'credit', 'handler')
    search_fields = ('unit_name',)


@admin.register(NameMapping)
class NameMappingAdmin(admin.ModelAdmin):
    list_display = ('raw_name', 'canonical_name', 'tax_no')
    search_fields = ('raw_name', 'canonical_name', 'tax_no')


@admin.register(CustomerSalesperson)
class CustomerSalespersonAdmin(admin.ModelAdmin):
    list_display = ('unit_name', 'salesperson', 'is_current', 'conflict_flag', 'source', 'effective_date', 'manual_override')
    list_filter = ('is_current', 'source', 'manual_override')
    search_fields = ('unit_name', 'salesperson')
    actions = ['make_current', 'apply_derived_latest', 'set_manual_override', 'clear_manual_override']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        from books.salesperson import build_derived_latest_map
        self._derived_map = build_derived_latest_map()
        return qs

    def conflict_flag(self, obj):
        dmap = getattr(self, '_derived_map', None)
        if dmap is None:
            from books.salesperson import build_derived_latest_map
            dmap = build_derived_latest_map()
        if not obj.is_current:
            return ''
        rec = dmap.get(obj.unit_name)
        if not rec:
            return ''
        derived = rec[1]
        if derived and derived != obj.salesperson:
            lock = ' [锁定]' if obj.manual_override else ''
            return f'⚠ 冲突(派生:{derived}){lock}'
        return ''
    conflict_flag.short_description = '冲突提示'

    @admin.action(description='设为该客户现任')
    def make_current(self, request, queryset):
        updated = 0
        for obj in queryset:
            CustomerSalesperson.objects.filter(unit_name=obj.unit_name).exclude(pk=obj.pk).update(is_current=False)
            if not obj.is_current:
                obj.is_current = True
                obj.save(update_fields=['is_current'])
                updated += 1
        self.message_user(request, f'已设 {updated} 条为现任')

    @admin.action(description='应用派生最新经手人并设为现任')
    def apply_derived_latest(self, request, queryset):
        from books.imports.utils import upsert_customer_salesperson
        from books.salesperson import build_derived_latest_map, source_label_for
        dmap = build_derived_latest_map()
        done = 0
        for obj in queryset:
            rec = dmap.get(obj.unit_name)
            if not rec:
                continue
            date, handler, st = rec
            upsert_customer_salesperson(obj.unit_name, handler, source_label_for(st), date)
            done += 1
        self.message_user(request, f'已对 {done} 个客户应用派生最新经手人')

    @admin.action(description='标记手动锁定(不被自动改写)')
    def set_manual_override(self, request, queryset):
        n = queryset.update(manual_override=True)
        self.message_user(request, f'已锁定 {n} 条')

    @admin.action(description='取消手动锁定')
    def clear_manual_override(self, request, queryset):
        n = queryset.update(manual_override=False)
        self.message_user(request, f'已解锁 {n} 条')

    def change_view(self, request, object_id, form_url='', extra_context=None):
        from django.contrib import messages
        from django.http import HttpResponseRedirect
        resp = super().change_view(request, object_id, form_url, extra_context)
        if request.method == 'GET' and not isinstance(resp, HttpResponseRedirect):
            obj = self.get_object(request, object_id)
            if obj and obj.is_current:
                from books.salesperson import build_derived_latest_map
                rec = build_derived_latest_map().get(obj.unit_name)
                if rec and rec[1] and rec[1] != obj.salesperson:
                    messages.warning(request,
                        f'⚠ 冲突：历史派生最新经手人为「{rec[1]}」，当前现任为「{obj.salesperson}」。'
                        f'可在列表用「应用派生最新经手人」批量处理，或手动调整。')
        return resp


@admin.register(SalespersonChangeReminder)
class SalespersonChangeReminderAdmin(admin.ModelAdmin):
    list_display = ('unit_name', 'current_salesperson', 'suggested_salesperson',
                   'source', 'occur_date', 'status', 'created_at')
    list_filter = ('status', 'source')
    search_fields = ('unit_name', 'current_salesperson', 'suggested_salesperson')
    actions = ['accept_reminder', 'dismiss_reminder']

    @admin.action(description='采纳：设为该客户现任业务员')
    def accept_reminder(self, request, queryset):
        from django.utils import timezone
        from books.models import CustomerSalesperson, log_operation
        done = 0
        for rem in queryset.filter(status='待处理'):
            unit, sp = rem.unit_name, rem.suggested_salesperson
            row, _ = CustomerSalesperson.objects.get_or_create(
                unit_name=unit, salesperson=sp,
                defaults={'source': rem.source, 'effective_date': rem.occur_date,
                          'is_current': False})
            CustomerSalesperson.objects.filter(unit_name=unit).exclude(pk=row.pk).update(is_current=False)
            if not row.is_current:
                row.is_current = True
                row.save(update_fields=['is_current'])
            rem.status = '已采纳'
            rem.resolved_at = timezone.now()
            rem.resolved_by = request.user.username
            rem.save(update_fields=['status', 'resolved_at', 'resolved_by'])
            log_operation(request.user, '采纳业务员变更提醒',
                          target_type='业务员变更提醒', target_id=rem.pk,
                          detail=f'{unit}: {rem.current_salesperson}→{sp}', request=request)
            done += 1
        self.message_user(request, f'已采纳 {done} 条提醒并设为现任')

    @admin.action(description='忽略：标记为已忽略')
    def dismiss_reminder(self, request, queryset):
        from django.utils import timezone
        from books.models import log_operation
        n = 0
        for rem in queryset.filter(status='待处理'):
            rem.status = '已忽略'
            rem.resolved_at = timezone.now()
            rem.resolved_by = request.user.username
            rem.save(update_fields=['status', 'resolved_at', 'resolved_by'])
            log_operation(request.user, '忽略业务员变更提醒',
                          target_type='业务员变更提醒', target_id=rem.pk,
                          detail=rem.unit_name, request=request)
            n += 1
        self.message_user(request, f'已忽略 {n} 条提醒')


# ===========================================================================
# B2B 模型注册（组织/商户/账户/订单/匹配/认证）
# ===========================================================================
@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'parent', 'manager', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name', 'code')
    raw_id_fields = ('parent', 'manager')


@admin.register(SalespersonProfile)
class SalespersonProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'department', 'user', 'phone', 'is_active')
    list_filter = ('is_active', 'department')
    search_fields = ('name', 'phone')


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'merchant_type', 'salesperson', 'department', 'is_active')
    list_filter = ('merchant_type', 'is_active', 'department')
    search_fields = ('name', 'code')


@admin.register(ReceivingAccount)
class ReceivingAccountAdmin(admin.ModelAdmin):
    list_display = ('account_name', 'account_no', 'channel', 'account_type', 'owner_merchant', 'is_active')
    list_filter = ('channel', 'account_type', 'is_active')
    search_fields = ('account_name', 'account_no')


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ('name', 'party_type', 'tax_no', 'is_active')
    list_filter = ('party_type', 'is_active')
    search_fields = ('name', 'tax_no')


class OrderPaymentInline(admin.TabularInline):
    model = OrderPayment
    extra = 0
    fields = ('pay_date', 'amount', 'account', 'direction', 'external_ref')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('external_order_no', 'counterparty_raw_name', 'direction', 'order_date',
                    'total_amount', 'paid_amount', 'merchant', 'salesperson', 'match_status')
    list_filter = ('direction', 'match_status', 'source_type', 'department')
    search_fields = ('external_order_no', 'counterparty_raw_name', 'remark')
    date_hierarchy = 'order_date'
    raw_id_fields = ('counterparty', 'merchant', 'salesperson', 'department',
                     'receiving_account', 'source_upload', 'raw_row')
    inlines = [OrderPaymentInline]


@admin.register(AccountNameMapping)
class AccountNameMappingAdmin(admin.ModelAdmin):
    list_display = ('raw_name', 'normalized_name', 'counterparty', 'merchant', 'salesperson',
                    'receiving_account', 'is_manual', 'match_source', 'use_count', 'is_active')
    list_filter = ('is_manual', 'is_active', 'match_source')
    search_fields = ('raw_name', 'normalized_name')


@admin.register(AccountNameAlias)
class AccountNameAliasAdmin(admin.ModelAdmin):
    list_display = ('alias', 'canonical_raw_name', 'source', 'is_active')
    search_fields = ('alias', 'canonical_raw_name')


@admin.register(InvitedPhone)
class InvitedPhoneAdmin(admin.ModelAdmin):
    list_display = ('phone', 'role_hint', 'created_by', 'created_at')
    search_fields = ('phone',)


@admin.register(SmsCode)
class SmsCodeAdmin(admin.ModelAdmin):
    list_display = ('phone', 'purpose', 'code', 'is_used', 'expires_at', 'sent_at')
    list_filter = ('purpose', 'is_used')
    search_fields = ('phone',)


# ===========================================================================
# 往来单位归一（0028）
# ===========================================================================
@admin.register(CustomerEntity)
class CustomerEntityAdmin(admin.ModelAdmin):
    list_display = ('name', 'tax_no', 'platform_customer_id', 'party_type', 'is_active')
    list_filter = ('party_type', 'is_active')
    search_fields = ('name', 'tax_no', 'platform_customer_id')


@admin.register(CustomerAlias)
class CustomerAliasAdmin(admin.ModelAdmin):
    list_display = ('alias_type', 'alias_value', 'normalized_value', 'entity',
                    'platform_shop_id', 'is_manual', 'is_active', 'use_count')
    list_filter = ('alias_type', 'is_manual', 'is_active')
    search_fields = ('alias_value', 'normalized_value', 'platform_shop_id')


@admin.register(Owner)
class OwnerAdmin(admin.ModelAdmin):
    list_display = ('name', 'platform_owner_id', 'tax_no', 'is_active')
    search_fields = ('name', 'platform_owner_id', 'tax_no')


@admin.register(OwnerAccountMap)
class OwnerAccountMapAdmin(admin.ModelAdmin):
    list_display = ('owner', 'channel_type', 'pay_method', 'account', 'priority',
                    'is_default', 'is_active')
    list_filter = ('channel_type', 'pay_method', 'is_default', 'is_active')
    search_fields = ('owner__name', 'account__account_name')


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(admin.ModelAdmin):
    list_display = ('pay_date', 'payer_name', 'amount', 'channel_type', 'pay_method',
                    'owner', 'entity', 'receiving_account', 'shop_name', 'edited_by', 'match_status')
    list_filter = ('channel_type', 'pay_method', 'match_status')
    search_fields = ('payer_name', 'payer_account', 'external_ref', 'receiving_account_name',
                     'shop_name')
    date_hierarchy = 'pay_date'
    raw_id_fields = ('source_upload', 'raw_row', 'receiving_account', 'owner',
                     'entity', 'order')


@admin.register(OrderDetail)
class OrderDetailAdmin(admin.ModelAdmin):
    list_display = ('order', 'row_index', 'detail_no', 'goods_name', 'qty', 'unit_price', 'subtotal')
    search_fields = ('order__external_order_no', 'goods_name', 'detail_no')
    raw_id_fields = ('order',)


@admin.register(ShopBusinessRegistry)
class ShopBusinessRegistryAdmin(admin.ModelAdmin):
    list_display = ('shop_name', 'registered_name', 'credit_code', 'entity', 'source', 'updated_by')
    search_fields = ('shop_name', 'registered_name', 'credit_code')
    raw_id_fields = ('entity',)


@admin.register(CollectionReminder)
class CollectionReminderAdmin(admin.ModelAdmin):
    list_display = ('shop_name', 'remind_date', 'due_date', 'amount', 'salesperson', 'operator')
    search_fields = ('shop_name', 'content')


@admin.register(ShopStatement)
class ShopStatementAdmin(admin.ModelAdmin):
    list_display = ('letter_no', 'shop_name', 'period_from', 'period_to', 'amount', 'status')
    search_fields = ('letter_no', 'shop_name')
    raw_id_fields = ('entity',)
