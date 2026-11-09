from django.db import migrations, models


def create_mutation_authority(apps, schema_editor):
    authority = apps.get_model("schedules", "SchedulingMutationAuthority")
    authority.objects.get_or_create(singleton=True)


class Migration(migrations.Migration):
    dependencies = [("schedules", "0010_published_week_revision")]

    operations = [
        migrations.CreateModel(
            name="SchedulingMutationAuthority",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
            ],
        ),
        migrations.RunPython(create_mutation_authority, migrations.RunPython.noop),
    ]
