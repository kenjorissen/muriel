from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("schedules", "0027_schedulerinvitation")]

    operations = [
        migrations.AlterField(
            model_name="accountchange",
            name="action",
            field=models.CharField(
                choices=[
                    ("updated", "Updated"),
                    ("invited", "Invited"),
                    ("resent", "Invitation resent"),
                    ("revoked", "Invitation revoked"),
                    ("accepted", "Invitation accepted"),
                    ("delivered", "Invitation delivered"),
                    ("delivery_failed", "Invitation delivery failed"),
                ],
                max_length=16,
            ),
        ),
    ]
