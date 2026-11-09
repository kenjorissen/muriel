from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("schedules", "0028_alter_accountchange_action")]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX auth_user_email_case_insensitive_unique "
                "ON auth_user (LOWER(email)) WHERE email <> '';"
            ),
            reverse_sql="DROP INDEX auth_user_email_case_insensitive_unique;",
        ),
    ]
