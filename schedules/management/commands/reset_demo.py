"""Destructively rebuild the local demonstration database."""

import subprocess
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connections

from schedules.models import (
    AcademicEvent,
    CommunityPreceptorRule,
    CustomAvailabilityCategory,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    PreceptorSession,
    Vacation,
)
from schedules.models.accounts import SchedulerInvitation
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    initialize_monthly_academic_event_occurrences,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.availability_categories import (
    create_custom_availability_category,
    ensure_system_schedule_options,
    set_custom_availability_category_physicians,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.holidays import holiday_closure_for
from schedules.services.interviews import (
    assign_interview_shift,
    create_interview_shift,
    interview_assignment_picker_candidates_on,
)
from schedules.services.lead_times import ensure_lead_time_defaults
from schedules.services.physicians import (
    add_role,
    correct_faculty_availability_template,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
)
from schedules.services.preceptors import assign_preceptor, open_monthly_draft
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.vacations import create_vacation

# TEXT-CONTRACT: demo-scheduler-credentials. README.md's documented local
# demonstration login depends on the exact username/password spelling and punctuation;
# schedules.tests.test_demo_reset.DemoSeedTests protects it with independent literals.
DEMO_USERNAME = "demo-scheduler"
DEMO_PASSWORD = "demo-password"
DEMO_SCHEDULING_ADMINISTRATOR_USERNAME = "demo-scheduling-administrator"
DEMO_HORIZON_START = date(2026, 8, 1)
DEMO_HORIZON_END = date(2027, 7, 31)
DEMO_REASON = "Seeded local demonstration data"
NO_TEMPLATE_PHYSICIAN = "Taylor Adams"
EXTERNAL_PATIENT_CARE_PHYSICIAN = "Devon Park"
DEMO_FACULTY_PHYSICIANS = (
    "Alex Kim",
    "Avery Patel",
    "Casey Nguyen",
    "Devon Park",
    "Jordan Lee",
    "Morgan Reed",
    "Quinn Baker",
    "Riley Chen",
    "Taylor Adams",
    "Wren Foster",
)
DEMO_COMMUNITY_PRECEPTORS = ("Fictional Cedar", "Fictional Harbor", "Fictional Summit")
DEMO_PHYSICIANS = DEMO_FACULTY_PHYSICIANS + DEMO_COMMUNITY_PRECEPTORS
DEMO_FACULTY_LEADERSHIP = {
    "Alex Kim": EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
    "Avery Patel": EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
    "Casey Nguyen": EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
}
DEMO_ACADEMIC_EVENT_DEFINITIONS = (
    # TEXT-CONTRACT: demo-department-meeting-name. The demo catalogue requires the
    # exact Department Meeting label, capitalization, and spacing for its meeting example.
    # Protected by test_academic_event_catalog_has_deterministic_quarter_defaults_and_unscheduled_choices
    # in schedules/tests/test_demo_reset.py (DemoSeedTests).
    ("Department Meeting", "Q1", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Foundations of Teaching Course", "Q1", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Clinical Reasoning Conference", "Q1", 3, AcademicEvent.PeriodOfDay.AM, 1),
    ("Scholarship Methods Course", "Q1", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Simulation Design Conference", "Q1", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Learning Analytics Course", "Q1", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Interprofessional Practice Conference", "Q1", 1, AcademicEvent.PeriodOfDay.PM, 1),
    ("Feedback Skills Course", "Q1", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Ambulatory Care Conference", "Q1", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Evidence Synthesis Course", "Q1", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Clinical Teaching Intensive Course", "Q2", 3, AcademicEvent.PeriodOfDay.AM, 2),
    ("Assessment Calibration Conference", "Q2", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Health Systems Course", "Q2", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Educational Leadership Conference", "Q2", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Quality Improvement Course", "Q2", 1, AcademicEvent.PeriodOfDay.PM, 1),
    ("Rural Health Conference", "Q2", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Bedside Teaching Course", "Q2", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Mentorship Practice Conference", "Q2", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Program Evaluation Course", "Q2", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Inclusive Learning Conference", "Q2", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Collaborative Assessment Conference", "Q3", 1, AcademicEvent.PeriodOfDay.PM, 1),
    ("Diagnostic Reasoning Course", "Q3", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Faculty Development Conference", "Q3", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Population Health Course", "Q3", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Digital Health Conference", "Q3", 2, AcademicEvent.PeriodOfDay.AM, 1),
    ("Team-Based Learning Course", "Q3", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Community Partnership Conference", "Q3", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Professional Identity Course", "Q3", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Research Translation Conference", "Q3", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Advanced Precepting Course", "Q3", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Curriculum Design Conference", "Q4", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
    ("Capstone Teaching Course", "Q4", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Clinical Innovation Conference", "Q4", 1, AcademicEvent.PeriodOfDay.AM, 1),
    ("Narrative Medicine Course", "Q4", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Global Health Conference", "Q4", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Patient Safety Course", "Q4", 1, AcademicEvent.PeriodOfDay.PM, 1),
    ("Transitions of Care Conference", "Q4", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Leadership Foundations Course", "Q4", 3, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Medical Education Research Conference", "Q4", 1, AcademicEvent.PeriodOfDay.WHOLE_DAY, 1),
    ("Reflective Practice Course", "Q4", 2, AcademicEvent.PeriodOfDay.WHOLE_DAY, 2),
)


def _canonical_main_worktree(base_dir):
    result = subprocess.run(
        ["git", "-C", str(base_dir), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise CommandError("Unable to identify the canonical main worktree.")

    worktree = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
        elif line == "branch refs/heads/main" and worktree:
            return worktree
    raise CommandError("Unable to identify the canonical main worktree.")


def _reset_database_path(*, debug=None, database=None, base_dir=None):
    if debug is None:
        debug = settings.DEBUG
    if not debug:
        raise CommandError("Demo reset is available only while Django is in development mode.")

    if database is None:
        database = settings.DATABASES["default"]
    if database["ENGINE"] != "django.db.backends.sqlite3":
        raise CommandError("Demo reset only accepts the configured SQLite development database.")

    base_dir = Path(base_dir or settings.BASE_DIR).resolve()
    expected_path = (base_dir / "db.sqlite3").resolve()
    configured_path = Path(database["NAME"]).resolve()
    if configured_path != expected_path:
        raise CommandError(
            "Demo reset only accepts the expected db.sqlite3 file in the main worktree."
        )
    if _canonical_main_worktree(base_dir) != base_dir:
        raise CommandError("Demo reset must be run from the canonical main worktree.")
    return configured_path


def _slots(*, available=(), backup_only=(), patient_care=()):
    values = {
        slot: FacultyAvailabilityTemplate.AvailabilityState.OFF
        for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
    }
    values.update({slot: FacultyAvailabilityTemplate.AvailabilityState.OPEN for slot in available})
    values.update(
        {slot: FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY for slot in backup_only}
    )
    values.update(
        {slot: FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE for slot in patient_care}
    )
    return values


def _scheduling_administrators_group():
    content_type, _ = ContentType.objects.get_or_create(app_label="schedules", model="physician")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="manage_scheduling_administration",
        defaults={"name": "Can manage scheduling administration"},
    )
    group, _ = Group.objects.get_or_create(name="Scheduling Administrators")
    group.permissions.add(permission)
    return group


def _permission_identifier(permission):
    return (permission.content_type.app_label, permission.content_type.model, permission.codename)


def _capture_login_accounts():
    """Capture only authentication state; all Muriel domain links are intentionally omitted."""
    user_model = get_user_model()
    users = user_model.objects.prefetch_related(
        "groups__permissions__content_type",
        "user_permissions__content_type",
        "scheduler_invitations",
    ).order_by(user_model.USERNAME_FIELD)
    snapshot = []
    for user in users:
        invitations = list(user.scheduler_invitations.all())
        if invitations and not any(
            invitation.status == SchedulerInvitation.Status.ACCEPTED for invitation in invitations
        ):
            continue
        groups = []
        for group in user.groups.all():
            groups.append(
                (
                    group.name,
                    tuple(
                        sorted(
                            _permission_identifier(permission)
                            for permission in group.permissions.all()
                        )
                    ),
                )
            )
        snapshot.append(
            {
                "username": getattr(user, user_model.USERNAME_FIELD),
                "password": user.password,
                "email": user.email,
                "is_active": user.is_active,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                "groups": tuple(sorted(groups)),
                "permissions": tuple(
                    sorted(
                        _permission_identifier(permission)
                        for permission in user.user_permissions.all()
                    )
                ),
            }
        )
    return tuple(snapshot)


def _restore_permission(identifier):
    app_label, model, codename = identifier
    try:
        return Permission.objects.get(
            content_type__app_label=app_label,
            content_type__model=model,
            codename=codename,
        )
    except Permission.DoesNotExist as error:
        raise CommandError(
            "Cannot restore preserved authority: permission "
            f"{app_label}.{model}.{codename} is unavailable after migration."
        ) from error


def _restore_login_accounts(snapshot):
    """Restore the in-memory snapshot after migration, failing rather than weakening authority."""
    permissions = {
        identifier: _restore_permission(identifier)
        for user in snapshot
        for _, group_permissions in user["groups"]
        for identifier in group_permissions
    }
    permissions.update(
        {
            identifier: _restore_permission(identifier)
            for user in snapshot
            for identifier in user["permissions"]
        }
    )
    groups = {}
    for user in snapshot:
        for name, group_permissions in user["groups"]:
            if name not in groups:
                group, _ = Group.objects.get_or_create(name=name)
                group.permissions.set([permissions[identifier] for identifier in group_permissions])
                groups[name] = group

    user_model = get_user_model()
    for user_data in snapshot:
        user = user_model(
            **{
                user_model.USERNAME_FIELD: user_data["username"],
                "password": user_data["password"],
                "email": user_data["email"],
                "is_active": user_data["is_active"],
                "is_staff": user_data["is_staff"],
                "is_superuser": user_data["is_superuser"],
            }
        )
        user.save()
        user.groups.set([groups[name] for name, _ in user_data["groups"]])
        user.user_permissions.set(
            [permissions[identifier] for identifier in user_data["permissions"]]
        )


def _seed_physicians(scheduler):
    physicians = {}
    patterns = (
        _slots(
            available=("monday_am", "monday_pm", "wednesday_am", "friday_pm"),
            backup_only=("friday_am",),
            patient_care=("thursday_pm",),
        ),
        _slots(
            available=("tuesday_am", "tuesday_pm", "thursday_am", "thursday_pm"),
            backup_only=("monday_am",),
        ),
        _slots(
            available=("monday_am", "wednesday_pm", "friday_am"),
            backup_only=("tuesday_pm", "thursday_am"),
        ),
        _slots(
            available=("monday_pm", "tuesday_am", "wednesday_am", "thursday_pm"),
            backup_only=("friday_pm",),
        ),
        _slots(
            available=("monday_am", "tuesday_pm", "wednesday_pm", "friday_am"),
            backup_only=("thursday_pm",),
        ),
        _slots(
            available=("monday_pm", "tuesday_am", "thursday_am", "friday_pm"),
            backup_only=("wednesday_am",),
        ),
        _slots(
            available=("monday_am", "wednesday_am", "thursday_pm", "friday_am"),
            backup_only=("tuesday_am",),
        ),
        _slots(
            available=("tuesday_pm", "wednesday_pm", "thursday_am", "friday_pm"),
            backup_only=("monday_pm",),
        ),
        _slots(
            available=("monday_am", "tuesday_am", "wednesday_pm", "friday_am"),
            backup_only=("thursday_am",),
        ),
    )
    for index, name in enumerate(DEMO_FACULTY_PHYSICIANS):
        physician = create_physician(display_name=name, scheduler=scheduler, reason=DEMO_REASON)
        physicians[name] = physician
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=DEMO_HORIZON_START,
            effective_end=DEMO_HORIZON_END,
            faculty_leadership=DEMO_FACULTY_LEADERSHIP.get(name, ""),
            scheduler=scheduler,
            reason=DEMO_REASON,
        )
        if name == "Alex Kim":
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FLOAT_PRECEPTOR,
                effective_start=DEMO_HORIZON_START,
                effective_end=DEMO_HORIZON_END,
                scheduler=scheduler,
                reason=DEMO_REASON,
            )
        if name == EXTERNAL_PATIENT_CARE_PHYSICIAN:
            add_role(
                physician=physician,
                role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
                effective_start=DEMO_HORIZON_START,
                effective_end=DEMO_HORIZON_END,
                scheduler=scheduler,
                reason=DEMO_REASON,
            )
        if name == NO_TEMPLATE_PHYSICIAN:
            continue
        first = patterns[len(physicians) - 1 if len(physicians) < 9 else 8]
        if index < 4:
            create_faculty_availability_template(
                physician=physician,
                effective_start=DEMO_HORIZON_START,
                effective_end=date(2026, 12, 31),
                slots=first,
                scheduler=scheduler,
                reason=DEMO_REASON,
            )
            create_faculty_availability_template(
                physician=physician,
                effective_start=date(2027, 1, 1),
                effective_end=DEMO_HORIZON_END,
                slots=patterns[(index + 3) % len(patterns)],
                scheduler=scheduler,
                reason=DEMO_REASON,
            )
        else:
            create_faculty_availability_template(
                physician=physician,
                effective_start=DEMO_HORIZON_START,
                effective_end=DEMO_HORIZON_END,
                slots=first,
                scheduler=scheduler,
                reason=DEMO_REASON,
            )
    for name in DEMO_COMMUNITY_PRECEPTORS:
        physician = create_physician(display_name=name, scheduler=scheduler, reason=DEMO_REASON)
        physicians[name] = physician
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=DEMO_HORIZON_START,
            effective_end=DEMO_HORIZON_END,
            faculty_equivalent=True,
            scheduler=scheduler,
            reason=DEMO_REASON,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=DEMO_HORIZON_START,
            effective_end=None if name == "Fictional Harbor" else date(2026, 9, 30),
            scheduler=scheduler,
            reason=DEMO_REASON,
        )
    training_room = create_custom_availability_category(
        name="Sports Med Training Room",
        impact="available",
        scheduler=scheduler,
        reason=DEMO_REASON,
    )
    sports_med_faculty = physicians["Alex Kim"]
    set_custom_availability_category_physicians(
        category=training_room,
        physicians=[sports_med_faculty],
        scheduler=scheduler,
        reason=DEMO_REASON,
    )
    template = sports_med_faculty.faculty_availability_templates.order_by("pk").first()
    slots = dict(template.slots)
    slots["monday_am"] = {"category_id": training_room.pk}
    correct_faculty_availability_template(
        template=template,
        effective_start=template.effective_start,
        effective_end=template.effective_end,
        slots=slots,
        scheduler=scheduler,
        reason=DEMO_REASON,
    )
    return physicians


def _seed_vacations(physicians, scheduler):
    for index, name in enumerate(DEMO_PHYSICIANS):
        physician = physicians[name]
        first_monday = date(2026, 8, 3) + timedelta(weeks=index)
        for week_offset in (0, 12, 24):
            start_date = first_monday + timedelta(weeks=week_offset)
            create_vacation(
                physician=physician,
                start_date=start_date,
                end_date=start_date + timedelta(days=4),
                scheduler=scheduler,
            )
        for week_offset, period_of_day in zip(
            (4, 8, 16),
            (Vacation.PeriodOfDay.WHOLE_DAY, Vacation.PeriodOfDay.AM, Vacation.PeriodOfDay.PM),
            strict=True,
        ):
            on_date = first_monday + timedelta(weeks=week_offset, days=2)
            create_vacation(
                physician=physician,
                start_date=on_date,
                end_date=on_date,
                period_of_day=period_of_day,
                scheduler=scheduler,
            )


def _seed_academic_events(physicians, scheduler):
    events = (
        (
            "Department Meeting",
            date(2026, 8, 4),
            date(2026, 8, 4),
            AcademicEvent.PeriodOfDay.WHOLE_DAY,
            ("Alex Kim", "Avery Patel"),
        ),
        (
            "Clinical Teaching Intensive Course",
            date(2026, 8, 17),
            date(2026, 8, 19),
            AcademicEvent.PeriodOfDay.AM,
            ("Casey Nguyen", "Devon Park"),
        ),
        (
            "Collaborative Assessment Conference",
            date(2026, 9, 10),
            date(2026, 9, 10),
            AcademicEvent.PeriodOfDay.PM,
            ("Jordan Lee",),
        ),
        (
            "Curriculum Design Conference",
            date(2026, 9, 21),
            date(2026, 9, 22),
            AcademicEvent.PeriodOfDay.WHOLE_DAY,
            ("Morgan Reed", "Quinn Baker", "Riley Chen"),
        ),
    )
    definitions = {}
    for (
        base_name,
        expected_quarter,
        duration,
        period_of_day,
        participant_count,
    ) in DEMO_ACADEMIC_EVENT_DEFINITIONS:
        definitions[base_name] = create_academic_event_definition(
            base_name=base_name,
            start_academic_year=2026,
            expected_quarter=expected_quarter,
            default_duration_days=duration,
            default_period_of_day=period_of_day,
            default_required_participant_count=participant_count,
            typically_scheduled=f"{expected_quarter} planning window",
            scheduler=scheduler,
        )
    for base_name, start_date, end_date, period_of_day, participant_names in events:
        create_academic_event_occurrence(
            definition=definitions[base_name],
            academic_year=2026,
            start_date=start_date,
            end_date=end_date,
            period_of_day=period_of_day,
            participants=[physicians[name] for name in participant_names],
            scheduler=scheduler,
            reason=DEMO_REASON,
        )
    individual_monthly = create_academic_event_definition(
        base_name="Monthly Faculty Workshop",
        scheduling_mode="monthly_rules",
        start_academic_year=2026,
        default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
        default_required_participant_count=2,
        default_participants=(physicians["Alex Kim"], physicians["Avery Patel"]),
        typically_scheduled="First and third Thursday",
        scheduler=scheduler,
    )
    collective_monthly = create_academic_event_definition(
        base_name="Monthly Faculty Forum",
        scheduling_mode="monthly_rules",
        start_academic_year=2026,
        default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
        all_available_faculty=True,
        typically_scheduled="First and third Friday",
        scheduler=scheduler,
    )
    create_academic_event_monthly_rule(
        definition=individual_monthly,
        weekday=3,
        ordinals=[1, 3],
        effective_start=DEMO_HORIZON_START,
        scheduler=scheduler,
        reason=DEMO_REASON,
    )
    create_academic_event_monthly_rule(
        definition=collective_monthly,
        weekday=4,
        ordinals=[1, 3],
        effective_start=DEMO_HORIZON_START,
        scheduler=scheduler,
        reason=DEMO_REASON,
    )
    initialize_monthly_academic_event_occurrences(
        start_date=DEMO_HORIZON_START,
        end_date=date(2026, 9, 30),
        scheduler=scheduler,
        reason=DEMO_REASON,
    )


def _seed_rounding(physicians, scheduler):
    rounder_roster = tuple(physicians[name] for name in ("Alex Kim", "Casey Nguyen", "Morgan Reed"))
    week = date(2026, 8, 3)
    roster_index = 0
    while week <= date(2026, 9, 28):
        record_normal_rounder(
            week=week,
            physician=rounder_roster[roster_index % len(rounder_roster)],
            scheduler=scheduler,
            reason=DEMO_REASON,
        )
        week += timedelta(weeks=1)
        roster_index += 1

    casual_rules = (
        ("Fictional Cedar", 0, "all", DEMO_HORIZON_START, date(2026, 9, 30)),
        ("Fictional Harbor", 2, [1, 3, 5], DEMO_HORIZON_START, None),
        ("Fictional Summit", 4, [2, 4], DEMO_HORIZON_START, date(2026, 9, 30)),
    )
    for physician_name, weekday, ordinals, effective_start, effective_end in casual_rules:
        create_casual_rounder_rule(
            physician=physicians[physician_name],
            weekday=weekday,
            ordinals=ordinals,
            effective_start=effective_start,
            effective_end=effective_end,
            scheduler=scheduler,
            reason=DEMO_REASON,
        )


def _seed_sessions(physicians, scheduler):
    open_monthly_draft(date(2026, 8, 1))
    open_monthly_draft(date(2026, 9, 1))
    intentional_assignments = {
        (date(2026, 8, 4), "AM"): ("Alex Kim",),
        (date(2026, 8, 14), "AM"): ("Alex Kim",),
        (date(2026, 9, 3), "AM"): ("Alex Kim",),
        (date(2026, 9, 4), "AM"): ("Alex Kim",),
    }
    sessions = PreceptorSession.objects.filter(
        date__range=(date(2026, 8, 3), date(2026, 9, 30))
    ).order_by("date", "period_of_day")
    for index, session in enumerate(sessions):
        if holiday_closure_for(session.date, session.period_of_day):
            continue
        if session.date <= date(2026, 8, 28):
            primary_positions = (1, 2)
        elif index % 3 == 0:
            primary_positions = (1, 2)
        elif index % 3 == 1:
            primary_positions = (1,)
        else:
            primary_positions = ()
        selected_names = [
            rule.physician.display_name
            for rule in CommunityPreceptorRule.objects.filter(
                weekday=session.date.weekday()
            ).select_related("physician")
            if rule.matches(session.date, session.period_of_day)
        ]
        selected_names.extend(
            intentional_assignments.get((session.date, session.period_of_day), ())
        )
        for primary_position in primary_positions:
            if selected_names:
                physician = physicians[selected_names.pop(0)]
            else:
                assigned_ids = set(session.assignments.values_list("physician_id", flat=True))
                available_physicians = [
                    physician
                    for physician in physicians.values()
                    if physician.pk not in assigned_ids
                    and effective_preceptor_availability_on(
                        physician, session.date, session.period_of_day
                    ).availability
                    == "available"
                ]
                if not available_physicians:
                    raise RuntimeError(
                        f"No available demo physician for {session.date} {session.period_of_day}."
                    )
                physician = available_physicians[0]
            assign_preceptor(
                session=session,
                physician=physician,
                responsibility="primary",
                primary_position=primary_position,
                scheduler=scheduler,
                reason=DEMO_REASON,
            )


def _seed_community_preceptor_rules(physicians, scheduler):
    rules = (
        ("Fictional Harbor", 0, [4], ["AM", "PM"], date(2026, 8, 1), date(2026, 8, 30)),
        ("Fictional Summit", 2, [2, 4], ["AM", "PM"], date(2026, 8, 1), date(2026, 9, 15)),
        ("Fictional Summit", 4, [3, 4], ["PM"], date(2026, 8, 15), date(2026, 9, 7)),
    )
    for name, weekday, ordinals, periods, effective_start, effective_end in rules:
        create_community_preceptor_rule(
            physician=physicians[name],
            weekday=weekday,
            ordinals=ordinals,
            periods=periods,
            effective_start=effective_start,
            effective_end=effective_end,
            scheduler=scheduler,
            reason=DEMO_REASON,
        )


def _seed_monthly_availability_rules(physicians, scheduler):
    training_room = CustomAvailabilityCategory.objects.get(name="Sports Med Training Room")
    rules = (
        (
            "Alex Kim",
            1,
            [2],
            ["AM"],
            {"category_id": training_room.pk},
            date(2026, 8, 1),
            None,
        ),
        ("Avery Patel", 2, [1, 3], ["PM"], {"base_state": "backup_only"}, None, None),
        ("Taylor Adams", 3, [3], ["AM", "PM"], {"base_state": "open"}, None, None),
    )
    for (
        name,
        weekday,
        ordinals,
        periods,
        availability_selection,
        effective_start,
        effective_end,
    ) in rules:
        create_monthly_physician_availability_rule(
            physician=physicians[name],
            weekday=weekday,
            ordinals=ordinals,
            periods=periods,
            availability_selection=availability_selection,
            effective_start=effective_start,
            effective_end=effective_end,
            scheduler=scheduler,
            reason=DEMO_REASON,
        )


def _seed_interviews(scheduler):
    shifts = (
        (date(2026, 10, 5), "hard"),
        (date(2026, 10, 14), "program_director"),
        (date(2026, 10, 23), "backup_only"),
        (date(2026, 10, 29), "available"),
        (date(2026, 11, 3), "hard"),
        (date(2026, 11, 12), "available"),
        (date(2026, 11, 20), "backup_only"),
        (date(2026, 11, 30), "available"),
        (date(2026, 12, 4), "backup_only"),
        (date(2026, 12, 11), "available"),
        (date(2026, 12, 17), "available"),
        (date(2026, 12, 29), "hard"),
        (date(2027, 1, 6), "program_director"),
        (date(2027, 1, 14), "backup_only"),
        (date(2027, 1, 25), "hard"),
    )
    for on_date, assignment_kind in shifts:
        shift = create_interview_shift(academic_year=2026, date=on_date, scheduler=scheduler)
        candidates = interview_assignment_picker_candidates_on(on_date)
        if assignment_kind == "hard":
            candidate = next(
                candidate for candidate in candidates if candidate.availability == "not_available"
            )
        elif assignment_kind == "backup_only":
            candidate = next(
                (candidate for candidate in candidates if candidate.tier == "backup_only"), None
            )
            if candidate is None:
                raise RuntimeError(f"No backup-only demo Interview candidate for {on_date}.")
        elif assignment_kind == "program_director":
            candidate = next(
                candidate for candidate in candidates if candidate.tier == "program_director"
            )
        else:
            candidate = next(
                candidate for candidate in candidates if candidate.tier == "available_faculty"
            )
        assign_interview_shift(
            shift=shift,
            physician=candidate.physician,
            scheduler=scheduler,
            override=assignment_kind == "hard",
        )


def seed_demo_data(*, scheduler=None, scheduling_administrator=None):
    ensure_lead_time_defaults()
    ensure_system_schedule_options()
    user_model = get_user_model()
    scheduler = scheduler or user_model.objects.filter(username=DEMO_USERNAME).first()
    scheduler_created = scheduler is None
    if scheduler_created:
        scheduler = user_model.objects.create_user(username=DEMO_USERNAME, password=DEMO_PASSWORD)
    scheduling_administrator = (
        scheduling_administrator
        or user_model.objects.filter(username=DEMO_SCHEDULING_ADMINISTRATOR_USERNAME).first()
    )
    scheduling_administrator_created = scheduling_administrator is None
    if scheduling_administrator_created:
        scheduling_administrator = user_model.objects.create_user(
            username=DEMO_SCHEDULING_ADMINISTRATOR_USERNAME
        )
        group, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        scheduling_administrator.groups.add(group)
        permission = Permission.objects.get(
            content_type__app_label="schedules",
            content_type__model="physician",
            codename="manage_scheduling_administration",
        )
        if not group.permissions.filter(pk=permission.pk).exists():
            scheduling_administrator.user_permissions.add(permission)
    physicians = _seed_physicians(scheduling_administrator)
    _seed_vacations(physicians, scheduler)
    _seed_academic_events(physicians, scheduler)
    _seed_rounding(physicians, scheduler)
    _seed_community_preceptor_rules(physicians, scheduler)
    _seed_monthly_availability_rules(physicians, scheduler)
    _seed_sessions(physicians, scheduler)
    _seed_interviews(scheduler)
    return scheduler_created


class Command(BaseCommand):
    help = (
        "Destructively rebuild the canonical local SQLite database with synthetic demo data "
        "or, with --empty, no application data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help=(
                "Required acknowledgement before reset. By default destroys Django login accounts and "
                "authority, sessions, invitation state, account-change history, Physician-account links, "
                "Physicians, scheduling data, and retained domain history. With --except-logins, preserves "
                "Django login accounts and authority while destroying the other listed categories."
            ),
        )
        parser.add_argument(
            "--except-logins",
            action="store_true",
            help=(
                "Preserve Django login accounts, password hashes, unique email, active, Staff, and superuser "
                "flags, group memberships, and direct permissions. Destroy sessions, invitation state, "
                "account-change history, Physician-account links, Physicians, scheduling data, and retained "
                "domain history."
            ),
        )
        parser.add_argument(
            "--empty",
            action="store_true",
            help=(
                "Rebuild an empty migrated database instead of seeding synthetic demo data. "
                "Combine with --except-logins to retain Django login accounts and authority."
            ),
        )

    def handle(self, *args, **options):
        preserve_logins = options.get("except_logins", False)
        empty = options.get("empty", False)
        result_description = (
            "The rebuilt local database will be empty."
            if empty
            else "Synthetic demo data will be created."
        )
        if not options["confirm"]:
            if preserve_logins:
                reset_scope = (
                    "Django login accounts and authority will be preserved, but sessions, invitation state, "
                    "account-change history, Physician-account links, Physicians, scheduling data, and "
                    "retained domain history will be permanently lost"
                )
            else:
                reset_scope = (
                    "Django login accounts and authority, sessions, invitation state, account-change history, "
                    "Physician-account links, Physicians, scheduling data, and retained domain history will "
                    "be permanently lost"
                )
            raise CommandError(
                f"Refusing to reset: {reset_scope}. {result_description} Re-run with --confirm."
            )
        database_path = _reset_database_path()
        snapshot = _capture_login_accounts() if preserve_logins else ()
        if preserve_logins:
            self.stdout.write(
                self.style.WARNING(
                    "Preserving Django login accounts, password hashes, unique email, active, Staff, and "
                    "superuser flags, group memberships, and direct permissions. Destroying sessions, "
                    "invitation state, account-change history, Physician-account links, Physicians, "
                    "scheduling data, and retained domain history."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Destroying Django login accounts and authority, sessions, invitation state, "
                    "account-change history, Physician-account links, Physicians, scheduling data, and "
                    "retained domain history."
                )
            )
        self.stdout.write(result_description)
        connections.close_all()
        database_path.unlink(missing_ok=True)
        call_command("migrate", interactive=False, verbosity=options["verbosity"])
        try:
            _restore_login_accounts(snapshot)
        except CommandError as error:
            raise CommandError(f"Login restore failed after database rebuild: {error}") from error
        if empty:
            self.stdout.write(self.style.SUCCESS("Local database rebuilt empty."))
            return
        scheduler_created = seed_demo_data()
        self.stdout.write(self.style.SUCCESS("Local demonstration data created."))
        if scheduler_created:
            self.stdout.write(
                "Canonical demo account created; its credentials are documented in README."
            )
        else:
            self.stdout.write(
                "Existing canonical demo account retained; its credentials were not changed."
            )
