from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0023_userprofile_must_change_password'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReconcileLetter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('unit_name', models.CharField(max_length=255, verbose_name='客户名称')),
                ('year', models.IntegerField(verbose_name='年份')),
                ('month_start', models.IntegerField(verbose_name='起始月')),
                ('month_end', models.IntegerField(verbose_name='截止月')),
                ('seq', models.IntegerField(verbose_name='年度流水号')),
                ('issued_by', models.CharField(blank=True, default='', max_length=150, verbose_name='出具人')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='出具时间')),
            ],
            options={
                'verbose_name': '对账函编号',
                'verbose_name_plural': '对账函编号',
                'unique_together': {('unit_name', 'year', 'month_start', 'month_end'), ('year', 'seq')},
            },
        ),
    ]
