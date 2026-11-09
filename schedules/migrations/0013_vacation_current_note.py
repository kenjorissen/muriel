from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0012_alter_vacationchange_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacation",
            name="note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="vacationchange",
            name="previous_note",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vacationchange",
            name="recorded_note",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RemoveField(
            model_name="vacationchange",
            name="reason",
        ),
    ]
