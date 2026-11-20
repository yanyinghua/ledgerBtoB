"""按对象标识（target_id）反查操作日志及其关联的业务记录。

用法：
  # 反查某个对象标识（如交易记录主键 216863）被哪些操作日志引用
  python manage.py trace_oplog 216863

  # 反查某条操作日志本身（按 OperationLog 自己的主键）
  python manage.py trace_oplog --log-id 1

  # 限定对象类型（避免跨类型误匹配）
  python manage.py trace_oplog 216863 --type MasterRecord

说明：
  OperationLog.target_id 存的是「被操作对象的主键」。对“更新标注/备注”这类批量接口，
  多个主键会用 、 拼接（如 "216863、216864"）。本命令会按 target_type 映射到对应模型，
  把每条日志关联的业务记录实体也一并打印出来，便于审计追溯。
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model

from books.models import (
    OperationLog, MasterRecord, CustomerSalesperson, NameMapping,
    SourceUpload, AcceptanceOpeningBalance,
)

# target_type 字符串 -> (模型类, 展示字段列表)
_RESOLVERS = {
    'MasterRecord': (MasterRecord, [
        'id', 'unit_name', 'biz_type', 'category', 'debit', 'credit',
        'occur_date', 'handler', 'tag', 'remark', 'source_type', 'invoice_number',
    ]),
    'CustomerSalesperson': (CustomerSalesperson, [
        'id', 'unit_name', 'salesperson', 'is_current', 'credit_term',
    ]),
    'NameMapping': (NameMapping, [
        'id', 'raw_name', 'canonical_name', 'tax_no',
    ]),
    'SourceUpload': (SourceUpload, [
        'id', 'source_type', 'file_name', 'status', 'row_count', 'created_at',
    ]),
    'AcceptanceOpeningBalance': (AcceptanceOpeningBalance, [
        'id', 'as_of_date', 'amount', 'remark',
    ]),
}


def _split_pks(target_id):
    return [p for p in (target_id or '').split('、') if p.strip()]


class Command(BaseCommand):
    help = '按对象标识(target_id)或操作日志主键反查操作日志及其关联业务记录'

    def add_arguments(self, parser):
        parser.add_argument('identifier', nargs='?',
                            help='对象标识(target_id)，如交易记录主键 216863')
        parser.add_argument('--log-id', type=int, default=None,
                            help='按 OperationLog 自身主键反查')
        parser.add_argument('--type', dest='target_type', default=None,
                            help='限定 target_type（如 MasterRecord），避免跨类型误匹配')

    def handle(self, *args, **options):
        identifier = options['identifier']
        log_id = options['log_id']
        target_type = options['target_type']

        if log_id is not None:
            logs = OperationLog.objects.filter(pk=log_id)
            if not logs.exists():
                raise CommandError(f'未找到 OperationLog id={log_id}')
        elif identifier:
            logs = OperationLog.objects.filter(target_id__contains=identifier)
            if target_type:
                logs = logs.filter(target_type=target_type)
            # 精确匹配：identifier 必须是该 target_id 中某个完整主键
            logs = [lg for lg in logs
                    if identifier in _split_pks(lg.target_id)]
            if not logs:
                raise CommandError(
                    f'未找到 target_id 含 "{identifier}"'
                    + (f' 且 target_type={target_type}' if target_type else '')
                    + ' 的操作日志')
        else:
            raise CommandError('请提供 identifier（对象标识）或 --log-id')

        for lg in logs:
            self._print_log(lg)

    def _print_log(self, lg: OperationLog):
        self.stdout.write('=' * 60)
        self.stdout.write(
            f'操作日志 #{lg.id} | 操作人={lg.username} | 动作={lg.action}')
        self.stdout.write(
            f'对象类型={lg.target_type} | 对象标识={lg.target_id!r}')
        self.stdout.write(f'详情={lg.detail} | IP={lg.ip} | 时间={lg.created_at}')
        self._resolve(lg.target_type, lg.target_id)

    def _resolve(self, ttype, target_id):
        if ttype not in _RESOLVERS:
            self.stdout.write(f'  （target_type={ttype!r} 无对应解析器，跳过关联记录）')
            return
        model, fields = _RESOLVERS[ttype]
        pks = _split_pks(target_id)
        if not pks:
            self.stdout.write('  （对象标识为空，无具体行）')
            return
        objs = model.objects.filter(pk__in=pks)
        if not objs.exists():
            self.stdout.write(
                f'  ⚠ 关联表 {model.__name__} 中找不到 pk∈{pks} 的记录'
                f'（可能已被删除/重跑导入清除）')
            return
        for obj in objs:
            self.stdout.write(f'  ▶ {model.__name__} #{obj.pk}:')
            for f in fields:
                if f == 'id':
                    continue
                val = getattr(obj, f, None)
                if isinstance(val, Model):
                    val = getattr(val, 'name', val)
                self.stdout.write(f'      {f} = {val!r}')
