from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0024_physician_manager_authority"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="physicianchange",
            name="subject",
            field=models.CharField(
                choices=[
                    ("identity", "Identity"),
                    ("account_identity", "Account identity"),
                    ("role", "Role"),
                    ("faculty_availability_template", "Faculty Schedule template"),
                    ("monthly_availability_rule", "Monthly scheduling rule"),
                ],
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="physician",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="physician_identity",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
