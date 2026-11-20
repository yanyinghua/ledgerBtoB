"""数据库在线一致性备份与恢复（SQLite / PostgreSQL 双引擎）。

为什么不用 cp：
- SQLite：官方明确警告，在另一进程（gunicorn）正在读写时直接 cp 数据库文件，
  拷贝过程中若发生写入，可能得到一份「损坏」的副本。本命令改用 Python sqlite3
  的 Connection.backup() 读取一致性快照，并做 PRAGMA integrity_check。
- PostgreSQL：用 pg_dump -Fc（自定义格式，便于 pg_restore 选择性恢复与校验），
  校验用 pg_restore --list。

用法：
    python3 manage.py backup_db                       # 热备到 backups/<时间戳>.<ext>
    python3 manage.py backup_db --dest /var/backups/ledger/db-xxx.dump
    python3 manage.py backup_db --keep 20             # 备份后按分级策略清理旧备份
    python3 manage.py backup_db --keep 20 --keep-days 30   # 回溯窗口放宽到 30 天
    python3 manage.py backup_db --keep 20 --dry-run   # 只打印将清理哪些，不实际删除
    python3 manage.py backup_db --restore backups/xxx.dump   # 恢复到当前库（先自动安全备份）
    python3 manage.py backup_db --check backups/xxx.dump     # 仅校验备份完整性

说明：恢复会覆盖当前库，故 --restore 前会自动对当前库做一份安全备份，便于回滚。

保留策略（--keep）：每次部署都会新增一份完整副本，只增不减会撑爆磁盘，故提供
--keep 在备份成功后清理旧文件。采用分级保留（三层并集，命中任一层即保留）：
    1. 最近 N 份（--keep N）
    2. 最近 D 天每天最新一份（--keep-days D，默认 14）
    3. 每个自然月最新一份（永久存档）
第 2 层不可少：只按"最近 N 份"的话，一天连续部署几十次就会把历史冲干净
（实测 20 份仅覆盖 1.5 天）；按天分桶后密集部署只挤占当天的桶。

清理**只认** db-<时间戳>.sqlite3 / db-<时间戳>.dump 这两种例行备份名，
pre-restore-* 与 db.pre-*（恢复/重跑/清备注等破坏性操作前的安全备份）一律不动。
"""
import os
import re
import subprocess
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# 例行备份文件名：db-YYYYMMDD-HHMMSS.sqlite3 (SQLite) 或 db-YYYYMMDD-HHMMSS.dump (PostgreSQL)
# 用 ^$ 全串锚定，且分隔符是 '-'，因此 db.pre-sync-xxx.sqlite3 这类安全备份不会命中。
ROUTINE_BACKUP_RE = re.compile(r'^db-\d{8}-\d{6}\.(sqlite3|dump)$')


# ---------------------------------------------------------------------------
# 引擎分派
# ---------------------------------------------------------------------------
def _vendor():
    """返回 'sqlite' 或 'postgresql'。"""
    return connections['default'].vendor


def _db_name():
    """SQLite 为文件路径；PostgreSQL 为库名。"""
    return connections['default'].settings_dict['NAME']


def _ext():
    return 'dump' if _vendor() == 'postgresql' else 'sqlite3'


# ---------------------------------------------------------------------------
# SQLite 专属实现
# ---------------------------------------------------------------------------
def _sqlite_integrity_ok(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    finally:
        conn.close()
    return all(r[0] == 'ok' for r in rows)


def _sqlite_online_copy(src_path, dst_path):
    """用 SQLite 在线备份 API 把 src 一致性地拷贝到 dst（dst 先被清空重建）。"""
    import sqlite3
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


# ---------------------------------------------------------------------------
# PostgreSQL 专属实现（依赖 pg_dump / pg_restore，连接信息走 libpq 环境变量，
# 避免把密码写进命令行参数）
# ---------------------------------------------------------------------------
def _pg_env():
    cfg = connections['default'].settings_dict
    env = dict(os.environ)
    env['PGHOST'] = cfg.get('HOST') or '127.0.0.1'
    env['PGPORT'] = str(cfg.get('PORT') or '5432')
    env['PGUSER'] = cfg.get('USER') or 'postgres'
    env['PGPASSWORD'] = cfg.get('PASSWORD') or ''
    env['PGDATABASE'] = cfg.get('NAME') or 'ledger'
    return env


def _pg_dump(dest, env):
    subprocess.run(['pg_dump', '-Fc', '-f', dest], env=env, check=True)


def _pg_restore_list_ok(path, env):
    """pg_restore --list 对有效 dump 返回 0 并列出内容；损坏或不完整则非 0。"""
    r = subprocess.run(['pg_restore', '--list', path], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _pg_restore(src, env):
    # -c 先 drop 相关对象再建，适合整库恢复；失败即抛 CalledProcessError。
    subprocess.run(['pg_restore', '-c', '-d', env['PGDATABASE'], src],
                   env=env, check=True)


# ---------------------------------------------------------------------------
# 引擎无关门面
# ---------------------------------------------------------------------------
def _backup(dest):
    if _vendor() == 'postgresql':
        _pg_dump(dest, _pg_env())
    else:
        _sqlite_online_copy(_db_name(), dest)


def _integrity_ok(path):
    if _vendor() == 'postgresql':
        return _pg_restore_list_ok(path, _pg_env())
    return _sqlite_integrity_ok(path)


def _online_copy(src, dst):
    """把当前库拷贝到 dst（用于 --restore 前的安全备份，或同引擎拷贝）。"""
    if _vendor() == 'postgresql':
        _pg_dump(dst, _pg_env())
    else:
        _sqlite_online_copy(src, dst)


def _restore(src):
    if _vendor() == 'postgresql':
        _pg_restore(src, _pg_env())
    else:
        _sqlite_online_copy(src, _db_name())


# ---------------------------------------------------------------------------
# 旋转清理（引擎无关）
# ---------------------------------------------------------------------------
def _prune_backups(directory, keep, days=14, keep_newest_per_month=True):
    """清理目录下多余的例行备份，返回 (已删列表, 释放字节数)。

    分级保留（三层并集，命中任一层即保留）：
      1. 最近 keep 份         —— 保住刚出问题时最近几次部署的现场
      2. 最近 days 天每天最新一份 —— 回溯能力按"天"衡量，不受部署频率影响
      3. 每个自然月最新一份     —— 长期存档，永不过期

    第 2 层是关键：只按"最近 N 份"的话，一天连续部署几十次就会把所有历史
    冲干净（实测 20 份只覆盖 1.5 天）；按天分桶后，密集部署只挤占当天的桶。

    只处理匹配 ROUTINE_BACKUP_RE 的文件，安全备份不在清理范围内。
    """
    if not os.path.isdir(directory):
        return [], 0

    routine = [n for n in os.listdir(directory) if ROUTINE_BACKUP_RE.match(n)]
    # 文件名里的时间戳即备份时刻，按名排序等价于按时间排序，且不受
    # 复制/rsync 改动 mtime 的影响，比 getmtime 更可靠。
    routine.sort(reverse=True)

    # 第 1 层：最近 keep 份
    protected = set(routine[:keep])

    # 第 2 层：最近 days 天，每天最新一份
    # 以"最新备份所在日期"而非今天为基准，避免长期没部署时把历史全判为过期。
    if days > 0 and routine:
        newest_day = routine[0][3:11]  # db-YYYYMMDD-... → YYYYMMDD
        cutoff = (datetime.strptime(newest_day, '%Y%m%d') - timedelta(days=days - 1)).strftime('%Y%m%d')
        seen_days = set()
        for name in routine:
            day = name[3:11]
            if day < cutoff:
                break  # 已按时间倒序，再往后只会更旧
            if day not in seen_days:
                seen_days.add(day)
                protected.add(name)

    # 第 3 层：每月最新一份
    if keep_newest_per_month:
        seen_months = set()
        for name in routine:
            month = name[3:9]  # db-YYYYMMDD-... → YYYYMM
            if month not in seen_months:
                seen_months.add(month)
                protected.add(name)

    removed, freed = [], 0
    for name in routine:
        if name in protected:
            continue
        path = os.path.join(directory, name)
        freed += os.path.getsize(path)
        removed.append(path)
    return removed, freed


class Command(BaseCommand):
    help = '数据库在线一致性备份（替代 cp，避免活库拷贝损坏）与恢复，支持 SQLite / PostgreSQL'

    def add_arguments(self, parser):
        parser.add_argument('--dest', default=None,
                            help='备份输出路径（默认 backups/<时间戳>.<sqlite3|dump>）')
        parser.add_argument('--restore', metavar='FILE', default=None,
                            help='从指定备份恢复到当前库（覆盖，自动先做安全备份）')
        parser.add_argument('--check', metavar='FILE', default=None,
                            help='仅校验指定备份文件的完整性')
        parser.add_argument('--backup-dir', default='backups',
                            help='默认备份目录（当 --dest 未指定时使用）')
        parser.add_argument('--keep', type=int, default=None, metavar='N',
                            help='启用清理并保留最近 N 份例行备份；不传则完全不清理。'
                                 '实际保留 = 最近 N 份 ∪ 最近 --keep-days 天每天最新一份 '
                                 '∪ 每月最新一份。安全备份 pre-restore-*/db.pre-* 永不清理')
        parser.add_argument('--keep-days', type=int, default=14, metavar='D',
                            help='最近 D 天内每天至少保留一份（默认 14；设 0 关闭该层）')
        parser.add_argument('--dry-run', action='store_true',
                            help='配合 --keep 使用：只打印将清理的文件，不实际删除')

    def handle(self, *args, **options):
        keep = options['keep']
        if keep is not None and keep < 1:
            raise CommandError('--keep 至少为 1（不允许清空所有例行备份）')

        if options['check']:
            path = options['check']
            if not os.path.exists(path):
                self.stderr.write(f'备份文件不存在：{path}')
                return
            ok = _integrity_ok(path)
            self.stdout.write(('OK' if ok else 'FAIL') + f'  integrity_check: {path}')
            if not ok:
                self.stderr.write(self.style.ERROR('备份文件不完整，不可恢复。'))
            return

        if options['restore']:
            src = options['restore']
            if not os.path.exists(src):
                self.stderr.write(f'备份文件不存在：{src}')
                return
            if not _integrity_ok(src):
                self.stderr.write(self.style.ERROR(f'备份文件不完整，拒绝恢复：{src}'))
                return
            # 恢复前自动安全备份当前库
            safe = f'{options["backup_dir"]}/pre-restore-{datetime.now():%Y%m%d-%H%M%S}.{_ext()}'
            os.makedirs(options['backup_dir'], exist_ok=True)
            _online_copy(_db_name(), safe)
            self.stdout.write(self.style.WARNING(f'已对当前库做安全备份：{safe}'))
            _restore(src)
            self.stdout.write(self.style.SUCCESS(f'已从 {src} 恢复到 {_db_name()}'))
            return

        # 默认：备份
        dest = options['dest']
        if not dest:
            os.makedirs(options['backup_dir'], exist_ok=True)
            dest = f'{options["backup_dir"]}/db-{datetime.now():%Y%m%d-%H%M%S}.{_ext()}'

        # SQLite 才需要检查文件路径是否存在；PostgreSQL 是库名，不做此检查。
        if _vendor() == 'sqlite3' and not os.path.exists(_db_name()):
            self.stderr.write(f'当前数据库不存在：{_db_name()}')
            return

        _backup(dest)
        ok = _integrity_ok(dest)
        size = os.path.getsize(dest)
        self.stdout.write(f'已备份：{dest}（{size/1024/1024:.1f} MB）')
        if not ok:
            self.stderr.write(self.style.ERROR('备份完整性校验未通过，请重试。'))
            # 新备份不可用时绝不清理旧备份，否则会在"手上没有可用备份"时销毁退路
            if keep is not None:
                self.stderr.write(self.style.ERROR('因备份校验失败，已跳过旧备份清理。'))
            return
        self.stdout.write(self.style.SUCCESS('完整性校验通过，备份可恢复。'))

        if keep is not None:
            self._prune(os.path.dirname(os.path.abspath(dest)), keep,
                        options['keep_days'], options['dry_run'])

    def _prune(self, directory, keep, keep_days, dry_run):
        policy = f'保留最近 {keep} 份 + 最近 {keep_days} 天每天一份 + 每月最新一份'
        removed, freed = _prune_backups(directory, keep, days=keep_days)
        if not removed:
            self.stdout.write(f'无可清理的例行备份（{policy}）。')
            return

        verb = '将清理' if dry_run else '已清理'
        for path in removed:
            self.stdout.write(f'  {verb}：{os.path.basename(path)}')
            if not dry_run:
                os.remove(path)
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(removed)} 份旧备份，{"可" if dry_run else "已"}释放 {freed/1024/1024:.1f} MB'
            f'（{policy}）'))
