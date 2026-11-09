import hashlib
import json

from django.db import migrations, models

OLD_ROLE = "faculty_preceptor"
NEW_ROLE = "faculty"


def _replace_role(value, old_role, new_role):
    if value == old_role:
        return new_role
    if isinstance(value, list):
        return [_replace_role(item, old_role, new_role) for item in value]
    if isinstance(value, dict):
        return {key: _replace_role(item, old_role, new_role) for key, item in value.items()}
    return value


def _snapshot_digest(snapshot):
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _replace_published_snapshot_roles(PublishedWeekRevision, old_role, new_role):
    for revision in PublishedWeekRevision.objects.iterator():
        snapshot = revision.snapshot
        replacement = _replace_role(snapshot, old_role, new_role)
        if replacement != snapshot:
            PublishedWeekRevision.objects.filter(pk=revision.pk).update(
                snapshot=replacement,
                content_digest=_snapshot_digest(replacement),
            )


def rename_faculty_role(apps, schema_editor):
    EffectiveRole = apps.get_model("schedules", "EffectiveRole")
    PhysicianChange = apps.get_model("schedules", "PhysicianChange")
    PublishedWeekRevision = apps.get_model("schedules", "PublishedWeekRevision")

    EffectiveRole.objects.filter(role=OLD_ROLE).update(role=NEW_ROLE)
    for change in PhysicianChange.objects.filter(subject="role").iterator():
        updated_fields = []
        for field in ("previous_value", "recorded_value"):
            value = getattr(change, field)
            replacement = _replace_role(value, OLD_ROLE, NEW_ROLE)
            if replacement != value:
                setattr(change, field, replacement)
                updated_fields.append(field)
        if updated_fields:
            change.save(update_fields=updated_fields)
    _replace_published_snapshot_roles(PublishedWeekRevision, OLD_ROLE, NEW_ROLE)


def restore_faculty_preceptor_role(apps, schema_editor):
    EffectiveRole = apps.get_model("schedules", "EffectiveRole")
    PhysicianChange = apps.get_model("schedules", "PhysicianChange")
    PublishedWeekRevision = apps.get_model("schedules", "PublishedWeekRevision")

    EffectiveRole.objects.filter(role=NEW_ROLE).update(role=OLD_ROLE)
    for change in PhysicianChange.objects.filter(subject="role").iterator():
        updated_fields = []
        for field in ("previous_value", "recorded_value"):
            value = getattr(change, field)
            replacement = _replace_role(value, NEW_ROLE, OLD_ROLE)
            if replacement != value:
                setattr(change, field, replacement)
                updated_fields.append(field)
        if updated_fields:
            change.save(update_fields=updated_fields)
    _replace_published_snapshot_roles(PublishedWeekRevision, NEW_ROLE, OLD_ROLE)


class Migration(migrations.Migration):
    dependencies = [("schedules", "0030_scheduling_administrator_authority")]

    operations = [
        migrations.AlterField(
            model_name="effectiverole",
            name="role",
            field=models.CharField(
                choices=[
                    ("faculty", "Faculty"),
                    ("community_preceptor", "Community preceptor"),
                    ("casual_rounder", "Casual rounder"),
                ],
                max_length=24,
            ),
        ),
        migrations.RunPython(rename_faculty_role, restore_faculty_preceptor_role),
    ]
