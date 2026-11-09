from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0014_academic_event_definition"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="AcademicEventChange"),
        migrations.DeleteModel(name="AcademicEvent"),
        migrations.AddField(
            model_name="academiceventdefinition",
            name="default_required_participant_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.CreateModel(
            name="AcademicEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("academic_year", models.PositiveIntegerField()),
                ("base_name", models.CharField(max_length=120)),
                ("suffix", models.CharField(blank=True, default="", max_length=120)),
                ("required_participant_count", models.PositiveIntegerField()),
                (
                    "resolution",
                    models.CharField(
                        choices=[
                            ("dated", "Dated occurrence"),
                            ("not_occurring", "Not occurring this year"),
                        ],
                        max_length=16,
                    ),
                ),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                (
                    "period_of_day",
                    models.CharField(
                        choices=[
                            ("whole_day", "Whole day"),
                            ("AM", "AM only"),
                            ("PM", "PM only"),
                        ],
                        default="whole_day",
                        max_length=9,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=models.deletion.PROTECT,
                        related_name="annual_events",
                        to="schedules.academiceventdefinition",
                    ),
                ),
                (
                    "participants",
                    models.ManyToManyField(
                        blank=True, related_name="academic_events", to="schedules.person"
                    ),
                ),
            ],
            options={"ordering": ("academic_year", "start_date", "base_name", "pk")},
        ),
        migrations.AddConstraint(
            model_name="academicevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True),
                fields=("definition", "academic_year"),
                name="one_live_academic_event_resolution_per_year",
            ),
        ),
        migrations.CreateModel(
            name="AcademicEventChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("event_id_at_recording", models.PositiveBigIntegerField()),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("corrected", "Corrected"),
                            ("removed", "Removed"),
                            ("not_occurring", "Not occurring this year"),
                            ("reversed", "Not-occurring decision reversed"),
                        ],
                        max_length=16,
                    ),
                ),
                ("previous_value", models.JSONField(blank=True, null=True)),
                ("recorded_value", models.JSONField(blank=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        on_delete=models.deletion.PROTECT,
                        related_name="recorded_academic_event_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="changes",
                        to="schedules.academicevent",
                    ),
                ),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
    ]
