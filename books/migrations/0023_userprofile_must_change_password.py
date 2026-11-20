from django.db import migrations, models


def mark_existing_users_force_change(apps, schema_editor):
    """存量用户（迁移前已存在）标记为必须改密，使其下次登录即被强制跳转改密页。"""
    UserProfile = apps.get_model('books', 'UserProfile')
    UserProfile.objects.update(must_change_password=True)


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0022_alter_masterrecord_tag'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='must_change_password',
            field=models.BooleanField(
                default=False,
                verbose_name='强制改密',
                help_text='为 True 时，已登录用户除改密/登出外被强制跳转改密页；首次改密成功后自动清除'),
        ),
        # 仅把迁移前已存在的存量用户标记为必须改密；新创建用户默认 False（建号时密码已校验）。
        migrations.RunPython(mark_existing_users_force_change, migrations.RunPython.noop),
    ]
