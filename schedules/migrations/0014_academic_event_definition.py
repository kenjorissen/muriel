from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0013_vacation_current_note"),
    ]

    operations = [
        migrations.CreateModel(
            name="AcademicEventDefinition",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("base_name", models.CharField(max_length=120)),
                ("start_academic_year", models.PositiveIntegerField()),
                ("end_academic_year", models.PositiveIntegerField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("base_name", "pk")},
        ),
        migrations.CreateModel(
            name="AcademicEventDefinitionChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("definition_id_at_recording", models.PositiveBigIntegerField()),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("corrected", "Corrected"),
                            ("deactivated", "Deactivated"),
                        ],
                        max_length=12,
                    ),
                ),
                ("previous_value", models.JSONField(blank=True, null=True)),
                ("recorded_value", models.JSONField(blank=True, null=True)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        on_delete=models.deletion.PROTECT,
                        related_name="recorded_academic_event_definition_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "definition",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="changes",
                        to="schedules.academiceventdefinition",
                    ),
                ),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
    ]
