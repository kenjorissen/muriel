import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("schedules", "0045_academicevent_precepting_impact_and_more")]

    operations = [
        migrations.AddField(
            model_name="academiceventdefinition",
            name="all_available_faculty",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="academiceventdefinition",
            name="scheduling_mode",
            field=models.CharField(
                choices=[("yearly", "Yearly"), ("monthly_rules", "Monthly rules")],
                default="yearly",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="academiceventdefinition",
            name="expected_quarter",
            field=models.CharField(
                blank=True,
                choices=[("Q1", "Q1"), ("Q2", "Q2"), ("Q3", "Q3"), ("Q4", "Q4")],
                default="Q1",
                max_length=2,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="AcademicEventMonthlyRule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "weekday",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (0, "Monday"),
                            (1, "Tuesday"),
                            (2, "Wednesday"),
                            (3, "Thursday"),
                            (4, "Friday"),
                            (5, "Saturday"),
                            (6, "Sunday"),
                        ]
                    ),
                ),
                ("ordinals", models.JSONField()),
                ("effective_start", models.DateField(blank=True, null=True)),
                ("effective_end", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="monthly_rules",
                        to="schedules.academiceventdefinition",
                    ),
                ),
            ],
            options={"ordering": ("definition__base_name", "weekday", "effective_start", "pk")},
        ),
        migrations.CreateModel(
            name="AcademicEventMonthlyRuleChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source_id_at_recording", models.PositiveBigIntegerField()),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("corrected", "Corrected"),
                            ("removed", "Removed"),
                        ],
                        max_length=10,
                    ),
                ),
                ("previous_value", models.JSONField(blank=True, null=True)),
                ("recorded_value", models.JSONField(blank=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="monthly_rule_changes",
                        to="schedules.academiceventdefinition",
                    ),
                ),
                (
                    "rule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="changes",
                        to="schedules.academiceventmonthlyrule",
                    ),
                ),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
    ]
