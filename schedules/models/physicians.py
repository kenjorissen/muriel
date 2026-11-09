import calendar
from datetime import date, timedelta
from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from ..services.operational_attention import organization_today


class Physician(models.Model):
    """A scheduled physician, deliberately separate from an application account."""

    display_name = models.CharField(max_length=200)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="physician_identity",
    )
    faculty_precepting_capacity_override = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
        validators=(MinValueValidator(0), MaxValueValidator(10)),
        help_text="Optional weekly Faculty Precepting workload target, from 0 through 10 shifts.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("display_name", "pk")
        permissions = (
            ("manage_scheduling_administration", "Can manage scheduling administration"),
        )

    def __str__(self):
        return self.display_name

    def delete(self, *args, **kwargs):
        raise ValidationError("Physicians are retained scheduling records and cannot be deleted.")


class EffectiveRole(models.Model):
    class Role(models.TextChoices):
        FACULTY = "faculty", "Faculty"
        COMMUNITY_ROUNDER = "community_rounder", "Community Rounder"
        COMMUNITY_PRECEPTOR = "community_preceptor", "Community Preceptor"
        CASUAL_ROUNDER = "casual_rounder", "Casual Rounder"
        FLOAT_PRECEPTOR = "float_preceptor", "Float Preceptor"
        EXTERNAL_PATIENT_CARE = "external_patient_care", "External Patient Care"

    class FacultyLeadership(models.TextChoices):
        NONE = "", "None"
        PROGRAM_DIRECTOR = "program_director", "Program Director (PD)"
        ASSOCIATE_PROGRAM_DIRECTOR = (
            "associate_program_director",
            "Associate Program Director (APD)",
        )

    physician = models.ForeignKey(Physician, on_delete=models.PROTECT, related_name="roles")
    role = models.CharField(max_length=24, choices=Role.choices)
    effective_start = models.DateField()
    effective_end = models.DateField(null=True, blank=True)
    faculty_equivalent = models.BooleanField(default=False)
    faculty_leadership = models.CharField(
        max_length=28, choices=FacultyLeadership.choices, blank=True, default=""
    )

    class Meta:
        ordering = ("role", "effective_start", "pk")

    def clean(self):
        super().clean()
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})
        if self.role != self.Role.COMMUNITY_PRECEPTOR and self.faculty_equivalent:
            raise ValidationError(
                {
                    "faculty_equivalent": "Faculty-equivalent applies only to Community Preceptor roles."
                }
            )
        if self.role != self.Role.FACULTY and self.faculty_leadership:
            raise ValidationError(
                {"faculty_leadership": "Faculty leadership applies only to Faculty roles."}
            )

        if not self.effective_start or not self.physician_id:
            return

        overlapping = EffectiveRole.objects.filter(
            physician=self.physician, role=self.role
        ).exclude(pk=self.pk)
        if self.effective_end:
            overlapping = overlapping.filter(effective_start__lte=self.effective_end)
        overlapping = overlapping.filter(
            models.Q(effective_end__isnull=True) | models.Q(effective_end__gte=self.effective_start)
        )
        if overlapping.exists():
            raise ValidationError(
                "This role has an overlapping effective range for this physician."
            )

        incompatible_roles = {
            self.Role.FACULTY: (
                self.Role.COMMUNITY_ROUNDER,
                self.Role.COMMUNITY_PRECEPTOR,
                self.Role.CASUAL_ROUNDER,
            ),
            self.Role.COMMUNITY_ROUNDER: (self.Role.FACULTY,),
            self.Role.COMMUNITY_PRECEPTOR: (self.Role.FACULTY,),
            self.Role.CASUAL_ROUNDER: (self.Role.FACULTY,),
        }
        incompatible = EffectiveRole.objects.filter(
            physician=self.physician,
            role__in=incompatible_roles.get(self.role, ()),
        ).exclude(pk=self.pk)
        if self.effective_end:
            incompatible = incompatible.filter(effective_start__lte=self.effective_end)
        incompatible = incompatible.filter(
            models.Q(effective_end__isnull=True) | models.Q(effective_end__gte=self.effective_start)
        )
        conflict = incompatible.order_by("effective_start", "pk").first()
        if conflict:
            current_label = self.get_role_display().lower().replace(" ", "-")
            conflict_label = conflict.get_role_display().lower().replace(" ", "-")
            conflict_end = (
                conflict.effective_end.isoformat() if conflict.effective_end else "ongoing"
            )
            raise ValidationError(
                f"{current_label} role cannot overlap the effective {conflict_label} role "
                f"from {conflict.effective_start.isoformat()} through {conflict_end}."
            )

        _validate_dependent_role_coverage(self)

    def __str__(self):
        return f"{self.physician}: {self.get_role_display()} from {self.effective_start}"


def _validate_dependent_role_coverage(proposed_role):
    dependent_roles = {
        EffectiveRole.Role.FLOAT_PRECEPTOR: {
            "coverage_roles": {
                EffectiveRole.Role.FACULTY,
                EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            },
            "message": (
                "Every Float Preceptor date must be covered by an effective Faculty or "
                "Community Preceptor role."
            ),
        },
        EffectiveRole.Role.EXTERNAL_PATIENT_CARE: {
            "coverage_roles": {EffectiveRole.Role.FACULTY},
            "message": "Every External Patient Care date must be covered by an effective Faculty role.",
        },
    }
    roles_of_interest = set(dependent_roles)
    roles_of_interest.update(
        role for dependency in dependent_roles.values() for role in dependency["coverage_roles"]
    )
    roles = list(
        EffectiveRole.objects.filter(physician_id=proposed_role.physician_id)
        .exclude(pk=proposed_role.pk)
        .filter(role__in=roles_of_interest)
    )
    if proposed_role.role in roles_of_interest:
        roles.append(proposed_role)

    for dependent_role, dependency in dependent_roles.items():
        coverage_ranges = sorted(
            (
                (role.effective_start, role.effective_end)
                for role in roles
                if role.role in dependency["coverage_roles"]
            ),
            key=lambda interval: interval[0],
        )
        for role in roles:
            if role.role == dependent_role and not _effective_range_is_covered(
                role.effective_start, role.effective_end, coverage_ranges
            ):
                raise ValidationError(dependency["message"])


def _effective_range_is_covered(start, end, coverage_ranges):
    uncovered_start = start
    for coverage_start, coverage_end in coverage_ranges:
        if coverage_end and coverage_end < uncovered_start:
            continue
        if coverage_start > uncovered_start:
            return False
        if coverage_end is None:
            return True
        uncovered_start = coverage_end + timedelta(days=1)
        if end and uncovered_start > end:
            return True
    return False


class FacultyAvailabilityTemplate(models.Model):
    """A manually maintained, effective-dated weekly faculty schedule input."""

    class AvailabilityState(models.TextChoices):
        OPEN = "open", "Open"
        BACKUP_ONLY = "backup_only", "Backup only"
        PATIENT_CARE = "patient_care", "Patient care"
        OFF = "off", "Off"

    _BASE_STATE_IMPACTS: ClassVar = {
        AvailabilityState.OPEN: "available",
        AvailabilityState.BACKUP_ONLY: "backup_only",
        AvailabilityState.PATIENT_CARE: "patient_care",
        AvailabilityState.OFF: "not_available",
    }

    SLOT_CHOICES = (
        ("monday_am", "Monday AM"),
        ("monday_pm", "Monday PM"),
        ("tuesday_am", "Tuesday AM"),
        ("tuesday_pm", "Tuesday PM"),
        ("wednesday_am", "Wednesday AM"),
        ("wednesday_pm", "Wednesday PM"),
        ("thursday_am", "Thursday AM"),
        ("thursday_pm", "Thursday PM"),
        ("friday_am", "Friday AM"),
        ("friday_pm", "Friday PM"),
    )

    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="faculty_availability_templates"
    )
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    slots = models.JSONField()

    class Meta:
        ordering = ("effective_start", "pk")

    @classmethod
    def base_selection(cls, state):
        return {"base_state": state}

    @classmethod
    def base_state_impact(cls, state):
        return cls._BASE_STATE_IMPACTS[state]

    @staticmethod
    def category_selection(category):
        return {"category_id": category.pk, "category_name": category.name}

    @classmethod
    def selection_choice_value(cls, selection):
        if "base_state" in selection:
            return f"base:{selection['base_state']}"
        return f"category:{selection['category_id']}"

    @staticmethod
    def selection_category_id(selection):
        return selection.get("category_id") if isinstance(selection, dict) else None

    @classmethod
    def selection_label(cls, selection):
        if "base_state" in selection:
            return cls.AvailabilityState(selection["base_state"]).label
        return selection["category_name"]

    def clean(self):
        super().clean()
        if (
            self.effective_end
            and self.effective_start
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})

        expected_slots = {key for key, _ in self.SLOT_CHOICES}
        if not isinstance(self.slots, dict) or set(self.slots) != expected_slots:
            raise ValidationError(
                {"slots": "Provide exactly one value for every weekday AM/PM slot."}
            )
        invalid_values = []
        for selection in self.slots.values():
            if not isinstance(selection, dict):
                invalid_values.append(selection)
                continue
            if set(selection) == {"base_state"}:
                if selection["base_state"] not in self.AvailabilityState.values:
                    invalid_values.append(selection)
            elif set(selection) == {"category_id", "category_name"}:
                if (
                    not isinstance(selection["category_id"], int)
                    or isinstance(selection["category_id"], bool)
                    or selection["category_id"] <= 0
                    or not isinstance(selection["category_name"], str)
                    or not selection["category_name"].strip()
                ):
                    invalid_values.append(selection)
            else:
                invalid_values.append(selection)
        if invalid_values:
            raise ValidationError(
                {
                    "slots": (
                        "Each slot must identify one availability state or retained schedule option."
                    )
                }
            )
        _validate_float_schedule_selections(
            self.physician_id,
            self.slots.values(),
            "slots",
            on_date=self.effective_start,
            effective_end=self.effective_end,
        )

        overlapping = FacultyAvailabilityTemplate.objects.filter(physician=self.physician).exclude(
            pk=self.pk
        )
        if self.effective_end:
            overlapping = overlapping.filter(
                models.Q(effective_start__isnull=True)
                | models.Q(effective_start__lte=self.effective_end)
            )
        if self.effective_start:
            overlapping = overlapping.filter(
                models.Q(effective_end__isnull=True)
                | models.Q(effective_end__gte=self.effective_start)
            )
        if overlapping.exists():
            raise ValidationError(
                "Faculty Schedule templates cannot have overlapping effective ranges."
            )


class MonthlyPhysicianAvailabilityRule(models.Model):
    """A retained ordinal-weekday override for one physician's weekly template."""

    class PeriodOfDay(models.TextChoices):
        AM = "AM", "AM"
        PM = "PM", "PM"

    WEEKDAY_CHOICES = (
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    )
    MONTH_CHOICES = tuple((month, calendar.month_name[month]) for month in range(1, 13))

    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="monthly_availability_rules"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    ordinals = models.JSONField()
    periods = models.JSONField(default=list)
    availability_selection = models.JSONField()
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    annual_start_month = models.PositiveSmallIntegerField(
        choices=MONTH_CHOICES, null=True, blank=True
    )
    annual_end_month = models.PositiveSmallIntegerField(
        choices=MONTH_CHOICES, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("physician__display_name", "weekday", "effective_start", "pk")

    def clean(self):
        super().clean()
        if self.weekday not in range(7):
            raise ValidationError({"weekday": "Choose one day of the week."})
        try:
            ordinal_values = set(self.ordinals)
        except TypeError, AttributeError:
            ordinal_values = set()
        if (
            not isinstance(self.ordinals, list)
            or not self.ordinals
            or ordinal_values - set(range(1, 6))
            or len(ordinal_values) != len(self.ordinals)
        ):
            raise ValidationError(
                {"ordinals": "Choose one or more of the first through fifth occurrences."}
            )
        try:
            period_values = set(self.periods)
        except TypeError, AttributeError:
            period_values = set()
        if (
            not isinstance(self.periods, list)
            or not self.periods
            or period_values - set(self.PeriodOfDay.values)
            or len(period_values) != len(self.periods)
        ):
            raise ValidationError({"periods": "Select AM, PM, or both periods."})
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})
        if (self.annual_start_month is None) != (self.annual_end_month is None):
            raise ValidationError(
                {
                    "annual_start_month": "Choose both annual window months or leave both blank.",
                    "annual_end_month": "Choose both annual window months or leave both blank.",
                }
            )
        if not _is_availability_selection(self.availability_selection):
            raise ValidationError(
                {
                    "availability_selection": "Choose one availability state or retained schedule option."
                }
            )
        _validate_float_schedule_selections(
            self.physician_id,
            (self.availability_selection,),
            "availability_selection",
            on_date=self.effective_start,
            effective_end=self.effective_end,
        )
        if not self.is_active or not self.physician_id:
            return
        candidates = MonthlyPhysicianAvailabilityRule.objects.filter(
            physician=self.physician, weekday=self.weekday, is_active=True
        ).exclude(pk=self.pk)
        for candidate in candidates:
            if _monthly_rules_overlap(self, candidate):
                raise ValidationError(
                    "Monthly scheduling rules cannot govern the same physician, date, and period."
                )

    def matches(self, on_date, period_of_day=None):
        return (
            self.is_active
            and on_date.weekday() == self.weekday
            and _weekday_ordinal_in_month(on_date) in self.ordinals
            and on_date.month in self.annual_months
            and (not self.effective_start or self.effective_start <= on_date)
            and (not self.effective_end or on_date <= self.effective_end)
            and (period_of_day is None or period_of_day in self.periods)
        )

    @property
    def annual_months(self):
        return _annual_window_months(self.annual_start_month, self.annual_end_month)

    @property
    def ordinal_display(self):
        labels = ("First", "Second", "Third", "Fourth", "Fifth")
        return ", ".join(labels[ordinal - 1] for ordinal in self.ordinals)

    @property
    def periods_display(self):
        if set(self.periods) == set(self.PeriodOfDay.values):
            return "AM and PM"
        return " and ".join(period for period in self.PeriodOfDay.values if period in self.periods)

    @property
    def availability_display(self):
        return FacultyAvailabilityTemplate.selection_label(self.availability_selection)

    def delete(self, *args, **kwargs):
        raise ValidationError("Monthly scheduling rules are retained and cannot be deleted.")


class PhysicianChange(models.Model):
    class Subject(models.TextChoices):
        IDENTITY = "identity", "Identity"
        ACCOUNT_IDENTITY = "account_identity", "Account identity"
        ROLE = "role", "Role"
        FACULTY_AVAILABILITY_TEMPLATE = "faculty_availability_template", "Faculty Schedule template"
        MONTHLY_AVAILABILITY_RULE = "monthly_availability_rule", "Monthly scheduling rule"
        FACULTY_PRECEPTING_CAPACITY_OVERRIDE = (
            "faculty_precepting_capacity_override",
            "Faculty Precepting capacity override",
        )

    physician = models.ForeignKey(Physician, on_delete=models.PROTECT, related_name="changes")
    subject = models.CharField(max_length=40, choices=Subject.choices)
    subject_id = models.PositiveBigIntegerField(null=True, blank=True)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_physicians_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def __str__(self):
        return f"{self.physician} {self.get_subject_display()} change at {self.changed_at}"


def _is_availability_selection(selection):
    if not isinstance(selection, dict):
        return False
    if set(selection) == {"base_state"}:
        return selection["base_state"] in FacultyAvailabilityTemplate.AvailabilityState.values
    return (
        set(selection) == {"category_id", "category_name"}
        and isinstance(selection["category_id"], int)
        and not isinstance(selection["category_id"], bool)
        and selection["category_id"] > 0
        and isinstance(selection["category_name"], str)
        and bool(selection["category_name"].strip())
    )


def float_schedule_input_mode(physician, on_date=None):
    """Return the schedule-input mode supported by roles effective on ``on_date``."""
    on_date = on_date or organization_today()
    roles = set(
        EffectiveRole.objects.filter(
            physician=physician,
            effective_start__lte=on_date,
        )
        .filter(models.Q(effective_end__isnull=True) | models.Q(effective_end__gte=on_date))
        .values_list("role", flat=True)
    )
    if EffectiveRole.Role.FACULTY in roles:
        return "faculty"
    if {
        EffectiveRole.Role.COMMUNITY_PRECEPTOR,
        EffectiveRole.Role.FLOAT_PRECEPTOR,
    } <= roles:
        return "community_float"
    return None


def has_schedule_input_role_overlap(physician, effective_start=None, effective_end=None):
    """Whether a schedule-input role applies at any point in an effective range."""
    if _role_overlaps_effective_range(
        physician, EffectiveRole.Role.FACULTY, effective_start, effective_end
    ).exists():
        return True
    return any(
        _effective_ranges_overlap(
            (community.effective_start, community.effective_end),
            (floating.effective_start, floating.effective_end),
            (effective_start, effective_end),
        )
        for community in _role_overlaps_effective_range(
            physician, EffectiveRole.Role.COMMUNITY_PRECEPTOR, effective_start, effective_end
        )
        for floating in _role_overlaps_effective_range(
            physician, EffectiveRole.Role.FLOAT_PRECEPTOR, effective_start, effective_end
        )
    )


def has_community_float_schedule_input_overlap(physician, effective_start=None, effective_end=None):
    """Whether part of a range is governed by the Community Preceptor Float mode."""
    faculty_ranges = tuple(
        _role_overlaps_effective_range(
            physician, EffectiveRole.Role.FACULTY, effective_start, effective_end
        ).values_list("effective_start", "effective_end")
    )
    for community in _role_overlaps_effective_range(
        physician, EffectiveRole.Role.COMMUNITY_PRECEPTOR, effective_start, effective_end
    ):
        for floating in _role_overlaps_effective_range(
            physician, EffectiveRole.Role.FLOAT_PRECEPTOR, effective_start, effective_end
        ):
            overlap = _effective_range_intersection(
                (community.effective_start, community.effective_end),
                (floating.effective_start, floating.effective_end),
                (effective_start, effective_end),
            )
            if overlap and _range_has_date_outside(overlap, faculty_ranges):
                return True
    return False


def _role_overlaps_effective_range(physician, role, effective_start, effective_end):
    roles = EffectiveRole.objects.filter(physician=physician, role=role)
    if effective_end is not None:
        roles = roles.filter(effective_start__lte=effective_end)
    if effective_start is not None:
        roles = roles.filter(
            models.Q(effective_end__isnull=True) | models.Q(effective_end__gte=effective_start)
        )
    return roles


def _effective_ranges_overlap(*ranges):
    return _effective_range_intersection(*ranges) is not None


def _effective_range_intersection(*ranges):
    starts = (start for start, _end in ranges if start is not None)
    ends = (end for _start, end in ranges if end is not None)
    start = max(starts, default=None)
    end = min(ends, default=None)
    if start is not None and end is not None and end < start:
        return None
    return start, end


def _range_has_date_outside(range_to_check, covered_ranges):
    """Return whether an inclusive range has a date outside all covered ranges."""
    start, end = range_to_check
    cursor = start
    for covered_start, covered_end in sorted(covered_ranges, key=lambda covered: covered[0]):
        overlap = _effective_range_intersection((start, end), (covered_start, covered_end))
        if overlap is None:
            continue
        covered_start, covered_end = overlap
        if covered_start > cursor:
            return True
        if covered_end is None:
            return False
        if covered_end >= cursor:
            cursor = covered_end + timedelta(days=1)
        if end is not None and cursor > end:
            return False
    return True


def has_automatic_admin_option(physician, on_date=None):
    on_date = on_date or organization_today()
    return has_automatic_admin_option_overlap(
        physician, effective_start=on_date, effective_end=on_date
    )


def has_automatic_admin_option_overlap(physician, effective_start=None, effective_end=None):
    """Whether Float automatically authorizes Admin anywhere in an effective range."""
    schedule_input_roles = (
        EffectiveRole.Role.FACULTY,
        EffectiveRole.Role.COMMUNITY_PRECEPTOR,
    )
    return any(
        _effective_ranges_overlap(
            (schedule_input_role.effective_start, schedule_input_role.effective_end),
            (floating.effective_start, floating.effective_end),
            (effective_start, effective_end),
        )
        for role in schedule_input_roles
        for schedule_input_role in _role_overlaps_effective_range(
            physician, role, effective_start, effective_end
        )
        for floating in _role_overlaps_effective_range(
            physician, EffectiveRole.Role.FLOAT_PRECEPTOR, effective_start, effective_end
        )
    )


def _validate_float_schedule_selections(
    physician_id, selections, field, on_date=None, effective_end=None
):
    if not physician_id:
        return
    physician = Physician.objects.filter(pk=physician_id).first()
    if physician is None or not has_community_float_schedule_input_overlap(
        physician, effective_start=on_date, effective_end=effective_end
    ):
        return

    from .availability_categories import CustomAvailabilityCategory

    admin_category_ids = set(
        CustomAvailabilityCategory.objects.filter(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        ).values_list("pk", flat=True)
    )
    allowed = {
        ("base", FacultyAvailabilityTemplate.AvailabilityState.OFF),
        *(("category", category_id) for category_id in admin_category_ids),
    }
    selections_are_allowed = all(
        (
            ("base", selection.get("base_state"))
            if isinstance(selection, dict) and "base_state" in selection
            else ("category", selection.get("category_id"))
            if isinstance(selection, dict)
            else None
        )
        in allowed
        for selection in selections
    )
    if not selections_are_allowed:
        raise ValidationError(
            {field: ("Community Preceptor Float schedule inputs may use only Off or Admin.")}
        )


def _monthly_rules_overlap(first, second):
    if not set(first.periods).intersection(second.periods):
        return False
    common_ordinals = set(first.ordinals).intersection(second.ordinals)
    if not common_ordinals:
        return False
    common_months = first.annual_months.intersection(second.annual_months)
    if not common_months:
        return False
    start = max(
        (value for value in (first.effective_start, second.effective_start) if value is not None),
        default=None,
    )
    end_values = (
        value for value in (first.effective_end, second.effective_end) if value is not None
    )
    end = min(end_values, default=None)
    if end and start and end < start:
        return False
    if start is None and end is None:
        return True
    return _matching_ordinal_date_in_range(
        weekday=first.weekday,
        ordinals=common_ordinals,
        months=common_months,
        start=start,
        end=end,
    )


def _annual_window_months(start_month, end_month):
    if start_month is None and end_month is None:
        return frozenset(range(1, 13))
    if start_month is None or end_month is None:
        return frozenset()
    if start_month <= end_month:
        return frozenset(range(start_month, end_month + 1))
    return frozenset((*range(start_month, 13), *range(1, end_month + 1)))


def _matching_ordinal_date_in_range(*, weekday, ordinals, months, start, end):
    """Find an ordinal weekday in the date range and shared annual months.

    Gregorian month layouts repeat every 400 years, so that is a sufficient
    bound even for a one-month window containing only fifth occurrences.
    """
    month = (start or end or date(2000, 1, 1)).replace(day=1)
    direction = 1 if start or end is None else -1
    for _ in range(400 * 12):
        if month.month in months:
            first_weekday, days_in_month = calendar.monthrange(month.year, month.month)
            for ordinal in ordinals:
                day = 1 + (weekday - first_weekday) % 7 + (ordinal - 1) * 7
                if day <= days_in_month:
                    candidate = date(month.year, month.month, day)
                    if (start is None or candidate >= start) and (end is None or candidate <= end):
                        return True
        if (end and direction > 0 and month >= end.replace(day=1)) or (
            start and direction < 0 and month <= start.replace(day=1)
        ):
            break
        if (direction > 0 and month.year == date.max.year) or (
            direction < 0 and month.year == date.min.year
        ):
            break
        month = _adjacent_month(month, direction)
    return False


def _adjacent_month(month, direction):
    year = month.year + (month.month == 12 if direction > 0 else month.month == 1) * direction
    number = (
        1
        if direction > 0 and month.month == 12
        else 12
        if direction < 0 and month.month == 1
        else month.month + direction
    )
    return month.replace(year=year, month=number)


def _weekday_ordinal_in_month(on_date):
    return (on_date.day - 1) // 7 + 1


__all__ = (
    "EffectiveRole",
    "FacultyAvailabilityTemplate",
    "MonthlyPhysicianAvailabilityRule",
    "Physician",
    "PhysicianChange",
)
