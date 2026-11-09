from django.db import migrations, models
from django.db.models.functions import Lower, Trim


def create_or_adopt_admin_option(apps, schema_editor):
    Category = apps.get_model("schedules", "CustomAvailabilityCategory")
    candidates = list(
        Category.objects.annotate(normalized_name=Lower(Trim("name"))).filter(
            normalized_name="admin"
        )
    )
    if len(candidates) == 1:
        category = candidates[0]
        category.name = "Admin"
        category.impact = "not_available"
        category.is_active = True
        category.system_key = "admin"
        category.save(update_fields=("name", "impact", "is_active", "system_key"))
    elif not candidates:
        Category.objects.create(
            name="Admin", impact="not_available", is_active=True, system_key="admin"
        )


class Migration(migrations.Migration):
    dependencies = [("schedules", "0051_alter_effectiverole_role")]

    operations = [
        migrations.AddField(
            model_name="customavailabilitycategory",
            name="system_key",
            field=models.CharField(
                blank=True, choices=[("admin", "Admin")], max_length=24, null=True
            ),
        ),
        migrations.AlterField(
            model_name="effectiverole",
            name="role",
            field=models.CharField(
                choices=[
                    ("faculty", "Faculty"),
                    ("community_rounder", "Community Rounder"),
                    ("community_preceptor", "Community Preceptor"),
                    ("casual_rounder", "Casual Rounder"),
                    ("float_preceptor", "Float Preceptor"),
                ],
                max_length=24,
            ),
        ),
        migrations.RunPython(create_or_adopt_admin_option, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="customavailabilitycategory",
            constraint=models.UniqueConstraint(
                condition=models.Q(system_key__isnull=False),
                fields=("system_key",),
                name="custom_availability_category_system_key_unique",
            ),
        ),
    ]
