"""表头排序链接。

用法（视图把 books.sorting.SortState 放进上下文的 `sort`）：
    {% load sortable %}
    <th>{% sort_link sort "debit" "借方" "desc" %}</th>

sort 为 None 时输出纯文本表头（该列不可排序）。
"""
from django import template

register = template.Library()


@register.inclusion_tag('books/_sort_link.html')
def sort_link(sort, key, label, first_dir='asc'):
    if sort is None:
        return {'label': label}
    active = sort.is_active(key)
    nxt = sort.next_dir(key, first_dir)
    if active:
        zh = '升序' if sort.dir == 'asc' else '降序'
        other = '降序' if sort.dir == 'asc' else '升序'
        title = f'当前按{label}{zh}排列，点击改为{other}'
    else:
        title = f'按{label}{"降序" if nxt == "desc" else "升序"}排列'
    return {
        'label': label,
        'href': sort.url_for(key, nxt),
        'indicator': sort.indicator(key),
        'active': active,
        'title': title,
    }
