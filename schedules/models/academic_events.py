from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from ..academic_years import academic_year_label
from .physicians import EffectiveRole, Physician


class AcademicEventQuerySet(models.QuerySet):
    def overlapping(self, start_date, end_date):
        if end_date < start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})
        return (
            self.filter(
                is_active=True,
                resolution=AcademicEvent.Resolution.DATED,
            )
            .filter(
                Q(ranges__start_date__lte=end_date, ranges__end_date__gte=start_date)
                | Q(
                    ranges__isnull=True,
                    start_date__lte=end_date,
                    end_date__gte=start_date,
                )
            )
            .distinct()
        )

    def for_physician(self, physician):
        return (
            self.filter(is_active=True, resolution=AcademicEvent.Resolution.DATED)
            .filter(Q(participants=physician) | Q(all_available_faculty=True))
            .distinct()
        )

    def covering(self, physician, on_date, period_of_day):
        if period_of_day not in (AcademicEvent.PeriodOfDay.AM, AcademicEvent.PeriodOfDay.PM):
            raise ValidationError({"period_of_day": "Period must be AM or PM."})
        return (
            self.for_physician(physician)
            .filter(
                Q(ranges__start_date__lte=on_date, ranges__end_date__gte=on_date)
                | Q(ranges__isnull=True, start_date__lte=on_date, end_date__gte=on_date)
            )
            .filter(
                models.Q(period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY)
                | models.Q(period_of_day=period_of_day)
            )
        )


class AcademicEventQuarter(models.TextChoices):
    Q1 = "Q1", "Q1"
    Q2 = "Q2", "Q2"
    Q3 = "Q3", "Q3"
    Q4 = "Q4", "Q4"


class AcademicEvent(models.Model):
    """One retained yearly resolution or monthly rule-owned occurrence."""

    class PeriodOfDay(models.TextChoices):
        WHOLE_DAY = "whole_day", "Whole day"
        AM = "AM", "AM only"
        PM = "PM", "PM only"
        NONE = "none", "None"

    class PreceptingImpact(models.TextChoices):
        HARD = "hard", "Hard"
        SOFT = "soft", "Soft"
        NONE = "none", "None"

    class Resolution(models.TextChoices):
        DATED = "dated", "Dated occurrence"
        NOT_OCCURRING = "not_occurring", "Not occurring this year"

    definition = models.ForeignKey(
        "AcademicEventDefinition", on_delete=models.PROTECT, related_name="annual_events"
    )
    academic_year = models.PositiveIntegerField()
    base_name = models.CharField(max_length=120)
    expected_quarter = models.CharField(
        max_length=2,
        choices=AcademicEventQuarter.choices,
        default=AcademicEventQuarter.Q1,
        null=True,
        blank=True,
    )
    suffix = models.CharField(max_length=120, blank=True, default="")
    note = models.TextField(blank=True, default="")
    required_participant_count = models.PositiveIntegerField()
    resolution = models.CharField(max_length=16, choices=Resolution.choices)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    period_of_day = models.CharField(
        max_length=9, choices=PeriodOfDay.choices, default=PeriodOfDay.WHOLE_DAY
    )
    precepting_impact = models.CharField(
        max_length=4, choices=PreceptingImpact.choices, default=PreceptingImpact.HARD
    )
    participants = models.ManyToManyField(Physician, related_name="academic_events", blank=True)
    all_available_faculty = models.BooleanField(default=False)
    assignment_is_default = models.BooleanField(default=True)
    monthly_rule = models.ForeignKey(
        "AcademicEventMonthlyRule",
        on_delete=models.SET_NULL,
        related_name="occurrences",
        null=True,
        blank=True,
    )
    monthly_rule_source_id = models.PositiveBigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AcademicEventQuerySet.as_manager()

    class Meta:
        ordering = ("academic_year", "start_date", "base_name", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("definition", "academic_year"),
                condition=Q(
                    is_active=True, monthly_rule__isnull=True, monthly_rule_source_id__isnull=True
                ),
                name="one_live_academic_event_resolution_per_year",
            ),
            models.UniqueConstraint(
                fields=("monthly_rule", "start_date"),
                condition=Q(is_active=True, monthly_rule__isnull=False),
                name="one_live_monthly_academic_event_occurrence_per_rule_date",
            ),
        )

    def clean(self):
        super().clean()
        if self.required_participant_count is not None and self.required_participant_count < 1:
            raise ValidationError(
                {
                    "required_participant_count": "Required participant count must be a positive whole number."
                }
            )
        if self.monthly_rule_id or self.monthly_rule_source_id:
            if self.monthly_rule_id and self.monthly_rule.definition_id != self.definition_id:
                raise ValidationError("A monthly occurrence must belong to its rule's definition.")
            if self.monthly_rule_id and self.monthly_rule_source_id != self.monthly_rule_id:
                raise ValidationError("A monthly occurrence must record its recurrence rule.")
            if self.resolution != self.Resolution.DATED:
                raise ValidationError("A monthly occurrence must be dated.")
            if not self.start_date or self.end_date != self.start_date:
                raise ValidationError("A monthly occurrence must have one rule-computed date.")
            if self.expected_quarter:
                raise ValidationError("A monthly occurrence has no expected quarter.")
        if self.resolution == self.Resolution.NOT_OCCURRING:
            if self.monthly_rule_id:
                raise ValidationError("A monthly occurrence cannot be marked not occurring.")
            if self.start_date or self.end_date:
                raise ValidationError("A not-occurring decision cannot have dates.")
            return
        if self.resolution != self.Resolution.DATED:
            raise ValidationError({"resolution": "Choose a valid annual resolution."})
        if not self.start_date or not self.end_date:
            raise ValidationError("A dated occurrence requires start and end dates.")
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})
        year_start = self.academic_year
        from ..academic_years import academic_year_for_date

        if (
            academic_year_for_date(self.start_date) != year_start
            or academic_year_for_date(self.end_date) != year_start
        ):
            raise ValidationError(
                {"end_date": "Occurrence dates must both fall within the selected academic year."}
            )
        _validate_impact_coverage(self.precepting_impact, self.period_of_day)

    def covers(self, on_date, period_of_day):
        if period_of_day not in (self.PeriodOfDay.AM, self.PeriodOfDay.PM):
            raise ValidationError({"period_of_day": "Period must be AM or PM."})
        if not self.is_active or self.resolution != self.Resolution.DATED:
            return False
        return any(start <= on_date <= end for start, end in self.date_ranges) and (
            self.period_of_day == self.PeriodOfDay.WHOLE_DAY or self.period_of_day == period_of_day
        )

    @property
    def presentation_coverage_label(self):
        if self.precepting_impact == self.PreceptingImpact.NONE:
            return (
                "Calendar only"
                if self.period_of_day == self.PeriodOfDay.NONE
                else (f"Calendar only ({self.get_period_of_day_display()})")
            )
        return self.get_period_of_day_display()

    def covers_physician(self, physician, on_date, period_of_day):
        if self.all_available_faculty:
            return EffectiveRole.objects.filter(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start__lte=on_date,
            ).filter(
                Q(effective_end__isnull=True) | Q(effective_end__gte=on_date)
            ).exists() and self.covers(on_date, period_of_day)
        return self.participants.filter(pk=physician.pk).exists() and self.covers(
            on_date, period_of_day
        )

    @property
    def date_ranges(self):
        ranges = getattr(self, "_prefetched_objects_cache", {}).get("ranges")
        if ranges is None and self.pk:
            ranges = tuple(self.ranges.order_by("start_date", "end_date", "pk"))
        if ranges:
            return tuple((item.start_date, item.end_date) for item in ranges)
        return ((self.start_date, self.end_date),) if self.start_date and self.end_date else ()

    @property
    def date_range_label(self):
        return "; ".join(f"{start} through {end}" for start, end in self.date_ranges)

    @property
    def title(self):
        return self.base_name if not self.suffix else f"{self.base_name} - {self.suffix}"

    @property
    def participant_status(self):
        if self.resolution == self.Resolution.NOT_OCCURRING:
            return "Not occurring this year"
        if self.all_available_faculty:
            return "Complete"
        count = self.participants.count()
        if count == 0:
            return "Scheduled, unpopulated"
        if count < self.required_participant_count:
            return "Scheduled, needs participants"
        return "Complete"

    def __str__(self):
        if self.resolution == self.Resolution.NOT_OCCURRING:
            return f"{self.base_name}: not occurring in {academic_year_label(self.academic_year)}"
        return f"{self.title}: {self.date_range_label}"

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Academic Event annual resolutions are retained and cannot be deleted."
        )


class AcademicEventDefinitionQuerySet(models.QuerySet):
    def effective_for(self, academic_year):
        return (
            self.filter(is_active=True, start_academic_year__lte=academic_year)
            .filter(Q(end_academic_year__isnull=True) | Q(end_academic_year__gte=academic_year))
            .order_by(
                "scheduling_mode",
                "expected_quarter",
                models.Case(
                    models.When(typically_scheduled="", then=models.Value(1)),
                    default=models.Value(0),
                ),
                Lower("typically_scheduled"),
                "typically_scheduled",
                Lower("base_name"),
                "base_name",
                "pk",
            )
        )


class AcademicEventDefinition(models.Model):
    """A stable Academic Event name expected in an inclusive academic-year range."""

    class SchedulingMode(models.TextChoices):
        YEARLY = "yearly", "Yearly"
        MONTHLY_RULES = "monthly_rules", "Monthly rules"

    base_name = models.CharField(max_length=120)
    scheduling_mode = models.CharField(
        max_length=16, choices=SchedulingMode.choices, default=SchedulingMode.YEARLY
    )
    start_academic_year = models.PositiveIntegerField()
    end_academic_year = models.PositiveIntegerField(null=True, blank=True)
    expected_quarter = models.CharField(
        max_length=2,
        choices=AcademicEventQuarter.choices,
        null=True,
        blank=True,
        default=AcademicEventQuarter.Q1,
    )
    default_duration_days = models.PositiveIntegerField(default=1)
    default_period_of_day = models.CharField(
        max_length=9,
        choices=AcademicEvent.PeriodOfDay.choices,
        default=AcademicEvent.PeriodOfDay.WHOLE_DAY,
    )
    default_precepting_impact = models.CharField(
        max_length=4,
        choices=AcademicEvent.PreceptingImpact.choices,
        default=AcademicEvent.PreceptingImpact.HARD,
    )
    default_required_participant_count = models.PositiveIntegerField(default=1)
    all_available_faculty = models.BooleanField(default=False)
    default_participants = models.ManyToManyField(
        Physician, related_name="academic_event_default_definitions", blank=True
    )
    typically_scheduled = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AcademicEventDefinitionQuerySet.as_manager()

    class Meta:
        ordering = ("expected_quarter", "base_name", "pk")

    def clean(self):
        super().clean()
        if not self.base_name or not self.base_name.strip():
            raise ValidationError({"base_name": "Base name cannot be blank."})
        if (
            self.start_academic_year
            and self.end_academic_year
            and self.end_academic_year < self.start_academic_year
        ):
            raise ValidationError(
                {"end_academic_year": "End academic year cannot be before start academic year."}
            )
        _validate_impact_coverage(self.default_precepting_impact, self.default_period_of_day)
        if self.scheduling_mode == self.SchedulingMode.YEARLY and not self.expected_quarter:
            raise ValidationError(
                {"expected_quarter": "Yearly definitions need an expected quarter."}
            )
        if self.scheduling_mode == self.SchedulingMode.MONTHLY_RULES and self.expected_quarter:
            raise ValidationError(
                {"expected_quarter": "Monthly-rules definitions have no expected quarter."}
            )
        if self.default_duration_days is not None and self.default_duration_days < 1:
            raise ValidationError(
                {"default_duration_days": "Default duration must be a positive whole number."}
            )
        if (
            self.default_required_participant_count is not None
            and self.default_required_participant_count < 1
        ):
            raise ValidationError(
                {
                    "default_required_participant_count": (
                        "Default required participant count must be a positive whole number."
                    )
                }
            )
        if (
            self.default_duration_days is not None
            and self.default_duration_days >= 1
            and self.scheduling_mode == self.SchedulingMode.YEARLY
            and self.expected_quarter in AcademicEventQuarter.values
        ):
            from ..academic_years import default_duration_fits_expected_quarter

            if not default_duration_fits_expected_quarter(
                self.expected_quarter, self.default_duration_days
            ):
                raise ValidationError(
                    {
                        "default_duration_days": (
                            "Default duration must fit within an academic year when it starts "
                            "on the first day of the expected quarter."
                        )
                    }
                )
        if self.has_annual_use:
            used_years = self.annual_events.values_list("academic_year", flat=True)
            if not self.is_active:
                raise ValidationError(
                    "Cannot remove: this definition has scheduled or retained use."
                )
            if any(
                year < self.start_academic_year
                or (self.end_academic_year is not None and year > self.end_academic_year)
                for year in used_years
            ):
                raise ValidationError(
                    "A definition's effective range cannot exclude an academic year with annual use."
                )
        if not self.is_active or not self.base_name or not self.start_academic_year:
            return

        overlapping = AcademicEventDefinition.objects.filter(
            is_active=True,
            base_name__iexact=self.base_name.strip(),
            scheduling_mode=self.scheduling_mode,
            expected_quarter=self.expected_quarter,
        ).exclude(pk=self.pk)
        if self.end_academic_year:
            overlapping = overlapping.filter(start_academic_year__lte=self.end_academic_year)
        overlapping = overlapping.filter(
            Q(end_academic_year__isnull=True) | Q(end_academic_year__gte=self.start_academic_year)
        )
        conflict = overlapping.order_by("start_academic_year", "pk").first()
        if conflict:
            overlap_year = max(self.start_academic_year, conflict.start_academic_year)
            raise ValidationError(
                "An active definition with this base name already applies to "
                f"{academic_year_label(overlap_year)}."
            )

    def applies_to(self, academic_year):
        return (
            self.is_active
            and self.start_academic_year <= academic_year
            and (self.end_academic_year is None or academic_year <= self.end_academic_year)
        )

    @property
    def has_annual_use(self):
        """Annual resolutions remain use even after they are retained."""
        return (
            self.pk is not None
            and self.annual_events.filter(monthly_rule_source_id__isnull=True).exists()
        )

    @property
    def effective_range_label(self):
        start = academic_year_label(self.start_academic_year)
        if self.end_academic_year is None:
            return f"{start} onward"
        return f"{start} through {academic_year_label(self.end_academic_year)}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Academic Event definitions are retained and cannot be deleted.")

    def __str__(self):
        return f"{self.base_name}: {self.effective_range_label}"


class AcademicEventMonthlyRule(models.Model):
    """One effective-dated ordinal weekday recurrence owned by an Event definition."""

    WEEKDAY_CHOICES = (
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    )

    definition = models.ForeignKey(
        AcademicEventDefinition, on_delete=models.PROTECT, related_name="monthly_rules"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    ordinals = models.JSONField()
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("definition__base_name", "weekday", "effective_start", "pk")

    def clean(self):
        super().clean()
        if (
            self.definition_id
            and self.definition.scheduling_mode != self.definition.SchedulingMode.MONTHLY_RULES
        ):
            raise ValidationError("Only Monthly-rules definitions can have recurrence rules.")
        if self.weekday not in range(7):
            raise ValidationError({"weekday": "Choose one day of the week."})
        if (
            not isinstance(self.ordinals, list)
            or not self.ordinals
            or set(self.ordinals) - set(range(1, 6))
        ):
            raise ValidationError(
                {"ordinals": "Choose one or more of the first through fifth occurrences."}
            )
        if len(set(self.ordinals)) != len(self.ordinals):
            raise ValidationError({"ordinals": "Each occurrence can be selected only once."})
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})
        overlaps = AcademicEventMonthlyRule.objects.filter(
            definition=self.definition, weekday=self.weekday
        ).exclude(pk=self.pk)
        if self.effective_end:
            overlaps = overlaps.filter(
                Q(effective_start__isnull=True) | Q(effective_start__lte=self.effective_end)
            )
        if self.effective_start:
            overlaps = overlaps.filter(
                Q(effective_end__isnull=True) | Q(effective_end__gte=self.effective_start)
            )
        if overlaps.exists():
            raise ValidationError(
                "Academic Event rules for this definition and weekday cannot overlap."
            )

    def matches(self, on_date):
        return (
            on_date.weekday() == self.weekday
            and (on_date.day - 1) // 7 + 1 in self.ordinals
            and (not self.effective_start or self.effective_start <= on_date)
            and (not self.effective_end or on_date <= self.effective_end)
        )

    @property
    def ordinal_display(self):
        labels = ("First", "Second", "Third", "Fourth", "Fifth")
        return ", ".join(labels[ordinal - 1] for ordinal in self.ordinals)


class AcademicEventMonthlyRuleChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        REMOVED = "removed", "Removed"

    rule = models.ForeignKey(
        AcademicEventMonthlyRule,
        on_delete=models.SET_NULL,
        related_name="changes",
        null=True,
        blank=True,
    )
    source_id_at_recording = models.PositiveBigIntegerField()
    definition = models.ForeignKey(
        AcademicEventDefinition, on_delete=models.PROTECT, related_name="monthly_rule_changes"
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")


def _validate_impact_coverage(impact, coverage):
    if coverage == AcademicEvent.PeriodOfDay.NONE and impact != AcademicEvent.PreceptingImpact.NONE:
        raise ValidationError({"period_of_day": "None coverage requires None Precepting impact."})
    if (
        impact in (AcademicEvent.PreceptingImpact.HARD, AcademicEvent.PreceptingImpact.SOFT)
        and coverage == AcademicEvent.PeriodOfDay.NONE
    ):
        raise ValidationError(
            {
                "period_of_day": "Hard or Soft Precepting impact requires AM, PM, or Whole day coverage."
            }
        )


class AcademicEventDateRange(models.Model):
    """One inclusive, nonoverlapping covered period of an Academic Event."""

    event = models.ForeignKey(AcademicEvent, on_delete=models.CASCADE, related_name="ranges")
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ("start_date", "end_date", "pk")

    def clean(self):
        super().clean()
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})
        from ..academic_years import academic_year_for_date

        if self.event_id and (
            academic_year_for_date(self.start_date) != self.event.academic_year
            or academic_year_for_date(self.end_date) != self.event.academic_year
        ):
            raise ValidationError(
                "Occurrence dates must both fall within the selected academic year."
            )


class AcademicEventDefinitionChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        DEACTIVATED = "deactivated", "Deactivated"

    definition = models.ForeignKey(
        AcademicEventDefinition,
        on_delete=models.SET_NULL,
        related_name="changes",
        null=True,
        blank=True,
    )
    definition_id_at_recording = models.PositiveBigIntegerField()
    action = models.CharField(max_length=12, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_academic_event_definition_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Academic Event definition changes are retained and cannot be deleted."
        )

    def __str__(self):
        return f"Academic Event definition {self.get_action_display()} at {self.changed_at}"


class AcademicEventChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        REMOVED = "removed", "Removed"
        NOT_OCCURRING = "not_occurring", "Not occurring this year"
        REVERSED = "reversed", "Not-occurring decision reversed"

    event = models.ForeignKey(
        AcademicEvent, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    event_id_at_recording = models.PositiveBigIntegerField()
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_academic_event_changes",
        null=True,
        blank=True,
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def __str__(self):
        return f"Academic event {self.get_action_display()} at {self.changed_at}"


__all__ = (
    "AcademicEvent",
    "AcademicEventChange",
    "AcademicEventDateRange",
    "AcademicEventDefinition",
    "AcademicEventDefinitionChange",
    "AcademicEventMonthlyRule",
    "AcademicEventMonthlyRuleChange",
    "AcademicEventQuarter",
)
