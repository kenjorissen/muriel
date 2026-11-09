from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("schedules", "0052_admin_system_option_and_float_role")]

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
                    ("float_preceptor", "Float Preceptor"),
                    ("external_patient_care", "External Patient Care"),
                ],
                max_length=24,
            ),
        ),
    ]
