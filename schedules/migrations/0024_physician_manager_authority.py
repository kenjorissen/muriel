from django.db import migrations


def create_physician_manager_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    content_type, _ = ContentType.objects.get_or_create(app_label="schedules", model="physician")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="manage_physicians",
        defaults={"name": "Can manage physicians"},
    )
    group, _ = Group.objects.get_or_create(name="Physician Managers")
    group.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [("schedules", "0023_alter_physicianchange_subject")]

    operations = [
        migrations.AlterModelOptions(
            name="physician",
            options={
                "ordering": ("display_name", "pk"),
                "permissions": (("manage_physicians", "Can manage physicians"),),
            },
        ),
        migrations.RunPython(create_physician_manager_group, migrations.RunPython.noop),
    ]
