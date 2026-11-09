import hashlib
import json
import uuid
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PublicationStorageRemovalMigrationTests(TransactionTestCase):
    migrate_from = (
        "schedules",
        "0058_physician_faculty_precepting_capacity_override_and_more",
    )
    migrate_to = ("schedules", "0059_delete_publication_storage")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_upgrade_removes_only_publication_and_preserves_shared_authority(self):
        def create(name, **values):
            return self.old_apps.get_model("schedules", name).objects.create(**values)

        scheduler = get_user_model().objects.create_user(username="retained-scheduler")
        physician = create("Physician", display_name="Retained preceptor", user_id=scheduler.pk)
        create("Physician", display_name="Other retained physician")
        Authority = self.old_apps.get_model("schedules", "SchedulingMutationAuthority")
        authority, _ = Authority.objects.get_or_create(singleton=True)
        Authority.objects.filter(pk=authority.pk).update(id=73)
        authority = Authority.objects.get(pk=73)
        period = create("SchedulePeriod", month=date(2026, 9, 1))
        session = create(
            "PreceptorSession", period_id=period.pk, date=date(2026, 9, 7), period_of_day="AM"
        )
        assignment = create(
            "PreceptorAssignment",
            session_id=session.pk,
            physician_id=physician.pk,
            responsibility="primary",
            primary_position=1,
            is_locked=True,
        )
        create(
            "PreceptorAssignmentChange",
            session_id=session.pk,
            assignment_id=assignment.pk,
            action="assigned",
            recorded_value={"physician_id": physician.pk},
            reason="Retained assignment history",
            changed_by_id=scheduler.pk,
        )
        vacation = create(
            "Vacation",
            physician_id=physician.pk,
            start_date=date(2026, 9, 7),
            end_date=date(2026, 9, 8),
            note="Retained source fact",
        )
        create(
            "VacationChange",
            vacation_id=vacation.pk,
            vacation_id_at_recording=vacation.pk,
            physician_id=physician.pk,
            action="created",
            recorded_value={"note": vacation.note},
            changed_by_id=scheduler.pk,
        )
        create(
            "AccountChange",
            account_id=scheduler.pk,
            changed_by_id=scheduler.pk,
            action="updated",
            recorded_value={"username": scheduler.username},
        )
        from schedules.models import PreceptorSession
        from schedules.services.finding_acknowledgements import finding_identity
        from schedules.services.preceptors import draft_findings_for_session

        finding = next(
            finding
            for finding in draft_findings_for_session(
                PreceptorSession.objects.get(pk=session.pk)
            ).findings
            if finding.severity == "hard"
        )
        identity = finding_identity(finding)
        create(
            "FindingAcknowledgement",
            identity=identity.as_dict(),
            identity_key=identity.key,
            reason="Coverage confirmed externally",
            acknowledged_by_id=scheduler.pk,
        )
        proposal = create(
            "PreceptingSolverProposal",
            mode="fill",
            solve_start_date=date(2026, 9, 7),
            solve_end_date=date(2026, 9, 11),
            balance_start_date=date(2026, 9, 1),
            balance_end_date=date(2026, 9, 30),
            policy_version="retained-policy",
            input_fingerprint="b" * 64,
            input_snapshot={"physician_id": physician.pk},
            proposed_assignments={"precepting": []},
            before_findings=[],
            after_findings=[],
            workload_effects={},
            compromises=[],
            created_by_id=scheduler.pk,
            revision=4,
        )
        create(
            "PreceptingSolverProposalEdit",
            proposal_id=proposal.pk,
            assignment_type="precepting",
            date=date(2026, 9, 7),
            period_of_day="AM",
            position="primary-1",
            resulting_value={"physician_id": physician.pk},
            edited_by_id=scheduler.pk,
        )
        create(
            "PublishedWeekRevision",
            week_start=date(2026, 9, 7),
            week_end=date(2026, 9, 11),
            revision_number=2,
            content_digest="a" * 64,
            snapshot={"obsolete": True},
            published_by_id=scheduler.pk,
            publisher_name=scheduler.username,
        )
        publication_table = self.old_apps.get_model(
            "schedules", "PublishedWeekRevision"
        )._meta.db_table
        tables_before = set(connection.introspection.table_names())
        retained = {
            model._meta.model_name: list(model.objects.order_by("pk").values())
            for model in self.old_apps.get_app_config("schedules").get_models()
            if model._meta.model_name != "publishedweekrevision"
        }
        accounts_before = list(get_user_model().objects.order_by("pk").values())

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        apps = self.executor.loader.project_state([self.migrate_to]).apps

        self.assertEqual(
            set(connection.introspection.table_names()), tables_before - {publication_table}
        )
        with self.assertRaises(LookupError):
            apps.get_model("schedules", "PublishedWeekRevision")
        for name, values in retained.items():
            with self.subTest(model=name):
                self.assertEqual(
                    list(apps.get_model("schedules", name).objects.order_by("pk").values()), values
                )
        self.assertEqual(list(get_user_model().objects.order_by("pk").values()), accounts_before)

        # The live owner must reuse the row present before the upgrade, even when
        # it has a non-default ID. Retirement of acknowledgements still runs here.
        from schedules.models.mutation import SchedulingMutationAuthority
        from schedules.services._mutation import serialized_scheduling_state

        with serialized_scheduling_state():
            self.assertEqual(
                SchedulingMutationAuthority.objects.get(singleton=True).pk, authority.pk
            )
        self.assertEqual(
            list(SchedulingMutationAuthority.objects.values_list("pk", flat=True)), [73]
        )


class PhysicianVocabularyMigrationTests(TransactionTestCase):
    migrate_from = (("schedules", "0017_academic_event_definition_scheduling_defaults"),)
    migrate_to = (("schedules", "0018_physicianchange_remove_personchange_changed_by_and_more"),)

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_rewritten_published_snapshot_has_its_canonical_digest(self):
        legacy_snapshot = {
            "people": [{"person": {"person_id": 7, "display_name": "Dr. One"}}],
            "sessions": [{"assignments": [{"person": {"person_id": 7}}]}],
        }
        legacy_digest = hashlib.sha256(
            json.dumps(
                legacy_snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ).hexdigest()
        revision = self.old_apps.get_model("schedules", "PublishedWeekRevision").objects.create(
            week_start=date(2025, 2, 3),
            week_end=date(2025, 2, 7),
            revision_number=1,
            identifier=uuid.uuid4(),
            content_digest=legacy_digest,
            snapshot=legacy_snapshot,
            published_by_id=self.scheduler.pk,
            publisher_name=self.scheduler.username,
        )

        self.executor.loader.build_graph()
        self.executor.migrate(self.migrate_to)

        migrated_revision = (
            self.executor.loader.project_state(self.migrate_to)
            .apps.get_model("schedules", "PublishedWeekRevision")
            .objects.get(pk=revision.pk)
        )
        expected_snapshot = {
            "physicians": [{"physician": {"physician_id": 7, "display_name": "Dr. One"}}],
            "sessions": [{"assignments": [{"physician": {"physician_id": 7}}]}],
        }
        self.assertEqual(migrated_revision.snapshot, expected_snapshot)
        # This literal is the independently maintained SHA-256 example for the
        # migration's canonical UTF-8 JSON bytes; calling the publication helper
        # here would let a digest regression validate itself.
        self.assertEqual(
            migrated_revision.content_digest,
            "2946d17711394d46065f6cb142109083134762e2df806f3a3a0b1dbf6bf3f75d",
        )


class EffectiveRoleAttributesMigrationTests(TransactionTestCase):
    migrate_from = ("schedules", "0018_physicianchange_remove_personchange_changed_by_and_more")
    migrate_to = ("schedules", "0019_effectiverole_attributes_remove_qualification")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_retains_roles_history_and_scheduling_data_while_removing_qualifications(self):
        Physician = self.old_apps.get_model("schedules", "Physician")
        EffectiveRole = self.old_apps.get_model("schedules", "EffectiveRole")
        PhysicianChange = self.old_apps.get_model("schedules", "PhysicianChange")
        SchedulePeriod = self.old_apps.get_model("schedules", "SchedulePeriod")
        physician = Physician.objects.create(display_name="Retained physician")
        role = EffectiveRole.objects.create(
            physician_id=physician.pk,
            role="community_preceptor",
            effective_start=date(2025, 1, 1),
        )
        change = PhysicianChange.objects.create(
            physician_id=physician.pk,
            subject="role",
            subject_id=role.pk,
            recorded_value={"role": "community_preceptor"},
            changed_by_id=self.scheduler.pk,
        )
        period = SchedulePeriod.objects.create(month=date(2025, 1, 1))

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        apps = self.executor.loader.project_state([self.migrate_to]).apps
        migrated_role = apps.get_model("schedules", "EffectiveRole").objects.get(pk=role.pk)

        self.assertFalse(migrated_role.faculty_equivalent)
        self.assertEqual(migrated_role.faculty_leadership, "")
        self.assertTrue(
            apps.get_model("schedules", "PhysicianChange").objects.filter(pk=change.pk).exists()
        )
        self.assertTrue(
            apps.get_model("schedules", "SchedulePeriod").objects.filter(pk=period.pk).exists()
        )
        with self.assertRaises(LookupError):
            apps.get_model("schedules", "FacultyEquivalentQualification")


class PhysicianAccountIdentityMigrationTests(TransactionTestCase):
    migrate_from = ("schedules", "0024_physician_manager_authority")
    migrate_to = ("schedules", "0025_physician_user_identity")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_physicians_remain_unlinked_without_inferred_email_matches(self):
        Physician = self.old_apps.get_model("schedules", "Physician")
        existing_physician = Physician.objects.create(display_name="Existing physician")

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])

        migrated_physician = (
            self.executor.loader.project_state([self.migrate_to])
            .apps.get_model("schedules", "Physician")
            .objects.get(pk=existing_physician.pk)
        )
        self.assertIsNone(migrated_physician.user_id)


class SchedulingAdministratorAuthorityMigrationTests(TransactionTestCase):
    migrate_from = ("schedules", "0029_auth_user_case_insensitive_email")
    migrate_to = ("schedules", "0030_scheduling_administrator_authority")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_membership_invitation_and_audit_authority_are_renamed_without_loss(self):
        Group = self.old_apps.get_model("auth", "Group")
        Permission = self.old_apps.get_model("auth", "Permission")
        AccountChange = self.old_apps.get_model("schedules", "AccountChange")
        SchedulerInvitation = self.old_apps.get_model("schedules", "SchedulerInvitation")
        administrator = get_user_model().objects.create_superuser(
            username="administrator", password="test-password", email="administrator@example.com"
        )
        invited = get_user_model().objects.create_user(
            username="invited",
            password="test-password",
            email="invited@example.com",
            is_active=False,
        )
        content_type, _ = ContentType.objects.get_or_create(
            app_label="schedules", model="physician"
        )
        permission, _ = Permission.objects.get_or_create(
            content_type_id=content_type.pk,
            codename="manage_physicians",
            defaults={"name": "Can manage physicians"},
        )
        group, _ = Group.objects.get_or_create(name="Physician Managers")
        group.permissions.add(permission)
        invited.groups.add(group.pk)
        invitation = SchedulerInvitation.objects.create(
            account_id=invited.pk,
            created_by_id=administrator.pk,
            physician_manager=True,
            secret_digest="a" * 64,
            expires_at="2026-01-01T00:00:00Z",
        )
        change = AccountChange.objects.create(
            account_id=invited.pk,
            action="invited",
            previous_value={"physician_manager": False},
            recorded_value={"physician_manager": True},
            changed_by_id=administrator.pk,
        )

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        apps = self.executor.loader.project_state([self.migrate_to]).apps
        Group = apps.get_model("auth", "Group")
        Permission = apps.get_model("auth", "Permission")
        AccountChange = apps.get_model("schedules", "AccountChange")
        SchedulerInvitation = apps.get_model("schedules", "SchedulerInvitation")

        renamed_permission = Permission.objects.get(
            content_type__app_label="schedules", codename="manage_scheduling_administration"
        )
        renamed_group = Group.objects.get(name="Scheduling Administrators")
        migrated_invitation = SchedulerInvitation.objects.get(pk=invitation.pk)
        migrated_change = AccountChange.objects.get(pk=change.pk)
        invited.refresh_from_db()

        self.assertEqual(renamed_permission.pk, permission.pk)
        self.assertTrue(renamed_group.permissions.filter(pk=renamed_permission.pk).exists())
        self.assertTrue(invited.groups.filter(pk=renamed_group.pk).exists())
        self.assertFalse(Group.objects.filter(name="Physician Managers").exists())
        self.assertFalse(
            Permission.objects.filter(
                content_type__app_label="schedules", codename="manage_physicians"
            ).exists()
        )
        self.assertTrue(migrated_invitation.scheduling_administrator)
        self.assertEqual(migrated_change.previous_value, {"scheduling_administrator": False})
        self.assertEqual(migrated_change.recorded_value, {"scheduling_administrator": True})


class AdminSystemOptionMigrationTests(TransactionTestCase):
    migrate_from = ("schedules", "0051_alter_effectiverole_role")
    migrate_to = ("schedules", "0052_admin_system_option_and_float_role")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        self.old_apps.get_model("schedules", "CustomAvailabilityCategory").objects.all().delete()
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_adopts_the_existing_case_insensitive_admin_record_without_losing_relations(self):
        Category = self.old_apps.get_model("schedules", "CustomAvailabilityCategory")
        Assignment = self.old_apps.get_model("schedules", "CustomAvailabilityCategoryAssignment")
        Change = self.old_apps.get_model("schedules", "CustomAvailabilityCategoryChange")
        Physician = self.old_apps.get_model("schedules", "Physician")
        physician = Physician.objects.create(display_name="Assigned Faculty")
        category = Category.objects.create(name=" admin ", impact="available", is_active=False)
        assignment = Assignment.objects.create(category_id=category.pk, physician_id=physician.pk)
        change = Change.objects.create(
            category_id=category.pk,
            action="created",
            changed_by_id=self.scheduler.pk,
        )

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        apps = self.executor.loader.project_state([self.migrate_to]).apps
        migrated_category = apps.get_model("schedules", "CustomAvailabilityCategory").objects.get(
            pk=category.pk
        )

        self.assertEqual(migrated_category.name, "Admin")
        self.assertEqual(migrated_category.system_key, "admin")
        self.assertEqual(migrated_category.impact, "not_available")
        self.assertTrue(migrated_category.is_active)
        self.assertTrue(
            apps.get_model("schedules", "CustomAvailabilityCategoryAssignment")
            .objects.filter(pk=assignment.pk, category_id=category.pk)
            .exists()
        )
        self.assertTrue(
            apps.get_model("schedules", "CustomAvailabilityCategoryChange")
            .objects.filter(pk=change.pk, category_id=category.pk)
            .exists()
        )

    def test_creates_admin_when_no_existing_record_matches(self):
        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        Category = self.executor.loader.project_state([self.migrate_to]).apps.get_model(
            "schedules", "CustomAvailabilityCategory"
        )

        admin = Category.objects.get(system_key="admin")
        self.assertEqual(admin.name, "Admin")
        self.assertEqual(admin.impact, "not_available")
        self.assertTrue(admin.is_active)


class FacultyRoleVocabularyMigrationTests(TransactionTestCase):
    migrate_from = ("schedules", "0030_scheduling_administrator_authority")
    migrate_to = ("schedules", "0031_faculty_role_vocabulary")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_roles_and_retained_role_history_are_renamed_without_losing_context(self):
        Physician = self.old_apps.get_model("schedules", "Physician")
        EffectiveRole = self.old_apps.get_model("schedules", "EffectiveRole")
        PhysicianChange = self.old_apps.get_model("schedules", "PhysicianChange")
        PublishedWeekRevision = self.old_apps.get_model("schedules", "PublishedWeekRevision")
        physician = Physician.objects.create(display_name="Faculty physician")
        role = EffectiveRole.objects.create(
            physician_id=physician.pk,
            role="faculty_preceptor",
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 12, 31),
            faculty_leadership="program_director",
        )
        change = PhysicianChange.objects.create(
            physician_id=physician.pk,
            subject="role",
            subject_id=role.pk,
            previous_value={"role": "faculty_preceptor", "retained": ["unchanged"]},
            recorded_value={
                "role": "faculty_preceptor",
                "effective_start": "2025-01-01",
                "effective_end": "2025-12-31",
                "faculty_leadership": "program_director",
                "nested": {"role": "faculty_preceptor"},
            },
            reason="Retain the role context.",
            changed_by_id=self.scheduler.pk,
        )
        changed_at = change.changed_at
        legacy_snapshot = {
            "effective_category": "faculty_preceptor",
            "findings": [
                {
                    "physicians": [
                        {
                            "effective_category": "faculty_preceptor",
                            "effective_role_facts": [
                                {
                                    "role": "faculty_preceptor",
                                    "faculty_leadership": "program_director",
                                }
                            ],
                        }
                    ]
                }
            ],
        }
        revision = PublishedWeekRevision.objects.create(
            week_start=date(2025, 2, 3),
            week_end=date(2025, 2, 7),
            revision_number=1,
            identifier=uuid.uuid4(),
            content_digest="0" * 64,
            snapshot=legacy_snapshot,
            published_by_id=self.scheduler.pk,
            publisher_name=self.scheduler.username,
        )

        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        apps = self.executor.loader.project_state([self.migrate_to]).apps
        migrated_role = apps.get_model("schedules", "EffectiveRole").objects.get(pk=role.pk)
        migrated_change = apps.get_model("schedules", "PhysicianChange").objects.get(pk=change.pk)
        migrated_revision = apps.get_model("schedules", "PublishedWeekRevision").objects.get(
            pk=revision.pk
        )

        self.assertEqual(migrated_role.physician_id, physician.pk)
        self.assertEqual(migrated_role.role, "faculty")
        self.assertEqual(migrated_role.effective_start, date(2025, 1, 1))
        self.assertEqual(migrated_role.effective_end, date(2025, 12, 31))
        self.assertEqual(migrated_role.faculty_leadership, "program_director")
        self.assertEqual(
            migrated_change.previous_value,
            {"role": "faculty", "retained": ["unchanged"]},
        )
        self.assertEqual(migrated_change.recorded_value["role"], "faculty")
        self.assertEqual(migrated_change.recorded_value["nested"]["role"], "faculty")
        self.assertEqual(migrated_change.recorded_value["faculty_leadership"], "program_director")
        self.assertEqual(migrated_change.changed_by_id, self.scheduler.pk)
        self.assertEqual(migrated_change.changed_at, changed_at)
        self.assertEqual(migrated_change.reason, "Retain the role context.")
        self.assertEqual(migrated_revision.snapshot["effective_category"], "faculty")
        self.assertEqual(
            migrated_revision.snapshot["findings"][0]["physicians"][0]["effective_category"],
            "faculty",
        )
        self.assertEqual(
            migrated_revision.snapshot["findings"][0]["physicians"][0]["effective_role_facts"],
            [{"role": "faculty", "faculty_leadership": "program_director"}],
        )
        self.assertEqual(
            migrated_revision.content_digest,
            "107722df17ab3482ddd90d3f7b3d906e15ae3563e227776a140f5ea8dacd8dec",
        )
