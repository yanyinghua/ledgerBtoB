"""回填历史原始行的 row_hash，使增量导入能基于哈希去重。

哈希算法与 books.imports.engine._canon_hash 保持一致：
md5(json.dumps(data, sort_keys=True, ensure_ascii=False))。
"""
import hashlib
import json

from django.db import migrations


def _canon_hash(d):
    return hashlib.md5(
        json.dumps(d, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def backfill_row_hash(apps, schema_editor):
    RawImportRow = apps.get_model('books', 'RawImportRow')
    batch, batch_size = [], 2000
    for row in RawImportRow.objects.filter(row_hash=''):
        row.row_hash = _canon_hash(row.data)
        batch.append(row)
        if len(batch) >= batch_size:
            RawImportRow.objects.bulk_update(batch, ['row_hash'])
            batch = []
    if batch:
        RawImportRow.objects.bulk_update(batch, ['row_hash'])


def revert_row_hash(apps, schema_editor):
    RawImportRow = apps.get_model('books', 'RawImportRow')
    RawImportRow.objects.filter(row_hash__gt='').update(row_hash='')


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0005_rawimportrow_row_hash_sourceupload_mode_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_row_hash, revert_row_hash),
    ]
