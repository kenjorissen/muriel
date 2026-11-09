from django.db import migrations


def rename_scheduling_administrator_authority(apps, schema_editor):
    AccountChange = apps.get_model("schedules", "AccountChange")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    content_type = ContentType.objects.only("pk").get(app_label="schedules", model="physician")
    permission = Permission.objects.filter(
        content_type=content_type, codename="manage_physicians"
    ).first()
    renamed_permission = Permission.objects.filter(
        content_type=content_type, codename="manage_scheduling_administration"
    ).first()
    if permission and renamed_permission:
        for group in permission.group_set.all():
            group.permissions.add(renamed_permission)
        permission.delete()
    elif permission:
        permission.codename = "manage_scheduling_administration"
        permission.name = "Can manage scheduling administration"
        permission.save(update_fields=("codename", "name"))
        renamed_permission = permission
    elif not renamed_permission:
        renamed_permission = Permission.objects.create(
            content_type=content_type,
            codename="manage_scheduling_administration",
            name="Can manage scheduling administration",
        )

    group = Group.objects.filter(name="Physician Managers").first()
    renamed_group, _ = Group.objects.get_or_create(name="Scheduling Administrators")
    if group:
        renamed_group.permissions.add(*group.permissions.all())
        renamed_group.user_set.add(*group.user_set.all())
        group.delete()
    renamed_group.permissions.add(renamed_permission)

    for change in AccountChange.objects.iterator():
        updated_fields = []
        for field in ("previous_value", "recorded_value"):
            value = getattr(change, field)
            if isinstance(value, dict) and "physician_manager" in value:
                value = {
                    ("scheduling_administrator" if key == "physician_manager" else key): item
                    for key, item in value.items()
                }
                setattr(change, field, value)
                updated_fields.append(field)
        if updated_fields:
            change.save(update_fields=updated_fields)


def restore_physician_manager_authority(apps, schema_editor):
    AccountChange = apps.get_model("schedules", "AccountChange")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    content_type = (
        ContentType.objects.only("pk").filter(app_label="schedules", model="physician").first()
    )
    if content_type:
        permission = Permission.objects.filter(
            content_type=content_type, codename="manage_scheduling_administration"
        ).first()
        if permission:
            permission.codename = "manage_physicians"
            permission.name = "Can manage physicians"
            permission.save(update_fields=("codename", "name"))

    group = Group.objects.filter(name="Scheduling Administrators").first()
    if group:
        group.name = "Physician Managers"
        group.save(update_fields=("name",))

    for change in AccountChange.objects.iterator():
        updated_fields = []
        for field in ("previous_value", "recorded_value"):
            value = getattr(change, field)
            if isinstance(value, dict) and "scheduling_administrator" in value:
                value = {
                    ("physician_manager" if key == "scheduling_administrator" else key): item
                    for key, item in value.items()
                }
                setattr(change, field, value)
                updated_fields.append(field)
        if updated_fields:
            change.save(update_fields=updated_fields)


class Migration(migrations.Migration):
    dependencies = [("schedules", "0029_auth_user_case_insensitive_email")]

    operations = [
        migrations.AlterModelOptions(
            name="physician",
            options={
                "ordering": ("display_name", "pk"),
                "permissions": (
                    ("manage_scheduling_administration", "Can manage scheduling administration"),
                ),
            },
        ),
        migrations.RenameField(
            model_name="schedulerinvitation",
            old_name="physician_manager",
            new_name="scheduling_administrator",
        ),
        migrations.RunPython(
            rename_scheduling_administrator_authority, restore_physician_manager_authority
        ),
    ]
