"""重置序列到当前 max(id)+1。

背景：0016 等数据迁移用显式 pk 播种了配置行，PostgreSQL 不会自动推进序列，
导致后续 create()（不指定 pk）撞到已占用的 pk。SQLite 无此问题。
本迁移在所有迁移末尾统一重置，使测试库/生产库的序列自洽。
"""
from django.core.management.color import no_style
from django.db import connection, migrations


def reset_sequences(apps, schema_editor):
    if connection.vendor != 'postgresql':
        return
    models = apps.get_app_config('books').get_models()
    sql_list = connection.ops.sequence_reset_sql(no_style(), models)
    with connection.cursor() as cursor:
        for sql in sql_list:
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ('books', '0026_alter_userprofile_account_status'),
    ]

    operations = [
        migrations.RunPython(reset_sequences, migrations.RunPython.noop),
    ]
