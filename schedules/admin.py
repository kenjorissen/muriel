from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .forms.accounts import AccountChangeForm, AccountCreationForm
from .models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    AcademicEventMonthlyRuleChange,
    AccountChange,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulePeriod,
    Vacation,
    VacationChange,
)

User = get_user_model()


admin.site.unregister(User)


@admin.register(User)
class AccountAdmin(UserAdmin):
    form = AccountChangeForm
    add_form = AccountCreationForm
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "email", "password1", "password2")}),
    )


class RetainedPhysiciansRecordAdmin(admin.ModelAdmin):
    """Keep physicians facts inspectable without bypassing their service mutations."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountChange)
class AccountChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("account", "action", "changed_by", "changed_at")
    readonly_fields = (
        "account",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(SchedulePeriod)
class SchedulePeriodAdmin(admin.ModelAdmin):
    list_display = ("month", "created_at")
    ordering = ("-month",)


@admin.register(PreceptorSession)
class PreceptorSessionAdmin(admin.ModelAdmin):
    list_display = ("period", "date", "period_of_day")
    list_filter = ("period_of_day",)
    ordering = ("date", "period_of_day")


@admin.register(PreceptorAssignment)
class PreceptorAssignmentAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("session", "physician", "responsibility", "primary_position", "is_locked")


@admin.register(PreceptorAssignmentChange)
class PreceptorAssignmentChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("session", "action", "changed_by", "changed_at")
    readonly_fields = (
        "session",
        "assignment_id",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(FindingAcknowledgement)
class FindingAcknowledgementAdmin(RetainedPhysiciansRecordAdmin):
    list_display = (
        "identity_key",
        "acknowledged_by",
        "acknowledged_at",
        "removed_by",
        "removed_at",
        "no_longer_applies_at",
    )
    readonly_fields = (
        "identity",
        "identity_key",
        "reason",
        "acknowledged_by",
        "acknowledged_at",
        "removed_by",
        "removed_at",
        "no_longer_applies_at",
    )


@admin.register(Vacation)
class VacationAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "start_date", "end_date", "period_of_day", "note")


@admin.register(VacationChange)
class VacationChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "action", "changed_by", "changed_at")
    readonly_fields = (
        "vacation",
        "vacation_id_at_recording",
        "physician",
        "action",
        "previous_value",
        "recorded_value",
        "previous_note",
        "recorded_note",
        "changed_by",
        "changed_at",
    )


@admin.register(AcademicEvent)
class AcademicEventAdmin(RetainedPhysiciansRecordAdmin):
    list_display = (
        "title",
        "start_date",
        "end_date",
        "period_of_day",
        "monthly_rule_source_id",
        "assignment_is_default",
    )
    list_filter = ("period_of_day",)


@admin.register(AcademicEventChange)
class AcademicEventChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("event_id_at_recording", "action", "changed_by", "changed_at")
    readonly_fields = (
        "event",
        "event_id_at_recording",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(AcademicEventDefinition)
class AcademicEventDefinitionAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("base_name", "start_academic_year", "end_academic_year", "is_active")


@admin.register(AcademicEventDefinitionChange)
class AcademicEventDefinitionChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("definition_id_at_recording", "action", "changed_by", "changed_at")
    readonly_fields = (
        "definition",
        "definition_id_at_recording",
        "action",
        "previous_value",
        "recorded_value",
        "changed_by",
        "changed_at",
    )


@admin.register(AcademicEventMonthlyRule)
class AcademicEventMonthlyRuleAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("definition", "weekday", "ordinals", "effective_start", "effective_end")


@admin.register(AcademicEventMonthlyRuleChange)
class AcademicEventMonthlyRuleChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("definition", "source_id_at_recording", "action", "changed_by", "changed_at")
    readonly_fields = (
        "rule",
        "source_id_at_recording",
        "definition",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(NormalRounder)
class NormalRounderAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("week", "physician")


@admin.register(CasualRounderRule)
class CasualRounderRuleAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "weekday", "effective_start", "effective_end")


@admin.register(RoundingScheduleChange)
class RoundingScheduleChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("subject", "physician", "action", "changed_by", "changed_at")
    readonly_fields = (
        "normal_rounder",
        "casual_rounder_rule",
        "source_id_at_recording",
        "subject",
        "physician",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(CommunityPreceptorRule)
class CommunityPreceptorRuleAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "weekday", "periods", "effective_start", "effective_end")


@admin.register(CommunityPreceptorScheduleChange)
class CommunityPreceptorScheduleChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "action", "changed_by", "changed_at")
    readonly_fields = (
        "community_preceptor_rule",
        "source_id_at_recording",
        "physician",
        "action",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )


@admin.register(Physician)
class PhysicianAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("display_name", "created_at")
    search_fields = ("display_name",)


@admin.register(EffectiveRole)
class EffectiveRoleAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "role", "effective_start", "effective_end")
    list_filter = ("role",)


@admin.register(FacultyAvailabilityTemplate)
class FacultyAvailabilityTemplateAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "effective_start", "effective_end")


@admin.register(PhysicianChange)
class PhysicianChangeAdmin(RetainedPhysiciansRecordAdmin):
    list_display = ("physician", "subject", "changed_by", "changed_at")
    readonly_fields = (
        "physician",
        "subject",
        "subject_id",
        "previous_value",
        "recorded_value",
        "reason",
        "changed_by",
        "changed_at",
    )
