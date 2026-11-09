from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0050_academicevent_assignment_is_default_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="effectiverole",
            name="role",
            field=models.CharField(
                choices=[
                    ("faculty", "Faculty"),
                    ("community_rounder", "Community Rounder"),
                    ("community_preceptor", "Community Preceptor"),
                    ("casual_rounder", "Casual Rounder"),
                ],
                max_length=24,
            ),
        ),
    ]
