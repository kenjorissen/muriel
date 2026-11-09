from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0025_physician_user_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("action", models.CharField(choices=[("updated", "Updated")], max_length=16)),
                ("previous_value", models.JSONField(blank=True, null=True)),
                ("recorded_value", models.JSONField(blank=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=models.deletion.PROTECT,
                        related_name="muriel_account_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "changed_by",
                    models.ForeignKey(
                        on_delete=models.deletion.PROTECT,
                        related_name="recorded_account_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
    ]
