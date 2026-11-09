from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0046_academic_event_monthly_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="academicevent",
            name="note",
            field=models.TextField(blank=True, default=""),
        ),
    ]
