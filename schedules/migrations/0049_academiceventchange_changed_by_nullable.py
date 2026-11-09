from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0048_monthly_academic_event_occurrences"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="academiceventchange",
            name="changed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="recorded_academic_event_changes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
