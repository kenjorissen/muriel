from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0011_scheduling_mutation_authority"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vacationchange",
            name="action",
            field=models.CharField(
                choices=[("created", "Created"), ("corrected", "Edited"), ("removed", "Removed")],
                max_length=10,
            ),
        ),
    ]
