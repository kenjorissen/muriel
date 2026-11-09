from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0049_academiceventchange_changed_by_nullable"),
    ]

    operations = [
        migrations.AddField(
            model_name="academicevent",
            name="assignment_is_default",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="academiceventdefinition",
            name="default_participants",
            field=models.ManyToManyField(
                blank=True,
                related_name="academic_event_default_definitions",
                to="schedules.physician",
            ),
        ),
    ]
