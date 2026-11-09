import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0047_academicevent_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="academicevent",
            name="monthly_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="occurrences",
                to="schedules.academiceventmonthlyrule",
            ),
        ),
        migrations.AddField(
            model_name="academicevent",
            name="monthly_rule_source_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="academicevent",
            name="expected_quarter",
            field=models.CharField(
                blank=True,
                choices=[("Q1", "Q1"), ("Q2", "Q2"), ("Q3", "Q3"), ("Q4", "Q4")],
                default="Q1",
                max_length=2,
                null=True,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="academicevent",
            name="one_live_academic_event_resolution_per_year",
        ),
        migrations.AddConstraint(
            model_name="academicevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_active", True),
                    ("monthly_rule__isnull", True),
                    ("monthly_rule_source_id__isnull", True),
                ),
                fields=("definition", "academic_year"),
                name="one_live_academic_event_resolution_per_year",
            ),
        ),
        migrations.AddConstraint(
            model_name="academicevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True), ("monthly_rule__isnull", False)),
                fields=("monthly_rule", "start_date"),
                name="one_live_monthly_academic_event_occurrence_per_rule_date",
            ),
        ),
    ]
