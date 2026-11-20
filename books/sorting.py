"""表头点击排序：把 URL 上的 sort/dir 解析成 ORM order_by 或列表 sorted。

设计要点
--------
1. 白名单：sort 只能取调用方声明的 key，非法值一律回退该表默认顺序。
   绝不把 GET 值直接拼进 order_by()（否则可被注入任意字段/跨关系查询）。
2. 两种数据源：
   - sort_queryset：给 QuerySet 用，返回 (qs, state)。
   - sort_rows：给 Python 列表（聚合结果 / 内存里算好的行）用，返回 (rows, state)。
3. 同一页面多张表时用不同的 param 前缀（如 pkg_sort / det_sort）互相隔离。
4. state 交给模板标签 {% sort_link %} 渲染表头链接与 ▲/▼ 指示。
"""
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import F

ASC = 'asc'
DESC = 'desc'
_UP, _DOWN, _NONE = '▲', '▼', '⇅'


def _valid_dir(v):
    return v if v in (ASC, DESC) else None


def _resolve(GET, param, fields, default_key, default_dir):
    """解析 (key, dir, explicit)。

    - 无排序参数：key 取默认列（表头显示箭头，点击即可反向），ordering 用调用方的 default
    - 参数非法：key 置空（不显示箭头），同样回退 default
    - 方向非法：用该列首次点击方向
    """
    raw = (GET.get(param) or '').strip()
    if not raw:
        return default_key, _valid_dir(default_dir) or ASC, False
    if raw not in fields:
        return '', _valid_dir(default_dir) or ASC, False
    dir_ = _valid_dir((GET.get(param + '_dir') or '').strip())
    if dir_ is None:
        dir_ = _default_dir(fields[raw], default_dir)
    return raw, dir_, True


def _default_dir(spec, default_dir):
    """首次点击方向：spec 里声明优先，否则用调用方给的 default_dir。"""
    if len(spec) > 1 and spec[1] in (ASC, DESC):
        return spec[1]
    return _valid_dir(default_dir) or ASC


class SortState:
    """当前排序状态；模板据此渲染表头。"""

    def __init__(self, key, dir_, sort_param, base_qs, fields):
        self.key = key or ''
        self.dir = dir_
        self.sort_param = sort_param
        self.dir_param = sort_param + '_dir'
        self.base_qs = base_qs
        self.fields = fields

    @property
    def active(self):
        return bool(self.key)

    def is_active(self, key):
        return self.key == key

    def indicator(self, key):
        if not self.is_active(key):
            return _NONE
        return _UP if self.dir == ASC else _DOWN

    def next_dir(self, key, first_dir=ASC):
        if self.is_active(key):
            return DESC if self.dir == ASC else ASC
        return first_dir

    def url_for(self, key, dir_):
        parts = [self.base_qs, urlencode({self.sort_param: key}),
                 urlencode({self.dir_param: dir_})]
        return '?' + '&'.join(p for p in parts if p)

    def params(self):
        """当前排序的查询串片段（用于手写链接，如分页/每页条数）。"""
        if not self.active:
            return []
        return [urlencode({self.sort_param: self.key}),
                urlencode({self.dir_param: self.dir})]


def _base_querystring(GET, sort_param, drop):
    keys = {sort_param, sort_param + '_dir'} | set(drop)
    items = [(k, v) for k, v in GET.items() if k not in keys and v != '']
    return urlencode(items)


def _orm_expr(col, dir_):
    """空值统一排在最后，避免 SQLite 与 PostgreSQL 的 NULL 位置差异。"""
    f = F(col.lstrip('-'))
    return f.desc(nulls_last=True) if dir_ == DESC else f.asc(nulls_last=True)


def sort_queryset(qs, GET, *, fields, default, param='sort', default_key='',
                  default_dir=ASC, tiebreak='id'):
    """按白名单给 QuerySet 排序，返回 (qs, SortState)。

    fields: {key: (字段或字段元组, 首次点击方向)}；default: 无排序参数时的 order_by 列表。
    """
    key, dir_, explicit = _resolve(GET, param, fields, default_key, default_dir)
    if explicit:
        target = fields[key][0]
        cols = (target,) if isinstance(target, str) else tuple(target)
        ordering = [_orm_expr(c, dir_) for c in cols]
        if tiebreak:
            ordering.append(_orm_expr(tiebreak.lstrip('-'), dir_))
    else:
        ordering = list(default)
    state = SortState(key, dir_, param, _base_querystring(GET, param, ('page', 'per_page')), fields)
    return qs.order_by(*ordering), state


def _row_value(row, target):
    if callable(target):
        return target(row)
    if isinstance(row, dict):
        return row.get(target)
    return getattr(row, target, None)


def _row_key(value):
    """空值恒排最后；数字与字符串分开比较，避免 Decimal/str 直接比较抛错。"""
    if value is None or value == '':
        return (2, 0.0, '')
    if isinstance(value, bool):
        return (0, float(value), '')
    if isinstance(value, (int, float, Decimal)):
        return (0, float(value), '')
    return (1, 0.0, str(value))


def sort_rows(rows, GET, *, fields, param='sort', default_key='', default_dir=ASC):
    """给内存列表（聚合结果或已算好的行）排序，返回 (rows, SortState)。

    default_key/default_dir 只影响表头显示（列表本身已由调用方按默认顺序生成），
    不带排序参数时原样返回 rows，不改动既有顺序。
    """
    key, dir_, explicit = _resolve(GET, param, fields, default_key, default_dir)
    state = SortState(key, dir_, param, _base_querystring(GET, param, ('page', 'per_page')), fields)
    if not explicit:
        return rows, state
    target = fields[key][0]
    return sorted(rows, key=lambda r: _row_key(_row_value(r, target)),
                  reverse=(dir_ == DESC)), state


def page_querystring(GET, drop=('page', 'per_page')):
    """分页链接用的查询串：保留筛选与排序，剔除 page/per_page。"""
    return urlencode([(k, v) for k, v in GET.items() if k not in drop and v != ''])


class SortableListMixin:
    """Django ListView 的表头排序混入：子类声明 sort_fields / sort_default_*。"""

    sort_fields = {}
    sort_default_key = ''
    sort_default_dir = ASC
    sort_param = 'sort'
    sort_tiebreak = 'id'

    def apply_sort(self, qs):
        """子类若自己覆写了 get_queryset，取完 queryset 后调这个即可。"""
        qs, self._sort_state = sort_queryset(
            qs, self.request.GET, fields=self.sort_fields,
            default=list(self.ordering or []), param=self.sort_param,
            default_key=self.sort_default_key, default_dir=self.sort_default_dir,
            tiebreak=self.sort_tiebreak)
        return qs

    def get_queryset(self):
        return self.apply_sort(super().get_queryset())

    def get_ordering(self):
        # 排序已在 get_queryset 内应用，避免 ListView 再叠加 ordering
        return None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sort'] = getattr(self, '_sort_state', None)
        return ctx
